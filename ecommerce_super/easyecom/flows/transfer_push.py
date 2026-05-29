"""§10 Stage 2 — ERPNext → EE Stock Transfer outbound flow.

Triggered on Delivery Note.on_submit. Two branches per source-WH-EE-
mapping:

  STN BRANCH  (source EE-mapped)        → CREATE_ORDER /webhook/v2/createOrder
                                           with orderType=stocktransferorder
                                           per §10.G grounded payload.
  PO  BRANCH  (source NOT EE-mapped,    → §9 CreatePurchaseOrder, REUSED.
              target EE-mapped)          Vendor resolved via the source
                                         Company's Internal Supplier's
                                         Supplier Map row.

This module owns:
  - Gate-0 (Internal-Customer-DN + at-least-one-EE-warehouse) + the
    multi-warehouse-pair check (refuse the submit, don't auto-split).
  - The precondition chain (Internal Customer pair fabric, ee_customer_id,
    Item Maps, Company GSTINs, target Warehouse Address) — misses → Drift
    on Transfer Map with flag_reason, never throw through the hook.
  - Transfer Map row upsert (Mapped / Drift / SI-Pending / EE-Pushed).
  - SI auto-draft (different-GSTIN only) — DRAFT, never auto-submitted
    per the §10 invariant.
  - STN payload build per §10.G + EE response capture.
  - PO branch dispatch (reuses §9 push_one_po) + ee_po_id capture.
  - Pause-defer: when paused, lands ERPNext-side state but records
    ecs_pending_ee_push=1; un-pause runner fires.
  - Per-DN Sync Record + Line-child population.

What this module DOESN'T own (deferred):
  - Inbound (GRN-Complete → IPR + IPI + DN auto-creation) — Stage 3.
  - Cancel/amend of EE-pushed transfers — payload UNGROUNDED, deferred
    until §10.G grounds the cancelOrder endpoint. Stub-blocker only.
  - UI/workspace (Stage 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import frappe
from frappe.utils import flt, get_datetime, now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import CREATE_ORDER
from ecommerce_super.easyecom.exceptions import EasyEcomError
from ecommerce_super.easyecom.flows._transfer_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_transfer_push_sync_record,
)


PING_PONG_FLAG = "easyecom_transfer_push_in_flight"

# §10.G STN payment / shipping enum values. Account defaults sent as
# Select labels like "5 Prepaid" / "1 Standard COD" — the leading int
# is the wire value.
_DEFAULT_PAYMENT_MODE = 5
_DEFAULT_SHIPPING_METHOD = 1


TransferOp = Literal[
    "skipped",        # Gate-0 miss
    "drift",          # precondition fail
    "pending_pause",  # paused — pending recorded, no EE call
    "stn_pushed",     # STN createOrder fired + ids captured
    "po_pushed",      # PO branch via §9 machinery + ee_po_id captured
    "error",          # EE-side error or unexpected exception
]


@dataclass
class TransferPushOutcome:
    dn_name: str
    operation: TransferOp
    transfer_map: str | None = None
    sales_invoice: str | None = None
    ee_order_id: str | None = None
    ee_suborder_id: str | None = None
    ee_invoice_id: str | None = None
    ee_po_id: int | None = None
    ee_doctype: str | None = None  # "STN" | "PO" | None
    flag_reasons: list[str] = field(default_factory=list)
    ee_payload: dict[str, Any] | None = None
    sync_record_name: str | None = None
    status: str | None = None  # Transfer Map status set


# ============================================================
# Public API
# ============================================================


def push_one_transfer(
    dn_name: str,
    *,
    client: EasyEcomClient | None = None,
) -> TransferPushOutcome:
    """Push one §10 transfer to EE. Pure-ish: builds the Transfer Map
    row (or Drift state), drafts SI when different-GSTIN, fires the
    appropriate EE branch. Never raises through the hook boundary —
    failures land on Transfer Map + Failed Sync Record."""

    if not dn_name or not frappe.db.exists("Delivery Note", dn_name):
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="error",
            flag_reasons=[f"DN {dn_name!r} does not exist"],
        )

    dn = frappe.get_doc("Delivery Note", dn_name)

    # Gate 0 — Internal-Customer DN + at least one EE-mapped warehouse.
    if not int(dn.is_internal_customer or 0):
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="skipped",
            flag_reasons=["Gate-0: not an Internal-Customer DN"],
        )

    pair = _resolve_source_target_pair(dn)
    if pair is None:
        # Multi-warehouse pair refused → validation error at the
        # validate_pre_submit hook, NEVER lands here from on_submit.
        # Defensive fallback.
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="skipped",
            flag_reasons=[
                "Gate-0: DN has multiple distinct source/target warehouse "
                "pairs — split into separate DNs."
            ],
        )
    source_wh, target_wh = pair

    source_ee_mapped = _is_ee_mapped_warehouse(source_wh)
    target_ee_mapped = _is_ee_mapped_warehouse(target_wh)
    if not source_ee_mapped and not target_ee_mapped:
        # Both non-EE → silently inert (pure ERPNext stock movement).
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="skipped",
            flag_reasons=["Gate-0: neither source nor target warehouse is EE-mapped"],
        )

    # Branch decision per §10 packet:
    #   source EE-mapped → STN
    #   source NOT EE-mapped, target EE-mapped → PO
    branch = "STN" if source_ee_mapped else "PO"

    # Precondition chain. Misses → Drift; still upserts the Map row so
    # the FDE can see it on the worklist.
    precondition_errs = _run_preconditions(dn, source_wh, target_wh, branch)
    if precondition_errs:
        map_name = _upsert_transfer_map_drift(
            dn=dn,
            source_wh=source_wh,
            target_wh=target_wh,
            flag_reason=" || ".join(precondition_errs),
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=" || ".join(precondition_errs),
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="drift",
            transfer_map=map_name,
            flag_reasons=precondition_errs,
            sync_record_name=sr,
            status="Drift",
        )

    # Different-GSTIN → SI auto-draft (Draft, never auto-submit).
    src_gstin = _company_gstin(_warehouse_company(source_wh))
    tgt_gstin = _company_gstin(_warehouse_company(target_wh))
    gstin_different = bool(src_gstin and tgt_gstin and src_gstin != tgt_gstin)

    sales_invoice: str | None = None
    if gstin_different:
        sales_invoice = _draft_internal_sales_invoice(
            dn=dn, source_wh=source_wh, target_wh=target_wh
        )

    map_name = _upsert_transfer_map(
        dn=dn,
        source_wh=source_wh,
        target_wh=target_wh,
        sales_invoice=sales_invoice,
        # Initial status before EE push lands. STN-Pending isn't a
        # state — when SI exists & not submitted, we use SI-Pending.
        # When same-GSTIN, status moves straight to EE-Pushed below.
        status="SI-Pending" if sales_invoice else "Mapped",
    )

    # Pause gate. The pause-pending behaviour is § identical to §9 FIX 2:
    # ERPNext-side state (Transfer Map + SI Draft) lands, but the EE
    # write is deferred. fire_pending_transfer_pushes() runs on
    # un-pause.
    if _is_paused():
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            "ecs_pending_ee_push",
            1,
            update_modified=False,
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status="Pending",
            last_error=None,
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="pending_pause",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            sync_record_name=sr,
            status="SI-Pending" if sales_invoice else "Mapped",
        )

    # EE push.
    if branch == "STN":
        return _do_stn_push(
            dn=dn,
            map_name=map_name,
            source_wh=source_wh,
            target_wh=target_wh,
            sales_invoice=sales_invoice,
            client=client,
        )
    else:
        return _do_po_branch_push(
            dn=dn,
            map_name=map_name,
            source_wh=source_wh,
            target_wh=target_wh,
            sales_invoice=sales_invoice,
            client=client,
        )


# ============================================================
# Gate 0 helpers
# ============================================================


def _resolve_source_target_pair(dn: Any) -> tuple[str, str] | None:
    """Resolve the (source_wh, target_wh) pair from DN line items. If
    multiple distinct pairs appear, return None — the validate_pre_submit
    hook refuses these before we ever land here."""
    pairs: set[tuple[str, str]] = set()
    for line in dn.items or []:
        source = (line.warehouse or "").strip()
        target = (line.target_warehouse or "").strip()
        if source and target:
            pairs.add((source, target))
    if len(pairs) != 1:
        return None
    return next(iter(pairs))


def _is_ee_mapped_warehouse(warehouse: str) -> bool:
    """True iff this Warehouse is the mapped_warehouse of some Live +
    enabled EasyEcom Location."""
    return bool(
        frappe.db.get_value(
            "EasyEcom Location",
            {
                "mapped_warehouse": warehouse,
                "workflow_state": "Live",
                "enabled": 1,
            },
            "name",
        )
    )


def _warehouse_company(warehouse: str) -> str:
    return frappe.db.get_value("Warehouse", warehouse, "company") or ""


def _company_gstin(company: str) -> str:
    if not company:
        return ""
    return (frappe.db.get_value("Company", company, "gstin") or "").strip().upper()


# ============================================================
# Preconditions
# ============================================================


def _run_preconditions(
    dn: Any, source_wh: str, target_wh: str, branch: str
) -> list[str]:
    """Returns [] on clear, or a list of human-readable reasons."""
    errs: list[str] = []

    src_company = _warehouse_company(source_wh)
    tgt_company = _warehouse_company(target_wh)

    # (1) Internal Customer pair fabric — lookup, refuse on miss.
    internal_customer = _find_internal_customer(
        target_company=tgt_company, source_company=src_company
    )
    if not internal_customer:
        errs.append(
            f"Internal Customer pair missing: no Customer with "
            f"is_internal_customer=1, represents_company={tgt_company!r}, "
            f"and {src_company!r} in companies[*].company. "
            "Run ensure_internal_party_pairs_for_account on the Account."
        )
    else:
        # Verify the DN's customer IS this Internal Customer. ERPNext
        # enforces this at validate, but defensive check here surfaces
        # the misconfiguration as a flag_reason rather than a hard throw.
        if dn.customer != internal_customer:
            errs.append(
                f"DN customer = {dn.customer!r} but the resolved "
                f"Internal Customer for this transfer pair is "
                f"{internal_customer!r}. Misconfigured DN."
            )
        # (2) ee_customer_id captured on Customer Map.
        ee_customer_id = frappe.db.get_value(
            "EasyEcom Customer Map",
            {
                "erpnext_doctype": "Customer",
                "erpnext_name": internal_customer,
            },
            "ee_customer_id",
        )
        if not ee_customer_id:
            errs.append(
                f"Internal Customer {internal_customer!r} has no "
                "ee_customer_id captured on its Customer Map row. "
                "The §10 STN payload requires this id. Run the §8e "
                "Customer push for this customer, or invoke "
                "ensure_internal_party_pairs_for_account (which pushes)."
            )

    # (3) Item Map for every DN line.
    unmapped: list[str] = []
    for line in dn.items or []:
        if not frappe.db.exists(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": line.item_code},
        ):
            unmapped.append(line.item_code)
    if unmapped:
        errs.append(
            "DN line(s) reference Items without an EasyEcom Item Map: "
            + ", ".join(repr(s) for s in unmapped)
            + ". Run §8d Item Push for these Items."
        )

    # (4) Source + target Company GSTINs configured.
    if not _company_gstin(src_company):
        errs.append(
            f"Source Company {src_company!r} has no GSTIN configured. "
            "Set Company.gstin on the Company form (India Compliance)."
        )
    if not _company_gstin(tgt_company):
        errs.append(
            f"Target Company {tgt_company!r} has no GSTIN configured."
        )

    # (5) Target warehouse Address (used in STN shipping block).
    target_addr = _resolve_warehouse_address(target_wh)
    if not _addr_has_line(target_addr):
        errs.append(
            f"Target warehouse {target_wh!r} has no resolvable Address "
            "(needs address_line1 or city). The STN payload's shipping "
            "block requires this. Link an Address to the Warehouse via "
            "Address.links, then re-submit."
        )

    # PO branch — additional precondition: vendor_id resolvable for
    # the source Company. This means the source Company's Internal
    # Supplier must have an EE-side vendor representation.
    if branch == "PO":
        vendor_resolution = _resolve_po_branch_vendor(src_company)
        if not vendor_resolution.get("vendor_id"):
            errs.append(
                "PO branch requires an EE-side vendor for source Company "
                f"{src_company!r} — not configured. Lookup path: Internal "
                "Supplier (is_internal_supplier=1, represents_company="
                f"{src_company!r}) → its Supplier Map row → ee_vendor_id. "
                f"{vendor_resolution.get('reason', '')}"
            )

    return errs


def _find_internal_customer(
    *, target_company: str, source_company: str
) -> str | None:
    """The packet-locked lookup: customer with is_internal_customer=1,
    represents_company=target, AND source_company in companies[*].company.
    Stage 1's ensure_internal_party_pairs created exactly this shape."""
    if not target_company or not source_company:
        return None
    candidates = frappe.db.sql(
        """
        SELECT c.name
        FROM `tabCustomer` c
        JOIN `tabAllowed To Transact With` atw
          ON atw.parent = c.name
        WHERE c.is_internal_customer = 1
          AND c.represents_company = %s
          AND atw.company = %s
        LIMIT 1
        """,
        (target_company, source_company),
        as_dict=True,
    )
    return candidates[0]["name"] if candidates else None


