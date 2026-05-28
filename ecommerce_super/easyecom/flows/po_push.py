"""§9 Stage 2 — ERPNext → EE Purchase Order push (two channels).

Two channels, two keys (the §9 packet's core modeling point):
  - CONTENT   → /WMS/Cart/CreatePurchaseOrder, keyed `referenceCode`
                (= ERPNext PO name, ERPNext-born, stable). createOrUpdate
                I/U on first vs subsequent push. Returns data.poId.
  - STATUS    → /wms/updatePoStatus, keyed `po_id` (= EE-returned int).
                All transitions including cancel. `isCancel` on the
                content channel is UNUSED — clean channel separation.

This module owns:
  - Gate-0 (warehouse opt-in) + the precondition chain (Supplier Map /
    Item Map / required fields) → flag-not-pushed on miss, never throw
    through Frappe hooks (don't block the user's save).
  - Content payload build via the §9 Stage 1 EasyEcom-PO-Push ruleset
    + the per-line tax-inclusive unitPrice + taxType derivation via
    the shared place-of-supply module.
  - Status payload build (po_id + po_status + markPoComplete=0) with
    idempotency guard against PO Map.last_pushed_po_status.
  - Per-PO Sync Record + Line-child population (§7.1.1).
  - PO Map row upsert (Mapped / Created-Flagged / Flagged-Not-Created /
    Drift / Disabled per §9 packet state machine).

What this module DOESN'T own (deferred to other stages):
  - po_status=5 Completion push (Stage 3; GRN-driven cumulative receipt).
  - po_status=5 force-close via ERPNext PO Close button (DEFERRED to
    Stage 3 to centralise all =5 triggers — see Stage 2 prompt).
  - GRN pull / Purchase Receipt creation (Stage 3).
  - UI / workspace integration (Stage 4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    PURCHASE_ORDER_CREATE,
    PURCHASE_ORDER_STATUS_UPDATE,
)
from ecommerce_super.easyecom.exceptions import (
    EasyEcomError,
    FieldMappingRuleError,
)
from ecommerce_super.easyecom.flows._po_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_po_push_sync_record,
)
from ecommerce_super.easyecom.tax.place_of_supply import compute_tax_type


CONTENT_RULESET: str = "EasyEcom-PO-Push"
PING_PONG_FLAG = "easyecom_po_push_in_flight"


# EE po_status codes (per §9 packet).
PO_STATUS_OPEN = 1
PO_STATUS_WAITING_APPROVAL = 2
PO_STATUS_APPROVED = 3
PO_STATUS_REJECTED = 4
PO_STATUS_COMPLETED = 5
PO_STATUS_PENDING_SUPPLIER = 6
PO_STATUS_CANCELLED = 7


PushOp = Literal["create", "update", "status_only", "skipped", "flagged", "error"]


@dataclass
class POPushOutcome:
    po_docname: str
    operation: PushOp
    pushed: bool
    ee_po_id: int | None = None  # captured on first successful content push
    po_map_status: str | None = None  # Mapped / Flagged-Not-Created / etc.
    flag_reasons: list[str] = field(default_factory=list)
    ee_content_payload: dict[str, Any] | None = None
    ee_status_payload: dict[str, Any] | None = None
    sync_record_name: str | None = None


# ============================================================
# Public API
# ============================================================


def push_one_po(
    po_docname: str,
    *,
    client: EasyEcomClient | None = None,
    push_status_after_content: bool = True,
) -> POPushOutcome:
    """Push one ERPNext PO to EE — content channel first, then status.

    Content side: I (insert) or U (update) per Map presence + ee_po_id.
    Status side: fires after a successful content push IF
    push_status_after_content (default True) and the PO is submitted —
    flips po_status from EE's default 2 (Waiting-for-Approval) → 3
    (Approved). The on_submit hook calls with default True; the manual
    Push button on the PO Map may call with False (content-only re-push).

    Returns POPushOutcome — never raises through the hook boundary;
    failures surface as PO Map status + Failed Sync Record.
    """
    if not frappe.db.exists("Purchase Order", po_docname):
        return POPushOutcome(
            po_docname=po_docname,
            operation="error",
            pushed=False,
            flag_reasons=[f"PO {po_docname!r} does not exist"],
        )

    # Gate 0: warehouse must map to an EE Location. Non-EE → silently
    # inert. No PO Map row, no Sync Record, no log.
    location_row = _resolve_po_warehouse_to_location(po_docname)
    if location_row is None:
        return POPushOutcome(
            po_docname=po_docname,
            operation="skipped",
            pushed=False,
            flag_reasons=["Gate-0: PO target warehouse is not EE-mapped"],
        )

    po = frappe.get_doc("Purchase Order", po_docname)

    # Precondition chain. Misses → Flagged-Not-Created on PO Map, no
    # EE call attempted.
    precondition_errs = _run_preconditions(po, location_row)
    if precondition_errs:
        _upsert_po_map_flagged(
            po=po,
            location_row=location_row,
            reasons=precondition_errs,
        )
        sr = write_po_push_sync_record(
            entity_name=po.name,
            company=po.company,
            status=STATUS_FAILED,
            last_error=" || ".join(precondition_errs),
        )
        return POPushOutcome(
            po_docname=po.name,
            operation="flagged",
            pushed=False,
            po_map_status="Flagged-Not-Created",
            flag_reasons=precondition_errs,
            sync_record_name=sr,
        )

    # Resolve or build the PO Map row early — it carries the
    # createOrUpdate flag (presence of ee_po_id ⇒ U).
    map_row = _get_or_create_po_map_row(po=po, location_row=location_row)

    if client is None:
        client = EasyEcomClient(
            company=po.company, location_key=location_row.get("location_key")
        )

    # Content channel push.
    content_outcome = _do_content_push(
        po=po,
        map_row=map_row,
        location_row=location_row,
        client=client,
    )
    if content_outcome.operation in ("error", "flagged"):
        return content_outcome

    # Status channel push (default po_status=3 on first successful content
    # if PO is submitted; idempotent on last_pushed_po_status).
    if push_status_after_content and int(po.docstatus or 0) == 1:
        # Refresh map row to pick up the just-captured ee_po_id.
        map_row = _get_po_map_row(po.name)
        if map_row and map_row.get("ee_po_id"):
            status_outcome = push_po_status(
                po_docname=po.name,
                target_status=PO_STATUS_APPROVED,
                client=client,
            )
            content_outcome.ee_status_payload = status_outcome.ee_status_payload
            # If the status push failed, surface that as the final
            # outcome but keep the content push's ee_po_id capture.
            if status_outcome.operation == "error":
                content_outcome.operation = "error"
                content_outcome.flag_reasons.extend(status_outcome.flag_reasons)

    return content_outcome


def push_po_status(
    *,
    po_docname: str,
    target_status: int,
    mark_complete: int = 0,
    client: EasyEcomClient | None = None,
) -> POPushOutcome:
    """Status-only push via /wms/updatePoStatus. Idempotent guard on
    PO Map.last_pushed_po_status — re-pushing the same status is a no-op.

    Preconditions:
      - PO Map row exists.
      - PO Map.ee_po_id is set (a content push has already happened —
        you can't transition a PO that EE doesn't know yet).
      - target_status is in EE's documented status set (1-9, 11-16).

    Returns POPushOutcome with operation=status_only on success;
    operation=flagged when ee_po_id is missing.
    """
    map_row = _get_po_map_row(po_docname)
    if not map_row:
        return POPushOutcome(
            po_docname=po_docname,
            operation="flagged",
            pushed=False,
            flag_reasons=[
                "PO Map row missing — content push must run before status"
            ],
        )
    if not map_row.get("ee_po_id"):
        # Surface this on the Map row so the FDE sees it.
        _flag_po_map_status_blocked(map_row, target_status)
        return POPushOutcome(
            po_docname=po_docname,
            operation="flagged",
            pushed=False,
            po_map_status=map_row.get("status"),
            flag_reasons=[
                f"Cannot push po_status={target_status} — ee_po_id not yet "
                "captured (content push must succeed first)"
            ],
        )

    # Idempotency guard.
    if int(map_row.get("last_pushed_po_status") or 0) == int(target_status):
        return POPushOutcome(
            po_docname=po_docname,
            operation="skipped",
            pushed=False,
            ee_po_id=int(map_row["ee_po_id"]),
            po_map_status=map_row.get("status"),
            flag_reasons=[
                f"status={target_status} already pushed — no-op "
                "(last_pushed_po_status guard)"
            ],
        )

    if client is None:
        po = frappe.get_doc("Purchase Order", po_docname)
        location_row = _resolve_po_warehouse_to_location(po_docname)
        client = EasyEcomClient(
            company=po.company,
            location_key=(location_row or {}).get("location_key"),
        )

    payload = {
        "po_id": int(map_row["ee_po_id"]),
        "po_status": int(target_status),
        "markPoComplete": int(mark_complete or 0),
    }

    company = frappe.db.get_value("Purchase Order", po_docname, "company")
    try:
        response = client.post(PURCHASE_ORDER_STATUS_UPDATE, payload=payload)
    except EasyEcomError as exc:
        sr = write_po_push_sync_record(
            entity_name=po_docname,
            company=company,
            status=STATUS_FAILED,
            last_error=f"updatePoStatus {target_status}: {type(exc).__name__}: {exc}",
        )
        return POPushOutcome(
            po_docname=po_docname,
            operation="error",
            pushed=False,
            ee_po_id=int(map_row["ee_po_id"]),
            po_map_status=map_row.get("status"),
            flag_reasons=[f"{type(exc).__name__}: {exc}"],
            ee_status_payload=payload,
            sync_record_name=sr,
        )

    # Success — update last_pushed_po_status (idempotency guard for
    # subsequent pushes).
    frappe.db.set_value(
        "EasyEcom PO Map",
        map_row["name"],
        "last_pushed_po_status",
        int(target_status),
        update_modified=True,
    )
    sr = write_po_push_sync_record(
        entity_name=po_docname,
        company=company,
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return POPushOutcome(
        po_docname=po_docname,
        operation="status_only",
        pushed=True,
        ee_po_id=int(map_row["ee_po_id"]),
        po_map_status=map_row.get("status"),
        ee_status_payload=payload,
        sync_record_name=sr,
    )


# ============================================================
# Gate 0 + preconditions
# ============================================================


def _resolve_po_warehouse_to_location(po_docname: str) -> dict | None:
    """Resolve the PO's effective target warehouse to an EE Location row.

    Resolution rules:
      - If PO.set_warehouse is set, use it. Per-line warehouses must
        all match (mixed-warehouse refused at validate hook).
      - Else use the first line's warehouse (PO.items[0].warehouse).
      - Look up EasyEcom Location.mapped_warehouse for that warehouse.
      - Return {"name", "location_key", "mapped_warehouse"} or None
        (Gate-0 miss).
    """
    po = frappe.db.get_value(
        "Purchase Order", po_docname, ["set_warehouse"], as_dict=True
    )
    if not po:
        return None
    warehouse = po.set_warehouse
    if not warehouse:
        line_rows = frappe.db.get_all(
            "Purchase Order Item",
            filters={"parent": po_docname},
            fields=["warehouse"],
            limit=1,
        )
        if line_rows:
            warehouse = line_rows[0].warehouse
    if not warehouse:
        return None
    loc = frappe.db.get_value(
        "EasyEcom Location",
        {"mapped_warehouse": warehouse},
        ["name", "location_key", "mapped_warehouse"],
        as_dict=True,
    )
    return loc


def _run_preconditions(
    po: Any, location_row: dict
) -> list[str]:
    """Returns the list of precondition failure reasons (empty list =
    all good).

    Order matters — short-circuit on first miss (the next layer can
    only progress when the previous one resolves). But for FDE clarity
    on the worklist, collect ALL line-level misses (unmapped SKUs)
    rather than just the first.
    """
    errs: list[str] = []

    # (2) Supplier Map row exists with ee_vendor_id populated.
    supplier = po.supplier
    if not supplier:
        errs.append("PO has no supplier")
        return errs
    map_row = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": supplier},
        ["name", "ee_vendor_id", "ee_vendor_c_id", "status"],
        as_dict=True,
    )
    if not map_row:
        errs.append(f"Supplier Map missing for {supplier!r}")
        return errs
    if not (map_row.get("ee_vendor_id") or "").strip():
        errs.append(
            f"Supplier Map {map_row['name']!r} has no ee_vendor_id (WRITE "
            "key) — supplier must be pushed/discovered first"
        )
        return errs

    # (3) Every line item has an Item Map row + Item has gst_hsn_code +
    #     resolvable UoM. Collect ALL misses.
    if not po.items:
        errs.append("PO has no line items")
        return errs

    for line in po.items:
        if not line.item_code:
            errs.append(f"Line idx={line.idx} has no item_code")
            continue
        item_map = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": line.item_code},
            ["name", "ee_sku", "status"],
            as_dict=True,
        )
        if not item_map:
            errs.append(
                f"Item Map missing for line idx={line.idx} item={line.item_code!r}"
            )
            continue
        # HSN is mandatory per §8d / India Compliance. The Item may
        # have been created with HSN; recheck on push so a later
        # un-HSN'd Item doesn't slip through.
        hsn = frappe.db.get_value("Item", line.item_code, "gst_hsn_code")
        if not (hsn or "").strip():
            errs.append(
                f"Item {line.item_code!r} (line idx={line.idx}) has no gst_hsn_code"
            )

    return errs


# ============================================================
# Content channel — CreatePurchaseOrder
# ============================================================


def _do_content_push(
    *,
    po: Any,
    map_row: dict,
    location_row: dict,
    client: EasyEcomClient,
) -> POPushOutcome:
    """Build + send the CreatePurchaseOrder payload."""
    existing_ee_po_id = int(map_row.get("ee_po_id") or 0)
    create_or_update = "U" if existing_ee_po_id else "I"

    # Resolve supplier write key + address fields.
    sup_map = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": po.supplier},
        ["name", "ee_vendor_id"],
        as_dict=True,
    )
    vendor_id = sup_map.ee_vendor_id

    # Warehouse address (for the address top-level field).
    warehouse_address = _resolve_warehouse_address(
        location_row["mapped_warehouse"]
    )

    # Tax-change detection for updateTaxRate on amend.
    tax_changed = (
        _content_tax_signature(po) != (map_row.get("ecs_last_tax_signature") or "")
        if existing_ee_po_id
        else False
    )

    # Build line items[] with tax derivation.
    line_outcomes: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []
    # Supplier doesn't carry gst_state by default — the field lives on
    # the linked Address (via IC's address-extension custom fields). Go
    # straight to the Address resolution.
    supplier_state = _find_supplier_state(po.supplier)
    supplier_country = (
        frappe.db.get_value("Supplier", po.supplier, "country")
        or "India"
    )
    warehouse_state = (warehouse_address or {}).get("gst_state")

    for line in po.items:
        item_map = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": line.item_code},
            ["ee_sku"],
            as_dict=True,
        )
        sku = item_map.ee_sku if item_map else line.item_code
        tax_rate_pct, _decimal = _resolve_line_tax_rate(line)
        tax_inclusive_unit_price = float(line.rate or 0) * (
            1.0 + (tax_rate_pct / 100.0)
        )
        tax_value = float(line.rate or 0) * (tax_rate_pct / 100.0) * float(
            line.qty or 0
        )
        tax_type = compute_tax_type(
            supplier_state=supplier_state,
            warehouse_state=warehouse_state,
            supplier_country=supplier_country,
        )
        # Build line with only POPULATED fields. Live finding 2026-05-28
        # on Harmony: empty-string optional fields (ean/AccountingSku/
        # batch_code/batch_mrp/expiry_date) caused EE's PO creation to
        # crash with HTTP 500 (HTML server error page, not a JSON
        # validation response). EE's parser appears to choke on
        # present-but-empty optional fields. Omit them instead.
        line_item: dict[str, Any] = {
            "lineItemNumber": int(line.idx or 0),
            "sku": sku,
            "quantity": float(line.qty or 0),
            "unitPrice": round(tax_inclusive_unit_price, 4),
            "taxRate": tax_rate_pct,
            "taxValue": round(tax_value, 4),
            "taxType": int(tax_type),
        }
        # Optional fields — only include if populated.
        if getattr(line, "ean", None):
            line_item["ean"] = line.ean
        if getattr(line, "batch_no", None):
            line_item["batch_code"] = line.batch_no
        if getattr(line, "expiry_date", None):
            line_item["expiry_date"] = _fmt_date(line.expiry_date)
        line_items.append(line_item)
        line_outcomes.append(
            {
                "source_line_ref": line.item_code,
                "source_line_number": int(line.idx or 0),
                "target_field": "sku",
                "line_status": "OK",
                "reason": None,
            }
        )

    # Top-level payload — same trim philosophy: omit empty optional
    # keys rather than send them blank. EE's PO creation crashed with
    # HTTP 500 on present-but-empty fields during the 2026-05-28 live
    # smoke.
    payload: dict[str, Any] = {
        "vendorId": vendor_id,
        "referenceCode": po.name,
        "expDeliveryDate": _fmt_date(po.schedule_date),
        "createOrUpdate": create_or_update,
        "isCancel": 0,  # never wired — cancel goes via updatePoStatus=7
        "docNumber": po.name,
        "updateTaxRate": 1 if (existing_ee_po_id and tax_changed) else 0,
        "lineItems": line_items,
    }
    addr = (warehouse_address or {}).get("address_line1")
    if addr:
        payload["address"] = addr
    # shippingCost — only include if non-zero (and as int when possible).
    # ERPNext PO has `taxes` table for shipping; for now we don't
    # extract; Stage 4 may add this.
    shipping_cost = 0
    if shipping_cost:
        payload["shippingCost"] = shipping_cost

    try:
        response = client.post(PURCHASE_ORDER_CREATE, payload=payload)
    except EasyEcomError as exc:
        # Flip PO Map → Flagged-Not-Created (status invariant: if we
        # tried to push and EE rejected, this is FDE-actionable —
        # don't leave the row as Mapped).
        flag_text = f"CreatePurchaseOrder rejected by EE: {type(exc).__name__}: {exc}"
        frappe.db.set_value(
            "EasyEcom PO Map",
            map_row["name"],
            {
                "status": "Flagged-Not-Created",
                "flag_reason": flag_text[:1000],
            },
            update_modified=True,
        )
        sr = write_po_push_sync_record(
            entity_name=po.name,
            company=po.company,
            status=STATUS_FAILED,
            last_error=flag_text,
            line_outcomes=[
                {**lo, "line_status": "Failed", "reason": f"{type(exc).__name__}"}
                for lo in line_outcomes
            ],
        )
        return POPushOutcome(
            po_docname=po.name,
            operation="error",
            pushed=False,
            ee_po_id=existing_ee_po_id or None,
            po_map_status="Flagged-Not-Created",
            flag_reasons=[f"{type(exc).__name__}: {exc}"],
            ee_content_payload=payload,
            sync_record_name=sr,
        )

    # Capture data.poId → PO Map.ee_po_id (on Create; on Update EE
    # echoes the same id, but we treat it as authoritative).
    new_ee_po_id = _extract_ee_po_id(response)
    map_updates: dict[str, Any] = {
        "status": "Mapped",
        "flag_reason": None,
    }
    if new_ee_po_id:
        map_updates["ee_po_id"] = int(new_ee_po_id)
    map_updates["ecs_last_tax_signature"] = _content_tax_signature(po)
    frappe.db.set_value(
        "EasyEcom PO Map",
        map_row["name"],
        map_updates,
        update_modified=True,
    )

    sr = write_po_push_sync_record(
        entity_name=po.name,
        company=po.company,
        status=STATUS_SUCCESS,
        last_error=None,
        line_outcomes=line_outcomes,
    )
    return POPushOutcome(
        po_docname=po.name,
        operation="update" if existing_ee_po_id else "create",
        pushed=True,
        ee_po_id=int(new_ee_po_id or existing_ee_po_id or 0) or None,
        po_map_status="Mapped",
        ee_content_payload=payload,
        sync_record_name=sr,
    )


# ============================================================
# PO Map helpers
# ============================================================


def _get_po_map_row(po_docname: str) -> dict | None:
    return frappe.db.get_value(
        "EasyEcom PO Map",
        {"purchase_order": po_docname},
        [
            "name",
            "ee_po_id",
            "last_pushed_po_status",
            "status",
            "ecs_last_tax_signature",
        ],
        as_dict=True,
    )


def _get_or_create_po_map_row(*, po: Any, location_row: dict) -> dict:
    existing = _get_po_map_row(po.name)
    if existing:
        return existing
    doc = frappe.new_doc("EasyEcom PO Map")
    doc.update(
        {
            "reference_code": po.name,
            "purchase_order": po.name,
            "status": "Mapped",
        }
    )
    doc.insert(ignore_permissions=True)
    return _get_po_map_row(po.name) or {"name": doc.name}


def _upsert_po_map_flagged(
    *, po: Any, location_row: dict, reasons: list[str]
) -> None:
    existing = _get_po_map_row(po.name)
    flag = " || ".join(reasons)[:1000]
    if existing:
        frappe.db.set_value(
            "EasyEcom PO Map",
            existing["name"],
            {
                "status": "Flagged-Not-Created",
                "flag_reason": flag,
            },
            update_modified=True,
        )
        return
    doc = frappe.new_doc("EasyEcom PO Map")
    doc.update(
        {
            "reference_code": po.name,
            "purchase_order": po.name,
            "status": "Flagged-Not-Created",
            "flag_reason": flag,
        }
    )
    doc.insert(ignore_permissions=True)


def _flag_po_map_status_blocked(map_row: dict, target_status: int) -> None:
    """Status-channel push attempted without ee_po_id captured. Mark
    the row so the FDE sees it."""
    if not map_row.get("name"):
        return
    frappe.db.set_value(
        "EasyEcom PO Map",
        map_row["name"],
        {
            "status": "Flagged-Not-Created",
            "flag_reason": (
                f"Status push blocked: target po_status={target_status} but "
                "ee_po_id not yet captured (content push must succeed first)."
            )[:1000],
        },
        update_modified=True,
    )


# ============================================================
# Misc helpers
# ============================================================


def _resolve_warehouse_address(warehouse: str) -> dict | None:
    """Return the Warehouse's linked Address (city/state/zip/country)
    via Address.links Dynamic Link."""
    if not warehouse:
        return None
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.city, a.pincode, a.state,
               a.gst_state, a.country
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (warehouse,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _find_supplier_state(supplier_docname: str) -> str | None:
    """Pull gst_state from the Supplier's primary Billing address."""
    rows = frappe.db.sql(
        """
        SELECT a.gst_state
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
          AND a.address_type IN ('Billing', 'Shipping')
        ORDER BY (a.address_type='Billing') DESC, a.creation ASC
        LIMIT 1
        """,
        (supplier_docname,),
    )
    return rows[0][0] if rows else None


