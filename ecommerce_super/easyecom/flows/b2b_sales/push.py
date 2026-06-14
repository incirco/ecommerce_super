"""§11 Stage 2 — B2B Sales Order push dispatcher + response handlers.

Two doc_event handlers wire into hooks.py:
  - validate_pre_push   ← Sales Order.validate
  - on_submit_push      ← Sales Order.on_submit

The split matters: preconditions run at validate (before save) so a
defective SO never reaches half-state in the DB; the actual EE push
enqueues at on_submit (after save, after commit) so the queue job
sees the persisted SO.

Async queue job — `push_b2b_order_async`:
  1. Build the per-module payload via payload_builder.
  2. POST to /webhook/v2/createOrder via EasyEcomClient.
  3. Persist response into a new EasyEcom B2B Order Map row.
  4. Set the SO back-reference (SO.ecs_b2b_order_map).
  5. Upsert an EasyEcom Sync Record (§7 contract — entity-centric
     status tracking).

Refusal discipline: every throw goes through frappe.throw with an
explicit title; users see WHICH precondition failed. EE failures
land as Integration Discrepancies (§9-pattern _raise_discrepancy)
with the EE Account name + correlation_id inlined into the reason
text per the established pattern.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import CREATE_ORDER
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomError,
)
from ecommerce_super.easyecom.flows.b2b_sales.gating import (
    is_section_11_gated,
    validate_preconditions,
)
from ecommerce_super.easyecom.flows.b2b_sales.payload_builder import (
    build_new_b2b_payload,
    build_old_b2b_payload,
    compute_payload_hash,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_account_for_warehouse,
    get_ee_location_for_warehouse,
)
from ecommerce_super.easyecom.queue import enqueue_easyecom_job
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.idempotency import so_push_key


# ============================================================
# Hook handlers — wired in hooks.py.
# ============================================================


def validate_pre_push(doc: Any, method: str | None = None) -> None:
    """Sales Order.validate hook.

    Runs Gate 0 + preconditions. Preconditions THROW on failure,
    blocking the SO save. Non-gated SOs fall through silently —
    pure ERPNext path, integration not involved.
    """
    if doc.doctype != "Sales Order":
        return
    if not is_section_11_gated(doc):
        return
    ee_account = get_ee_account_for_warehouse(doc.set_warehouse)
    if not ee_account:
        # Defensive: Gate 0 said the warehouse is EE-mapped but the
        # Location's easyecom_account pointer is empty. Refuse with
        # a specific message so the FDE knows where to look.
        frappe.throw(
            _(
                "Warehouse {0} maps to an EasyEcom Location but the "
                "Location has no easyecom_account set. Configure the "
                "Location's account before submitting this SO."
            ).format(doc.set_warehouse),
            title=_("EE Account Unresolved"),
        )
    validate_preconditions(doc, ee_account)


def on_submit_push(doc: Any, method: str | None = None) -> None:
    """Sales Order.on_submit hook.

    Gate 0 + enqueue the async push. Preconditions already ran at
    validate time — this hook trusts that the SO is push-ready.
    """
    if doc.doctype != "Sales Order":
        return
    if not is_section_11_gated(doc):
        return
    ee_account = get_ee_account_for_warehouse(doc.set_warehouse)
    if not ee_account:
        return  # validate caught this; defensive double-check
    ee_location = get_ee_location_for_warehouse(doc.set_warehouse)
    location_key = (
        getattr(ee_location, "location_key", None) if ee_location else None
    )
    idem_key = so_push_key(
        company=doc.company,
        so_name=doc.name,
        ee_location_key=str(location_key or ""),
    )
    enqueue_easyecom_job(
        job_type="b2b_push",
        company=doc.company,
        target_doctype="Sales Order",
        target_name=doc.name,
        idempotency_key=idem_key,
        correlation_id=new_correlation_id(),
    )


# ============================================================
# Queue job worker — registered via queue routing.
# ============================================================


def push_b2b_order_async(
    *,
    sales_order: str,
    easyecom_account: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Background entry that builds the payload, POSTs to EE, and
    persists the B2B Order Map row + SO back-reference.

    Returns a structured outcome dict (operation, status, map_name,
    ee_*ids). Used by tests + by trace_b2b_so for diagnostics.
    """
    so = frappe.get_doc("Sales Order", sales_order)

    ee_account = frappe.get_doc(
        "EasyEcom Account",
        easyecom_account or get_ee_account_for_warehouse(so.set_warehouse).name,
    )
    module = (ee_account.get("ecs_b2b_module") or "").strip()
    if module not in ("Old B2B", "New B2B"):
        raise frappe.ValidationError(
            f"ecs_b2b_module not configured on EE Account {ee_account.name}"
        )

    ee_location = get_ee_location_for_warehouse(so.set_warehouse)
    location_key = (
        str(getattr(ee_location, "location_key", "")) if ee_location else ""
    )
    correlation_id = correlation_id or new_correlation_id()

    if module == "Old B2B":
        payload = build_old_b2b_payload(so, ee_account)
    else:
        payload = build_new_b2b_payload(so, ee_account)
    payload_hash = compute_payload_hash(payload)

    _write_sync_record(
        so=so,
        ee_account=ee_account,
        location_key=location_key,
        correlation_id=correlation_id,
        idempotency_key=so_push_key(
            company=so.company, so_name=so.name, ee_location_key=location_key
        ),
        status="Running",
    )

    try:
        client = EasyEcomClient(location_key=location_key)
        response = client.post(
            CREATE_ORDER,
            payload=payload,
            correlation_id=correlation_id,
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        _record_push_failure(
            so=so,
            ee_account=ee_account,
            payload=payload,
            payload_hash=payload_hash,
            correlation_id=correlation_id,
            location_key=location_key,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        raise

    if module == "Old B2B":
        return _handle_old_b2b_response(
            so=so,
            ee_account=ee_account,
            payload=payload,
            payload_hash=payload_hash,
            response=response,
            correlation_id=correlation_id,
            location_key=location_key,
        )
    return _handle_new_b2b_response(
        so=so,
        ee_account=ee_account,
        payload=payload,
        payload_hash=payload_hash,
        response=response,
        correlation_id=correlation_id,
        location_key=location_key,
    )


# ============================================================
# Response handlers.
# ============================================================


def _handle_old_b2b_response(
    *,
    so: Any,
    ee_account: Any,
    payload: dict,
    payload_hash: str,
    response: dict,
    correlation_id: str,
    location_key: str,
) -> dict:
    """Old B2B returns OrderID + SuborderID + InvoiceID synchronously.

    Order of operations:
      1. Insert Map row with ee_*ids populated and status='Pushed'.
      2. Set SO.ecs_b2b_order_map back-reference.
      3. Transition Sync Record to Success.
      4. Commit so the back-ref survives the queue-job transaction.
    """
    if int(response.get("code") or 0) != 200:
        _record_push_failure(
            so=so,
            ee_account=ee_account,
            payload=payload,
            payload_hash=payload_hash,
            correlation_id=correlation_id,
            location_key=location_key,
            error_msg=(
                response.get("message") or "Unknown EE error"
            ),
        )
        return {
            "operation": "failed",
            "status": "Failed",
            "map_name": None,
            "ee_order_id": None,
        }

    data = response.get("data") or {}
    map_doc = frappe.new_doc("EasyEcom B2B Order Map")
    map_doc.update(
        {
            "sales_order": so.name,
            "easyecom_account": ee_account.name,
            "module": "Old B2B",
            "ee_order_id": str(data.get("OrderID") or "").strip() or None,
            "ee_suborder_id": str(data.get("SuborderID") or "").strip() or None,
            "ee_invoice_id": str(data.get("InvoiceID") or "").strip() or None,
            "status": "Pushed",
            "pushed_at": frappe.utils.now(),
            "payload_hash": payload_hash,
            "request_payload": frappe.as_json(payload),
            "response_payload": frappe.as_json(response),
            "last_error": None,
        }
    )
    map_doc.insert(ignore_permissions=True)

    # SO back-reference — set via db.set_value to bypass SO.validate
    # and avoid touching modified-by/etc on a submitted document.
    frappe.db.set_value(
        "Sales Order",
        so.name,
        "ecs_b2b_order_map",
        map_doc.name,
        update_modified=False,
    )

    _write_sync_record(
        so=so,
        ee_account=ee_account,
        location_key=location_key,
        correlation_id=correlation_id,
        idempotency_key=so_push_key(
            company=so.company, so_name=so.name, ee_location_key=location_key
        ),
        status="Success",
    )
    frappe.db.commit()

    return {
        "operation": "pushed",
        "status": "Pushed",
        "map_name": map_doc.name,
        "ee_order_id": map_doc.ee_order_id,
        "ee_suborder_id": map_doc.ee_suborder_id,
        "ee_invoice_id": map_doc.ee_invoice_id,
    }


def _handle_new_b2b_response(
    *,
    so: Any,
    ee_account: Any,
    payload: dict,
    payload_hash: str,
    response: dict,
    correlation_id: str,
    location_key: str,
) -> dict:
    """New B2B returns 'Successfully Queued' with empty data.

    The EE-side identifiers arrive later via Stage 3's polling. We
    insert the Map row with ee_*ids = None and status='Queued'; the
    polling reconciler backfills + transitions to 'Pushed' once EE
    has assigned an OrderID.
    """
    if int(response.get("code") or 0) != 200:
        _record_push_failure(
            so=so,
            ee_account=ee_account,
            payload=payload,
            payload_hash=payload_hash,
            correlation_id=correlation_id,
            location_key=location_key,
            error_msg=(
                response.get("message") or "Unknown EE error"
            ),
        )
        return {
            "operation": "failed",
            "status": "Failed",
            "map_name": None,
            "ee_order_id": None,
        }

    map_doc = frappe.new_doc("EasyEcom B2B Order Map")
    map_doc.update(
        {
            "sales_order": so.name,
            "easyecom_account": ee_account.name,
            "module": "New B2B",
            "ee_order_id": None,
            "ee_suborder_id": None,
            "ee_invoice_id": None,
            "status": "Queued",
            "pushed_at": frappe.utils.now(),
            "payload_hash": payload_hash,
            "request_payload": frappe.as_json(payload),
            "response_payload": frappe.as_json(response),
            "last_error": None,
        }
    )
    map_doc.insert(ignore_permissions=True)

    frappe.db.set_value(
        "Sales Order",
        so.name,
        "ecs_b2b_order_map",
        map_doc.name,
        update_modified=False,
    )

    _write_sync_record(
        so=so,
        ee_account=ee_account,
        location_key=location_key,
        correlation_id=correlation_id,
        idempotency_key=so_push_key(
            company=so.company, so_name=so.name, ee_location_key=location_key
        ),
        status="Success",
    )
    frappe.db.commit()

    return {
        "operation": "queued",
        "status": "Queued",
        "map_name": map_doc.name,
        "ee_order_id": None,
        "ee_suborder_id": None,
        "ee_invoice_id": None,
    }


# ============================================================
# Failure path — Integration Discrepancy + Sync Record → Failed.
# ============================================================


def _record_push_failure(
    *,
    so: Any,
    ee_account: Any,
    payload: dict,
    payload_hash: str,
    correlation_id: str,
    location_key: str,
    error_msg: str,
) -> None:
    """Persist a failure trace and raise an Integration Discrepancy.

    Does NOT insert a Map row in a Failed state — the Map row models
    "this SO has an EE-side counterpart"; a push that never succeeded
    has no such counterpart. The Discrepancy + Sync Record carry the
    failure narrative; the FDE retries by re-submitting (which
    re-enqueues the push via on_submit_push).
    """
    from ecommerce_super.easyecom.flows.grn_pull import (
        _raise_discrepancy,
    )

    _raise_discrepancy(
        kind="B2B Push Failed",
        reference_doctype="Sales Order",
        reference_name=so.name,
        company=so.company,
        reason=(
            f"§11 createOrder push to EE Account {ee_account.name} "
            f"failed (correlation_id={correlation_id}, "
            f"location_key={location_key}, payload_hash={payload_hash}). "
            f"Error: {error_msg}"
        ),
    )
    _write_sync_record(
        so=so,
        ee_account=ee_account,
        location_key=location_key,
        correlation_id=correlation_id,
        idempotency_key=so_push_key(
            company=so.company, so_name=so.name, ee_location_key=location_key
        ),
        status="Failed",
        last_error=error_msg,
    )
    frappe.db.commit()


# ============================================================
# Sync Record helper — §7 status tracking for the SO.
# ============================================================


def _write_sync_record(
    *,
    so: Any,
    ee_account: Any,
    location_key: str,
    correlation_id: str,
    idempotency_key: str,
    status: str,
    last_error: str | None = None,
) -> str | None:
    """Upsert + transition the §7 Sync Record for this SO push.

    Sync Record is the entity-centric status surface (one per
    (company, entity_doctype, entity_name, direction)). The Map row
    captures the EE-side counterpart; the Sync Record captures the
    integration's *attempt* + outcome.
    """
    try:
        from ecommerce_super.easyecom.doctype.easyecom_sync_record import (
            easyecom_sync_record as sync_record_mod,
        )
        sr = sync_record_mod.upsert(
            company=so.company,
            entity_doctype="Sales Order",
            entity_name=so.name,
            entity_type="Sales Order",
            direction="Push",
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            ee_location_key=location_key or None,
            status=status,
        )
        if status != "Pending":
            # Re-find + transition (upsert doesn't mutate on re-find).
            sr_name = sr.name if hasattr(sr, "name") else sr
            updates: dict = {"status": status}
            if last_error is not None:
                updates["last_error"] = last_error[:5000]
            frappe.db.set_value(
                "EasyEcom Sync Record", sr_name, updates,
                update_modified=False,
            )
        return sr.name if hasattr(sr, "name") else sr
    except Exception as exc:
        frappe.log_error(
            title=f"§11 push failed to write Sync Record for {so.name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return None