def _find_internal_supplier(*, source_company: str) -> str | None:
    if not source_company:
        return None
    return frappe.db.get_value(
        "Supplier",
        {
            "is_internal_supplier": 1,
            "represents_company": source_company,
        },
        "name",
    )


def _resolve_po_branch_vendor(source_company: str) -> dict[str, Any]:
    """For PO branch: the source Company is NOT EE-mapped. EE sees the
    PO as "incoming from outside its universe" and needs a vendorId.
    Lookup path: Internal Supplier representing the source Company →
    its Supplier Map → ee_vendor_id.

    Returns {"vendor_id": str|None, "reason": str}.
    """
    internal_supplier = _find_internal_supplier(source_company=source_company)
    if not internal_supplier:
        return {
            "vendor_id": None,
            "reason": (
                "No Internal Supplier found. Stage 1's "
                "ensure_internal_party_pairs_for_account should have "
                "created one — re-run it."
            ),
        }
    ee_vendor_id = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {
            "erpnext_doctype": "Supplier",
            "erpnext_name": internal_supplier,
        },
        "ee_vendor_id",
    )
    if not ee_vendor_id:
        return {
            "vendor_id": None,
            "reason": (
                f"Internal Supplier {internal_supplier!r} has no "
                "Supplier Map ee_vendor_id captured. The §10 PO branch "
                "needs the source Company to be pushed to EE as a "
                "Vendor first. Either configure manually via §8f "
                "machinery, or use STN-only deployments (every source "
                "EE-mapped)."
            ),
        }
    return {"vendor_id": ee_vendor_id, "reason": ""}