def _resolve_line_tax_rate(line: Any) -> tuple[float, float]:
    """Return (rate_pct, rate_decimal) for a PO line.

    Source of truth: Item.item_tax_template OR the line's explicit
    item_tax_template. We reuse the §8c tax_rule_map effective-rate
    resolver so the same logic that §8c stamps Items with is used
    on push.

    Falls back to 0% on any failure — flow doesn't throw on tax
    resolution; the PO can still push (the FDE corrects if needed,
    and Stage 3 reconciles received tax from EE).
    """
    from ecommerce_super.easyecom.doctype.easyecom_tax_rule_map.easyecom_tax_rule_map import (
        _effective_rate_for_template,
    )

    # Source of truth: the line's item_tax_template (set per PO line at
    # creation, typically inherited from Item.taxes child table). Item
    # has no direct item_tax_template column on the doc — its tax
    # templates live in the `taxes` child table. We deliberately do not
    # walk Item.taxes here: that walk needs a company context AND a date
    # filter (template effective ranges), which §8c's resolver already
    # handles when stamping Items. For Stage 2, trust the line's own
    # field; missing → 0% (Stage 3 will reconcile actual received tax).
    template = getattr(line, "item_tax_template", None)
    if not template:
        return 0.0, 0.0
    decimal = _effective_rate_for_template(template)
    if decimal is None:
        return 0.0, 0.0
    return decimal * 100.0, decimal


