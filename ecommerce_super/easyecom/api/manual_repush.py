"""FDE-facing "Re-fire §11 push" endpoint.

Backs the "Re-fire EasyEcom Push" button on the Sales Order form.
When the automatic on_submit push failed to enqueue (orphaned Queue
Job, worker restart during hook, or any transient) the SO sits in
limbo — submitted on our side, no B2B Order Map, EE has no idea it
exists. Before this endpoint, the only remediation was bench console
or waiting for the hourly resweep (gh#120).

Gates:
  - System Manager / EasyEcom System Manager / EasyEcom FDE only
  - §11 Gate 0 must pass (SO must be genuinely EE-eligible)
  - SO must be submitted (docstatus=1)
  - If a B2B Order Map already exists → returns idempotent no-op
    (don't create duplicates)
"""
from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.queue import enqueue_easyecom_job
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.idempotency import so_push_key


_ALLOWED_ROLES = frozenset({
    "System Manager",
    "EasyEcom System Manager",
    "EasyEcom FDE",
})


@frappe.whitelist()
def repush_so(so_name: str) -> dict[str, Any]:
    """Manually re-enqueue the §11 push for a submitted SO.

    Idempotent: if a B2B Order Map already exists, returns
    `{"ok": True, "already_mapped": True, ...}` without enqueuing.

    Returns on success:
      {"ok": True, "queue_job": "<name>", "correlation_id": "<uuid>",
       "message": "Re-push enqueued."}

    Returns on skip/failure:
      {"ok": False, "message": "<reason>"}
    """
    # Role gate.
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(_ALLOWED_ROLES):
        frappe.throw(
            frappe._("Re-fire push requires System Manager or EasyEcom FDE."),
            frappe.PermissionError,
        )

    try:
        so = frappe.get_doc("Sales Order", so_name)
    except frappe.DoesNotExistError:
        return {"ok": False, "message": f"Sales Order {so_name!r} not found."}

    if so.docstatus != 1:
        return {
            "ok": False,
            "message": (
                f"SO {so_name!r} is not submitted (docstatus={so.docstatus}). "
                "Submit the SO before firing the §11 push."
            ),
        }

    # Idempotency — if the outbound push already produced a Map row,
    # nothing to do.
    existing_map = frappe.db.get_value(
        "EasyEcom B2B Order Map", {"sales_order": so_name}, "name"
    )
    if existing_map:
        return {
            "ok": True,
            "already_mapped": True,
            "b2b_order_map": existing_map,
            "message": (
                f"B2B Order Map {existing_map!r} already exists. "
                "Nothing to re-fire."
            ),
        }

    # Gate 0.
    from ecommerce_super.easyecom.flows.b2b_sales.gating import (
        is_section_11_gated,
    )
    from ecommerce_super.easyecom.helpers.warehouse_mapping import (
        get_ee_account_for_warehouse,
        get_ee_location_for_warehouse,
    )
    if not is_section_11_gated(so):
        return {
            "ok": False,
            "message": (
                f"SO {so_name!r} does not pass §11 Gate 0. "
                "Warehouse is not EE-mapped (no live+enabled EasyEcom "
                "Location with mapped_warehouse=set_warehouse). "
                "Fix the setup, then retry."
            ),
        }

    ee_account = get_ee_account_for_warehouse(so.set_warehouse)
    if not ee_account:
        return {
            "ok": False,
            "message": (
                "Warehouse is EE-mapped but no EasyEcom Account resolves. "
                "Check the EasyEcom Location's easyecom_account link."
            ),
        }

    ee_location = get_ee_location_for_warehouse(so.set_warehouse)
    location_key = (
        getattr(ee_location, "location_key", None) if ee_location else None
    )

    correlation_id = new_correlation_id()
    idem_key = so_push_key(
        company=so.company,
        so_name=so.name,
        ee_location_key=str(location_key or ""),
    )
    qj = enqueue_easyecom_job(
        job_type="SO Push",
        company=so.company,
        target_doctype="Sales Order",
        target_name=so.name,
        idempotency_key=idem_key,
        correlation_id=correlation_id,
    )
    qj_name = getattr(qj, "name", None) or (
        qj.get("name") if isinstance(qj, dict) else None
    )
    return {
        "ok": True,
        "queue_job": qj_name,
        "correlation_id": correlation_id,
        "message": (
            "Re-push enqueued. Watch EasyEcom Queue Job / EasyEcom API Call "
            "for the outbound createOrder call."
        ),
    }