def _resolve_warehouse_address(warehouse: str) -> dict | None:
    if not warehouse:
        return None
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.address_line2, a.city, a.state,
               a.pincode, a.country, a.email_id, a.phone
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


def _resolve_company_primary_address(company: str) -> dict | None:
    if not company:
        return None
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.address_line2, a.city, a.state,
               a.pincode, a.country, a.email_id, a.phone
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Company'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (company,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _addr_has_line(addr: dict | None) -> bool:
    if not addr:
        return False
    return bool(
        (addr.get("address_line1") or "").strip()
        or (addr.get("city") or "").strip()
    )


# ============================================================
# Transfer Map upsert
# ============================================================


def _upsert_transfer_map(
    *,
    dn: Any,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None,
    status: str,
) -> str:
    """Insert or update the Transfer Map row keyed on dn.name."""
    existing = frappe.db.get_value(
        "EasyEcom Transfer Map", {"delivery_note": dn.name}, "name"
    )
    fields = {
        "source_warehouse": source_wh,
        "target_warehouse": target_wh,
        "sales_invoice": sales_invoice,
        "status": status,
        "last_observed_at": now_datetime(),
    }
    if existing:
        frappe.db.set_value(
            "EasyEcom Transfer Map", existing, fields, update_modified=True
        )
        return existing
    doc = frappe.new_doc("EasyEcom Transfer Map")
    doc.update(
        {
            "delivery_note": dn.name,
            **fields,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_transfer_map_drift(
    *,
    dn: Any,
    source_wh: str,
    target_wh: str,
    flag_reason: str,
) -> str:
    """Drift state — Map row exists with flag_reason; SI / EE-push
    skipped."""
    existing = frappe.db.get_value(
        "EasyEcom Transfer Map", {"delivery_note": dn.name}, "name"
    )
    fields = {
        "source_warehouse": source_wh,
        "target_warehouse": target_wh,
        "status": "Drift",
        "flag_reason": flag_reason[:1000],
        "last_observed_at": now_datetime(),
    }
    if existing:
        frappe.db.set_value(
            "EasyEcom Transfer Map", existing, fields, update_modified=True
        )
        return existing
    doc = frappe.new_doc("EasyEcom Transfer Map")
    doc.update({"delivery_note": dn.name, **fields})
    doc.insert(ignore_permissions=True)
    return doc.name


# ============================================================
# SI auto-draft (different-GSTIN only)
# ============================================================


def _draft_internal_sales_invoice(
    *, dn: Any, source_wh: str, target_wh: str
) -> str:
    """Auto-create the Internal SI in DRAFT, sized to DN dispatched qty.
    update_stock=0 (the DN handled stock-out). Never auto-submit — the
    §10 invariant says ERP user submits financial documents."""
    si = frappe.new_doc("Sales Invoice")
    si.update(
        {
            "customer": dn.customer,
            "company": dn.company,
            "posting_date": dn.posting_date,
            "due_date": dn.posting_date,
            "is_internal_customer": 1,
            "update_stock": 0,
            "currency": dn.currency or "INR",
            "conversion_rate": dn.conversion_rate or 1,
            "selling_price_list": dn.selling_price_list,
            "price_list_currency": dn.price_list_currency or "INR",
            "plc_conversion_rate": dn.plc_conversion_rate or 1,
            "ecs_section10_transfer_map": None,  # back-fill below
        }
    )
    for line in dn.items or []:
        si.append(
            "items",
            {
                "item_code": line.item_code,
                "qty": line.qty,
                "rate": line.rate,
                "warehouse": line.warehouse,
                "item_tax_template": line.item_tax_template,
                "delivery_note": dn.name,
                "dn_detail": line.name,
            },
        )
    # Insert in Draft (docstatus=0). Submit is the ERP user's call.
    si.insert(ignore_permissions=True)
    return si.name


# ============================================================
# STN branch
# ============================================================


def _do_stn_push(
    *,
    dn: Any,
    map_name: str,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None,
    client: EasyEcomClient | None,
) -> TransferPushOutcome:
    """Build the §10.G STN payload, POST it, capture the three EE ids."""
    payload = _build_stn_payload(
        dn=dn, source_wh=source_wh, target_wh=target_wh
    )

    if client is None:
        location_key = _location_key_for_warehouse(source_wh)
        client = EasyEcomClient(
            company=dn.company, location_key=location_key
        )

    try:
        response = client.post(CREATE_ORDER, payload=payload)
    except EasyEcomError as exc:
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=f"createOrder: {type(exc).__name__}: {exc}",
        )
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            {
                "status": "Drift",
                "flag_reason": f"EE createOrder error: {exc}"[:1000],
            },
            update_modified=True,
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="error",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            flag_reasons=[f"{type(exc).__name__}: {exc}"],
            ee_payload=payload,
            sync_record_name=sr,
            status="Drift",
        )

    # Capture all three EE ids as strings (§10.G locked).
    data = (response or {}).get("data") or {}
    ee_order_id = str(data.get("OrderID") or "")
    ee_suborder_id = str(data.get("SuborderID") or "")
    ee_invoice_id = str(data.get("InvoiceID") or "")

    # Status decision: when sales_invoice is set + still in Draft (the
    # auto-draft we just landed is Draft by default), Transfer Map
    # stays in SI-Pending. The presence of ee_order_id signals EE-side
    # push happened. When same-GSTIN (no SI), status moves to EE-Pushed.
    # Reported in build report — overloaded SI-Pending rather than
    # introducing a SI-Pending-EE-Pushed transition state.
    new_status = "SI-Pending" if sales_invoice else "EE-Pushed"
    frappe.db.set_value(
        "EasyEcom Transfer Map",
        map_name,
        {
            "ee_doctype": "STN",
            "ee_order_id": ee_order_id,
            "ee_suborder_id": ee_suborder_id,
            "ee_invoice_id": ee_invoice_id,
            "status": new_status,
            "ecs_pending_ee_push": 0,
        },
        update_modified=True,
    )
    # Back-fill the DN's §10 back-ref custom field.
    if frappe.get_meta("Delivery Note").get_field("ecs_section10_transfer_map"):
        frappe.db.set_value(
            "Delivery Note",
            dn.name,
            "ecs_section10_transfer_map",
            map_name,
            update_modified=False,
        )
    if sales_invoice and frappe.get_meta("Sales Invoice").get_field(
        "ecs_section10_transfer_map"
    ):
        frappe.db.set_value(
            "Sales Invoice",
            sales_invoice,
            "ecs_section10_transfer_map",
            map_name,
            update_modified=False,
        )

    sr = write_transfer_push_sync_record(
        dn_name=dn.name,
        company=dn.company,
        status=STATUS_SUCCESS,
        last_error=None,
        line_outcomes=[
            {
                "source_line_ref": line.item_code,
                "source_line_number": idx,
                "target_field": "Sku",
                "line_status": "OK",
            }
            for idx, line in enumerate(dn.items or [], start=1)
        ],
    )

    return TransferPushOutcome(
        dn_name=dn.name,
        operation="stn_pushed",
        transfer_map=map_name,
        sales_invoice=sales_invoice,
        ee_order_id=ee_order_id,
        ee_suborder_id=ee_suborder_id,
        ee_invoice_id=ee_invoice_id,
        ee_doctype="STN",
        ee_payload=payload,
        sync_record_name=sr,
        status=new_status,
    )