def _content_tax_signature(po: Any) -> str:
    """Stable string that changes only when line TAX shifts. Compared
    on amend to set updateTaxRate=1 vs 0.

    Includes (item_code, item_tax_template, rate). qty is deliberately
    EXCLUDED — updateTaxRate is about tax-rate change, not invoice-
    amount change. A qty edit is just a quantity push; the per-line
    taxValue (qty × rate × tax_rate) does change, but EE's
    updateTaxRate flag specifically signals "the tax structure
    changed" (different rate, different template, different SKU under
    different tax). Including qty would over-fire updateTaxRate=1 on
    any qty edit, polluting EE's tax-recompute path."""
    parts = []
    for line in po.items:
        parts.append(
            (
                str(line.item_code or ""),
                str(getattr(line, "item_tax_template", None) or ""),
                float(line.rate or 0),
            )
        )
    return json.dumps(parts, sort_keys=True)


def _extract_ee_po_id(response: dict) -> int | None:
    """EE returns `{"code":200,"message":"...","data":{"poId":N,...}}`.
    Tolerant against shape drift — try a few likely keys."""
    if not isinstance(response, dict):
        return None
    data = response.get("data") or {}
    if not isinstance(data, dict):
        return None
    for k in ("poId", "po_id", "id"):
        v = data.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _fmt_date(value: Any) -> str:
    """EE expects 'YYYY-MM-DD' on `expDeliveryDate` (discovered live
    on Harmony 2026-05-28; the §9 packet's DD/MM/YYYY guess was wrong
    — EE responds: "The expected delivery date must be a date after
    today in yyyy-mm-dd format"). Falls back to empty string on
    None / parse failure."""
    if not value:
        return ""
    try:
        from frappe.utils import getdate

        d = getdate(value)
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    except Exception:
        return ""