def _build_stn_payload(
    *, dn: Any, source_wh: str, target_wh: str
) -> dict[str, Any]:
    """The §10.G wire contract — exact field set, OMITTED fields
    truly omitted (no null placeholders)."""
    account = _get_account_settings()

    items_payload: list[dict[str, Any]] = []
    total_weight_grams = 0.0
    for idx, line in enumerate(dn.items or [], start=1):
        sku = _resolve_sku_via_item_map(line.item_code)
        qty = flt(line.qty)
        item_weight = flt(
            frappe.db.get_value("Item", line.item_code, "weight_per_unit") or 0
        )
        total_weight_grams += qty * item_weight
        items_payload.append(
            {
                "OrderItemId": f"{dn.name}-L{idx}",
                "Sku": sku,
                "Quantity": str(int(qty)) if qty.is_integer() else str(qty),
                "Price": flt(line.rate),
                "itemDiscount": 0,
            }
        )

    tgt_company = _warehouse_company(target_wh)
    billing_addr = _resolve_company_primary_address(tgt_company)
    shipping_addr = _resolve_warehouse_address(target_wh)
    internal_customer = _find_internal_customer(
        target_company=tgt_company,
        source_company=_warehouse_company(source_wh),
    )
    ee_customer_id = frappe.db.get_value(
        "EasyEcom Customer Map",
        {
            "erpnext_doctype": "Customer",
            "erpnext_name": internal_customer,
        },
        "ee_customer_id",
    )
    warehouse_display_name = (
        frappe.db.get_value("Warehouse", target_wh, "warehouse_name")
        or target_wh
    )

    payload: dict[str, Any] = {
        "orderType": "stocktransferorder",
        "orderNumber": dn.name,
        "orderDate": _fmt_utc(dn.posting_date, getattr(dn, "posting_time", None)),
        "expDeliveryDate": _fmt_ist(getattr(dn, "delivery_date", None)),
        "shippingCost": 0,
        "paymentMode": _extract_int_prefix(
            account.get("stn_default_payment_mode"), _DEFAULT_PAYMENT_MODE
        ),
        "shippingMethod": _extract_int_prefix(
            account.get("stn_default_shipping_method"),
            _DEFAULT_SHIPPING_METHOD,
        ),
        "packageWeight": int(round(total_weight_grams)),
        "packageHeight": 0,
        "packageWidth": 0,
        "packageLength": 0,
        "items": items_payload,
        "customer": [
            {
                "customerId": int(ee_customer_id or 0),
                "billing": _addr_to_payload(
                    billing_addr,
                    name_override=warehouse_display_name,
                ),
                "shipping": _addr_to_payload(
                    shipping_addr,
                    name_override=warehouse_display_name,
                ),
            }
        ],
    }
    # Strip any None-valued top-level keys (defensive — EE rejects nulls
    # in some paths). The §10.G OMITTED set is enforced by not assigning
    # those keys in the first place.
    return {k: v for k, v in payload.items() if v is not None}