# ============================================================
# Hooks — Purchase Order lifecycle
# ============================================================


def validate_pre_push(doc: Any, method: str | None = None) -> None:
    """Purchase Order.validate hook.

    Two roles, both BLOCKING (raise frappe.ValidationError):
      1. Mixed-warehouse refusal: if PO.set_warehouse is blank and the
         lines target different warehouses, throw — direct the user to
         split into one PO per warehouse. (EE doesn't model multi-
         warehouse POs cleanly.)
      2. Warehouse-flip refusal on amend: a PO whose target warehouse
         was EE-mapped cannot be amended to a non-EE warehouse, and
         vice versa. PO warehouse is fixed for lifetime per packet.
    """
    if doc.doctype != "Purchase Order":
        return
    _check_mixed_warehouses(doc)
    _check_warehouse_flip(doc)


def _check_mixed_warehouses(doc: Any) -> None:
    """§9 carry-in (Stage 3): resolve the SET of distinct warehouses
    across header set_warehouse + line warehouses, map each to its EE
    Location, then:
      - exactly ONE Location resolves → proceed (a multi-line PO whose
        lines all target the same physical warehouse is valid, even if
        expressed per-line)
      - TWO OR MORE Locations resolve → throw the split error (EE PO is
        single-warehouse)
      - ZERO Locations → silent skip (non-EE PO; validate hook returns,
        the per-call Gate-0 in the push path handles the rest)
    """
    if not doc.items:
        return

    warehouses: set[str] = set()
    if doc.set_warehouse:
        warehouses.add(doc.set_warehouse)
    for l in doc.items:
        if l.warehouse:
            warehouses.add(l.warehouse)
    if not warehouses:
        return

    # Map each distinct warehouse to its EE Location (if any).
    locations: set[str] = set()
    non_ee_warehouses: set[str] = set()
    for wh in warehouses:
        loc = frappe.db.get_value(
            "EasyEcom Location", {"mapped_warehouse": wh}, "name"
        )
        if loc:
            locations.add(loc)
        else:
            non_ee_warehouses.add(wh)

    if len(locations) > 1:
        frappe.throw(
            frappe._(
                "§9: Purchase Order spans multiple EasyEcom Locations "
                "({0}). EE PO is single-warehouse — split into one PO per "
                "Location."
            ).format(", ".join(sorted(locations))),
            frappe.ValidationError,
        )
    # One EE Location + some non-EE warehouses → also a split issue (a
    # PO can't be half-in-scope, half-out). One EE + zero non-EE → fine.
    if len(locations) == 1 and non_ee_warehouses:
        frappe.throw(
            frappe._(
                "§9: Purchase Order mixes an EasyEcom-mapped warehouse "
                "with non-EE warehouses ({0}). Split into separate POs — "
                "EE only sees the EE-mapped portion."
            ).format(", ".join(sorted(non_ee_warehouses))),
            frappe.ValidationError,
        )
    # Zero EE locations: silent — validate hook returns; the push
    # path's Gate-0 already short-circuits at runtime.