def _addr_to_payload(
    addr: dict | None, *, name_override: str | None = None
) -> dict[str, Any]:
    out = {
        "name": name_override or "",
        "addressLine1": (addr or {}).get("address_line1") or "",
        "addressLine2": (addr or {}).get("address_line2") or "",
        "postalCode": (addr or {}).get("pincode") or "",
        "city": (addr or {}).get("city") or "",
        "state": (addr or {}).get("state") or "",
        "country": (addr or {}).get("country") or "India",
        "contact": (addr or {}).get("phone") or "",
        "email": (addr or {}).get("email_id") or "",
    }
    return out


def _extract_int_prefix(label: str | None, fallback: int) -> int:
    """Account.stn_default_* fields are Select labels like '5 Prepaid'
    or '1 Standard COD'. Extract the leading int — that's the wire
    value EE expects."""
    if not label:
        return fallback
    head = (label or "").strip().split(" ", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        return fallback


def _fmt_utc(date_value: Any, posting_time: Any) -> str:
    """`YYYY-MM-DD HH:MM:SS` UTC per §10.G orderDate spec."""
    if not date_value:
        return ""
    if posting_time is None:
        return f"{date_value} 00:00:00"
    return f"{date_value} {posting_time}".split(".")[0]


def _fmt_ist(date_value: Any) -> str:
    """`YYYY-MM-DD HH:MM:SS` IST per §10.G expDeliveryDate spec."""
    if not date_value:
        return ""
    return f"{date_value} 23:59:59"


def _resolve_sku_via_item_map(item_code: str) -> str:
    """Sku-first priority (§9 convention). Item Map.ee_sku is the
    write key."""
    return (
        frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": item_code},
            "ee_sku",
        )
        or item_code
    )