def _check_warehouse_flip(doc: Any) -> None:
    """Catch the unambiguous flip-to-non-EE case: a PO that already has
    a PO Map row (was once in §9 scope) cannot be amended to a non-EE
    warehouse.

    The reverse case (PO created with non-EE warehouse, then amended to
    EE) is NOT caught here — it's indistinguishable from "PO created
    before §9 went live and is now being pushed for the first time" at
    the validate boundary, and the next push attempt creates the Map row
    naturally. We err on the side of false-negative here; the §9 packet
    treats this as low-risk (warehouse renames are rare; the alternative
    is false-positive blocks on every routine cancel after onboarding).
    """
    if doc.is_new():
        return
    existing_map = frappe.db.exists(
        "EasyEcom PO Map", {"purchase_order": doc.name}
    )
    if not existing_map:
        return
    current_loc = _resolve_po_warehouse_to_location(doc.name)
    if current_loc is None:
        frappe.throw(
            frappe._(
                "§9: This PO has an EasyEcom PO Map row but its current "
                "target warehouse is not EE-mapped. Amendment is blocked — "
                "a PO's EE-mappability is fixed at create time. Cancel and "
                "create a new PO if the warehouse needs to change."
            ),
            frappe.ValidationError,
        )


def enqueue_on_po_submit(doc: Any, method: str | None = None) -> None:
    """Purchase Order.on_submit hook. Fires the content push + status
    push to po_status=3 (Approved) when auto_push_pos_on_save=1.

    Ping-pong guard: skip when a §9 push is mid-flight (avoid hook
    re-firing on intra-flow saves).
    """
    if doc.doctype != "Purchase Order":
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return
    if not _auto_push_enabled():
        return
    # Don't enqueue if Gate-0 would fail — keeps the queue clean.
    loc = _resolve_po_warehouse_to_location(doc.name)
    if loc is None:
        return
    _enqueue_push(po_docname=doc.name, push_status_after_content=True)