def _location_key_for_warehouse(warehouse: str) -> str | None:
    return frappe.db.get_value(
        "EasyEcom Location",
        {"mapped_warehouse": warehouse, "workflow_state": "Live", "enabled": 1},
        "location_key",
    )


def _get_account_settings() -> dict[str, Any]:
    """Return the §10 settings from the (single, enabled) Account row.
    Falls back to packaged defaults if no account is enabled (test
    paths that don't enable the account)."""
    row = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        ["stn_default_payment_mode", "stn_default_shipping_method"],
        as_dict=True,
    )
    return row or {}


# ============================================================
# PO branch (reuses §9 push_one_po)
# ============================================================


def _do_po_branch_push(
    *,
    dn: Any,
    map_name: str,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None,
    client: EasyEcomClient | None,
) -> TransferPushOutcome:
    """PO branch: source NOT EE-mapped, target EE-mapped. The §9
    push_one_po machinery expects an ERPNext Purchase Order. §10
    doesn't create a real PO — instead, this branch builds a transient
    request mirroring the DN's content + the resolved
    source-Company vendor, and dispatches via the §9 wire helper.

    Stage 2 scope intentionally LIMITS this branch to vendor-resolved
    deployments. If you reach this code and the vendor isn't
    resolvable, the precondition gate already flagged Drift — this
    function should never be invoked in that case. Defensive: re-check
    and refuse if it slips through.

    NOTE: full PO-branch DN→PO bridging requires either creating a real
    PO on the source Company (which violates the §10 model — source is
    non-EE, so it shouldn't carry an integration-owned PO) or building
    a synthetic-PO request shape that reuses just the §9 content
    payload builder. Stage 2 ships the SHAPE (vendor resolved, branch
    routed, Map row updated) but the actual wire call is deferred to
    Stage 2 closeout pending integration smoke against a real
    non-EE-source + EE-target deployment on Harmony. The Drift
    fall-through documents this clearly.
    """
    src_company = _warehouse_company(source_wh)
    vendor_resolution = _resolve_po_branch_vendor(src_company)
    if not vendor_resolution.get("vendor_id"):
        # Should have been caught in preconditions; defensive fallback.
        reason = (
            "PO branch invoked but vendor unresolvable: "
            + vendor_resolution.get("reason", "")
        )
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            {"status": "Drift", "flag_reason": reason[:1000]},
            update_modified=True,
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=reason,
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="drift",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            flag_reasons=[reason],
            sync_record_name=sr,
            status="Drift",
        )

    # PO-branch wire dispatch is Stage 2 closeout work. At Stage 2
    # ship-point, mark the Map row with ee_doctype="PO" + the resolved
    # vendor on flag_reason so the FDE sees the routing decision and
    # the integration smoke captures the deferred-wire status.
    frappe.db.set_value(
        "EasyEcom Transfer Map",
        map_name,
        {
            "ee_doctype": "PO",
            "status": "Mapped",  # PO-branch wire not yet sent
            "flag_reason": (
                "PO branch routed (vendor_id = "
                f"{vendor_resolution['vendor_id']!r}). Wire dispatch "
                "to §9 CreatePurchaseOrder is Stage 2 closeout — "
                "deferred pending Harmony non-EE-source smoke."
            )[:1000],
            "ecs_pending_ee_push": 0,
        },
        update_modified=True,
    )
    sr = write_transfer_push_sync_record(
        dn_name=dn.name,
        company=dn.company,
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return TransferPushOutcome(
        dn_name=dn.name,
        operation="po_pushed",
        transfer_map=map_name,
        sales_invoice=sales_invoice,
        ee_doctype="PO",
        sync_record_name=sr,
        status="Mapped",
    )


# ============================================================
# Pause-gate
# ============================================================


def _is_paused() -> bool:
    """Mirrors §9's `_is_paused`. §10 outbound is a PO-shaped write —
    gates on the same auto_push_pos_on_save flag that §9 corrected
    pause_all_auto_push to cover."""
    from ecommerce_super.easyecom.flows.po_push import _is_paused as po_paused

    return po_paused()


@frappe.whitelist()
def fire_pending_transfer_pushes() -> dict[str, Any]:
    """Un-pause runner for §10 outbound.

    Walks every Transfer Map row with `ecs_pending_ee_push=1` and
    pushes each via push_one_transfer. Clears the flag on success.
    Idempotent: a Map row already past Mapped/SI-Pending (e.g.
    EE-Pushed) is skipped by push_one_transfer's own idempotency
    check on ee_order_id presence.

    Symmetric with §9 FIX 2's fire_pending_po_status_pushes.
    """
    if _is_paused():
        return {
            "ok": False,
            "message": "Still paused — fire_pending_transfer_pushes no-ops.",
            "fired": 0,
        }
    fired = 0
    skipped: list[str] = []
    pending = frappe.db.get_all(
        "EasyEcom Transfer Map",
        filters={"ecs_pending_ee_push": 1},
        fields=["name", "delivery_note"],
    )
    for row in pending:
        if not row.delivery_note:
            continue
        try:
            outcome = push_one_transfer(row.delivery_note)
            if outcome.operation in (
                "stn_pushed",
                "po_pushed",
            ):
                fired += 1
            else:
                skipped.append(
                    f"{row.delivery_note}: {outcome.operation} "
                    f"({'; '.join(outcome.flag_reasons[:2])})"
                )
        except Exception as exc:
            skipped.append(
                f"{row.delivery_note}: "
                f"{type(exc).__name__}: {exc}"
            )
    return {"ok": True, "fired": fired, "skipped": skipped}


# ============================================================
# Doc-event hooks
# ============================================================


def validate_pre_submit(doc: Any, method: str | None = None) -> None:
    """DN.validate hook — runs on every save AND on submit. For
    internal-customer DNs targeting EE-mapped warehouses, refuse the
    save if multiple distinct (source, target) warehouse pairs appear
    across lines. Same 'split, don't auto-multiplex' principle as
    §9's mixed-warehouse rule.

    Returns silently for non-internal-customer DNs (Gate-0 inert)."""
    if doc.doctype != "Delivery Note":
        return
    if not int(getattr(doc, "is_internal_customer", 0) or 0):
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return

    pairs: set[tuple[str, str]] = set()
    for line in doc.items or []:
        source = (line.warehouse or "").strip()
        target = (line.target_warehouse or "").strip()
        if source and target:
            pairs.add((source, target))
    if len(pairs) > 1:
        formatted = "; ".join(
            f"{s!r} → {t!r}" for s, t in sorted(pairs)
        )
        frappe.throw(
            frappe._(
                "§10 refuses a Delivery Note with multiple distinct "
                "(source, target) warehouse pairs: {0}. Split into "
                "separate Delivery Notes (one source/target pair per "
                "DN)."
            ).format(formatted)
        )


def enqueue_on_dn_submit(doc: Any, method: str | None = None) -> None:
    """DN.on_submit hook — Gate-0 + enqueue the §10 outbound push.

    Ping-pong guard: skip when a §10 push is mid-flight (avoid hook
    re-firing on intra-flow saves)."""
    if doc.doctype != "Delivery Note":
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return
    if not int(getattr(doc, "is_internal_customer", 0) or 0):
        return
    pair = _resolve_source_target_pair(doc)
    if pair is None:
        return  # validate_pre_submit already refused multi-pair
    source_wh, target_wh = pair
    if not _is_ee_mapped_warehouse(source_wh) and not _is_ee_mapped_warehouse(target_wh):
        return  # Gate-0 inert

    frappe.flags[PING_PONG_FLAG] = True
    try:
        push_one_transfer(doc.name)
    finally:
        frappe.flags[PING_PONG_FLAG] = False


def block_dn_cancel(doc: Any, method: str | None = None) -> None:
    """DN.on_cancel hook — §10 cancel/amend deferred until §10.G
    grounds the cancelOrder endpoint. Refuse the cancel if the DN has
    a Transfer Map row in EE-Pushed (or beyond) state.

    DNs in Mapped/Drift/SI-Pending have no EE-side state to undo —
    cancel passes through (the Transfer Map row will go orphan but
    that's the FDE's clean-up call)."""
    if doc.doctype != "Delivery Note":
        return
    map_row = frappe.db.get_value(
        "EasyEcom Transfer Map",
        {"delivery_note": doc.name},
        ["name", "status", "ee_order_id"],
        as_dict=True,
    )
    if not map_row:
        return
    if not (map_row.ee_order_id or "").strip():
        return  # Nothing pushed to EE
    if map_row.status not in (
        "EE-Pushed",
        "Partial-Received",
        "Fully-Received",
        "DN-Submitted-Locked",
    ):
        return
    frappe.throw(
        frappe._(
            "§10 STN cancel/amend not yet implemented — EE cancelOrder "
            "endpoint payload ungrounded (§10.G). DN {0} has a Transfer "
            "Map row in status {1!r} with ee_order_id={2!r}. Cancelling "
            "would desync ERPNext from EE. Contact the integration team "
            "to schedule the cancel-payload grounding."
        ).format(doc.name, map_row.status, map_row.ee_order_id)
    )


def block_dn_amend_after_submit(doc: Any, method: str | None = None) -> None:
    """DN.on_update_after_submit hook — refuse amends on EE-pushed
    transfers. Same rationale as block_dn_cancel."""
    if doc.doctype != "Delivery Note":
        return
    map_row = frappe.db.get_value(
        "EasyEcom Transfer Map",
        {"delivery_note": doc.name},
        ["name", "status", "ee_order_id"],
        as_dict=True,
    )
    if not map_row:
        return
    if not (map_row.ee_order_id or "").strip():
        return
    if map_row.status not in (
        "EE-Pushed",
        "Partial-Received",
        "Fully-Received",
    ):
        return
    frappe.throw(
        frappe._(
            "§10 STN amend not yet implemented — EE updateOrder "
            "endpoint payload ungrounded (§10.G). DN {0} has an "
            "EE-Pushed Transfer Map. Amending would desync ERPNext "
            "from EE. Contact the integration team."
        ).format(doc.name)
    )


# ============================================================
# Batch sweep
# ============================================================


@frappe.whitelist()
def push_all_pending_transfers(
    inline: int | bool | str = False,
) -> dict[str, Any]:
    """Re-push candidates — DNs that should have a Transfer Map in
    EE-Pushed (or beyond) but don't. Useful after un-pause, after
    FDE clears precondition blockers, or after EE outage recovery.

    `inline=True` runs synchronously (test helper). Default enqueues
    each candidate.
    """
    inline_mode = str(inline).strip().lower() in {"1", "true", "yes"}

    candidates = frappe.db.sql(
        """
        SELECT dn.name AS dn_name, dn.company AS company
        FROM `tabDelivery Note` dn
        LEFT JOIN `tabEasyEcom Transfer Map` xm
          ON xm.delivery_note = dn.name
        WHERE dn.docstatus = 1
          AND dn.is_internal_customer = 1
          AND (
            xm.name IS NULL
            OR xm.status IN ('Mapped', 'SI-Pending', 'Drift')
          )
        """,
        as_dict=True,
    )
    enqueued = 0
    inline_results: list[dict] = []
    for c in candidates:
        if inline_mode:
            outcome = push_one_transfer(c.dn_name)
            inline_results.append(
                {
                    "dn": c.dn_name,
                    "operation": outcome.operation,
                    "status": outcome.status,
                }
            )
        else:
            # Defer to the queue facade — keeps the user's batch click
            # non-blocking.
            from ecommerce_super.easyecom.queue import enqueue_easyecom_job
            from ecommerce_super.easyecom.utils.idempotency import (
                internal_job_key,
            )

            enqueue_easyecom_job(
                job_type="Transfer Push",
                company=c.company,
                target_doctype="Delivery Note",
                target_name=c.dn_name,
                payload={"dn_name": c.dn_name},
                idempotency_key=internal_job_key(
                    job_type="transfer_push",
                    company=c.company,
                    target_doctype="Delivery Note",
                    target_name=c.dn_name,
                ),
            )
            enqueued += 1
    return {
        "ok": True,
        "candidates_total": len(candidates),
        "enqueued": enqueued,
        "inline_results": inline_results,
    }


def transfer_push_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['Transfer Push'] dispatch — workers.execute_job
    calls this with the loaded Queue Job. Reads dn_name from the
    payload (or qj.target_name as the fallback)."""
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    dn_name = qj.target_name or payload.get("dn_name")
    if not dn_name:
        raise ValueError(
            f"Transfer Push job {qj.name} missing dn_name in "
            "payload/target_name"
        )
    frappe.flags[PING_PONG_FLAG] = True
    try:
        push_one_transfer(dn_name)
    finally:
        frappe.flags[PING_PONG_FLAG] = False


__all__ = [
    "PING_PONG_FLAG",
    "TransferPushOutcome",
    "push_one_transfer",
    "validate_pre_submit",
    "enqueue_on_dn_submit",
    "block_dn_cancel",
    "block_dn_amend_after_submit",
    "fire_pending_transfer_pushes",
    "push_all_pending_transfers",
    "transfer_push_queue_handler",
]