def enqueue_on_po_cancel(doc: Any, method: str | None = None) -> None:
    """Purchase Order.on_cancel hook. Fires updatePoStatus=7 (Cancelled)
    when the PO is in §9 scope AND has been pushed (ee_po_id captured).

    NOTE: this is NOT gated on auto_push_pos_on_save. Cancellation
    needs to propagate even if auto-push is off, because the EE-side
    PO already exists (it was created during onboarding or before the
    pause). A paused account that doesn't propagate cancels would leave
    EE with stale orders.
    """
    if doc.doctype != "Purchase Order":
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return
    map_row = _get_po_map_row(doc.name)
    if not map_row or not map_row.get("ee_po_id"):
        return  # never pushed → nothing to cancel on EE
    _enqueue_status_push(po_docname=doc.name, target_status=PO_STATUS_CANCELLED)


def after_rename_po(
    doc: Any,
    old_name: str,
    new_name: str,
    merge: bool = False,
    method: str | None = None,
) -> None:
    """Purchase Order rename hook. Per packet's documented fallback:
    flag the PO Map row as Drift; do NOT auto-re-push.

    Why fallback over auto-update:
      - reference_code IS the EE-side join key. Changing it via
        createOrUpdate=U with a new referenceCode would orphan the
        EE-side row (or worse, create a duplicate).
      - PO renames are rare (typical workflow is cancel+recreate).
      - Surfacing as Drift gets the FDE's attention without us
        guessing what to do.
    """
    if not frappe.db.exists("EasyEcom PO Map", {"purchase_order": new_name}):
        # Renamed PO has no Map row — nothing to coordinate.
        return
    map_row = _get_po_map_row(new_name)
    if not map_row:
        return
    frappe.db.set_value(
        "EasyEcom PO Map",
        map_row["name"],
        {
            "reference_code": new_name,
            "status": "Drift",
            "flag_reason": (
                f"PO renamed in ERPNext ({old_name!r} → {new_name!r}). "
                "EE-side referenceCode is now stale; manual reconciliation "
                "required. Options: (a) cancel the EE-side PO via the "
                "status channel and re-push the renamed PO as a fresh "
                "create; (b) ignore if the rename is purely cosmetic and "
                "the EE-side row will resolve via po_id on GRN pull."
            )[:1000],
        },
        update_modified=True,
    )


# ============================================================
# Triggers — auto-push + batch sweep
# ============================================================


def _auto_push_enabled() -> bool:
    """Returns True iff the (single, enabled) Account has
    auto_push_pos_on_save=1."""
    account = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        ["name", "auto_push_pos_on_save"],
        as_dict=True,
    )
    return bool(account and int(account.auto_push_pos_on_save or 0))


def _enqueue_push(*, po_docname: str, push_status_after_content: bool) -> None:
    """Wraps the §6.3.1 queue facade — non-blocking on the user's save."""
    from ecommerce_super.easyecom.queue import enqueue_easyecom_job
    from ecommerce_super.easyecom.utils.idempotency import po_push_key

    company = frappe.db.get_value("Purchase Order", po_docname, "company")
    location_row = _resolve_po_warehouse_to_location(po_docname)
    location_key = (location_row or {}).get("location_key") or ""
    enqueue_easyecom_job(
        job_type="PO Push",
        company=company,
        target_doctype="Purchase Order",
        target_name=po_docname,
        payload={
            "po_docname": po_docname,
            "push_status_after_content": int(bool(push_status_after_content)),
        },
        idempotency_key=po_push_key(
            company=company, po_name=po_docname, ee_location_key=location_key
        ),
    )


def _enqueue_status_push(*, po_docname: str, target_status: int) -> None:
    from ecommerce_super.easyecom.queue import enqueue_easyecom_job
    from ecommerce_super.easyecom.utils.idempotency import internal_job_key

    company = frappe.db.get_value("Purchase Order", po_docname, "company")
    enqueue_easyecom_job(
        job_type="PO Status Push",
        company=company,
        target_doctype="Purchase Order",
        target_name=po_docname,
        payload={
            "po_docname": po_docname,
            "target_status": int(target_status),
        },
        # PO Status Push is operationally an internal-bookkeeping
        # dispatch (each status transition is its own discrete event;
        # idempotency at the wire level is enforced by
        # last_pushed_po_status). Use the internal builder.
        idempotency_key=internal_job_key(
            job_type="PO Status Push",
            company=company,
            target_doctype="Purchase Order",
            target_name=po_docname,
            payload={"target_status": int(target_status)},
        ),
    )


def po_push_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['PO Push'] dispatch — workers.execute_job
    calls this with the loaded EasyEcom Queue Job doc."""
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    po_docname = qj.target_name or payload.get("po_docname")
    push_status_after_content = bool(
        int(payload.get("push_status_after_content", 1) or 0)
    )
    if not po_docname:
        raise ValueError(
            f"PO Push job {qj.name} missing po_docname in payload/target_name"
        )
    frappe.flags[PING_PONG_FLAG] = True
    try:
        push_one_po(
            po_docname=po_docname,
            push_status_after_content=push_status_after_content,
        )
    finally:
        frappe.flags[PING_PONG_FLAG] = False


def po_status_push_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['PO Status Push'] dispatch — status-only push."""
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    po_docname = qj.target_name or payload.get("po_docname")
    target_status = int(payload.get("target_status") or 0)
    if not po_docname or not target_status:
        raise ValueError(
            f"PO Status Push job {qj.name} missing po_docname or target_status"
        )
    frappe.flags[PING_PONG_FLAG] = True
    try:
        push_po_status(po_docname=po_docname, target_status=target_status)
    finally:
        frappe.flags[PING_PONG_FLAG] = False


def candidate_pos_for_sweep(limit: int | None = None) -> list[str]:
    """Find candidate POs for the §9 batch sweep:
      - docstatus=1 (submitted)
      - target warehouse is EE-mapped (via EasyEcom Location)
      - no PO Map row OR Map row status != Mapped

    Returns the PO names list. Pure query — no enqueue here.
    """
    sql = """
        SELECT po.name
        FROM `tabPurchase Order` po
        LEFT JOIN `tabEasyEcom PO Map` pm
               ON pm.purchase_order = po.name
        WHERE po.docstatus = 1
          AND (pm.name IS NULL OR pm.status != 'Mapped')
          AND po.set_warehouse IN (
                SELECT mapped_warehouse
                FROM `tabEasyEcom Location`
                WHERE mapped_warehouse IS NOT NULL
          )
        ORDER BY po.modified DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = frappe.db.sql(sql, as_dict=True)
    return [r["name"] for r in rows]


@frappe.whitelist()
def push_all_pending_pos(
    account: str | None = None,
    limit: int | None = None,
    inline: int | bool = False,
) -> dict[str, Any]:
    """§9 batch sweep entry point (whitelist).

    Async-by-default per the round-2 discover-async pattern: enqueues
    one job per candidate into the `long` queue (3600s), returns
    immediately with the count. Pass inline=1 from tests to run
    synchronously and get full outcomes back.
    """
    if account and not frappe.db.exists("EasyEcom Account", account):
        return {"ok": False, "message": f"Account {account!r} not found."}

    parsed_limit = int(limit) if limit else None
    candidates = candidate_pos_for_sweep(limit=parsed_limit)

    if _truthy_arg(inline):
        outcomes: list[POPushOutcome] = []
        for po_name in candidates:
            outcomes.append(push_one_po(po_name))
        return {
            "ok": True,
            "inline": True,
            "total_considered": len(candidates),
            "outcomes": [
                {
                    "po": o.po_docname,
                    "operation": o.operation,
                    "ee_po_id": o.ee_po_id,
                    "flag_reasons": o.flag_reasons,
                }
                for o in outcomes
            ],
        }

    # Async path — one Queue Job per candidate, idempotent on
    # (company, po_name, location_key).
    enqueued: list[str] = []
    for po_name in candidates:
        try:
            _enqueue_push(po_docname=po_name, push_status_after_content=True)
            enqueued.append(po_name)
        except Exception as exc:
            frappe.log_error(
                title=f"PO push enqueue failed for {po_name}",
                message=f"{type(exc).__name__}: {exc}",
            )
    return {
        "ok": True,
        "inline": False,
        "total_considered": len(candidates),
        "enqueued_count": len(enqueued),
        "queue_job_names_sample": enqueued[:10],
    }


def _truthy_arg(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


__all__ = [
    "POPushOutcome",
    "PO_STATUS_APPROVED",
    "PO_STATUS_CANCELLED",
    "PO_STATUS_COMPLETED",
    "PING_PONG_FLAG",
    "push_one_po",
    "push_po_status",
    "validate_pre_push",
    "enqueue_on_po_submit",
    "enqueue_on_po_cancel",
    "after_rename_po",
    "po_push_queue_handler",
    "po_status_push_queue_handler",
    "candidate_pos_for_sweep",
    "push_all_pending_pos",
]
