"""§11.5.2 Mode 2 — Mirror EE-generated invoice into ERPNext Sales Invoice.

When EE generates the GST invoice (Mode 2 polling) or fires
/einvoice/update (Mode 1 Custom GSP), this module creates the
corresponding ERPNext Sales Invoice.

**Architecture (PR #226, 2026-07-16):**

Previously we hand-built the SI field-by-field from EE's response.
That produced a long tail of "we forgot to copy field X" bugs — gh#201
(item_wise_tax_detail), gh#206 (tax template), gh#214 (GST context),
gh#215 (gst_category still missing), the ongoing gst_treatment gap
observed on SO-2610407, and so on. Each miss cost live money.

The new mirror uses ERPNext's own `make_sales_invoice(so.name)` as
the primary construction primitive:

  1. `make_sales_invoice(source_so.name)` — ERPNext copies every SO
     field to the SI (customer, addresses, both GSTINs, tax_category,
     place_of_supply, gst_category, per-item item_tax_template,
     gst_treatment, item_tax_rate, etc.), maps each SI Item's
     `sales_order` + `so_detail` (natively wiring the Connections
     tab), and copies Sales Taxes and Charges rows.
  2. Override item qtys with EE's payload (per user decision — EE's
     `item_quantity` is the source of truth for what got invoiced).
     Drop items where EE qty = 0.
  3. Copy `payment_terms_template` from SO (ERPNext's
     make_sales_invoice deliberately EXCLUDES this in field_no_map;
     per user decision we WANT it flowed through).
  4. Override EE-specific fields (invoice_id, invoice_number,
     posting_date from EE, set_posting_time=1, update_stock=1).
  5. `si.insert()` — IC's chain runs on the fully-copied SO context,
     producing correct tax computation without any hand-picked fields.
  6. Variance check — SI grand_total (from SO) vs EE total_amount
     (from EE) — throw `InvoiceMirrorVariance` if divergence >
     `VARIANCE_THRESHOLD_PCT` (0.01% — tightened post-refactor from
     the historical 1% ceiling per user decision).
     Post-gh#218, gsp_handler catches + logs a Comment on Draft SI +
     surfaces HTTP error to EE. The SO wins for building; EE-side
     disagreement is a signal that surfaces loudly for human review.

**Idempotency**: SI lookup by `ecs_easyecom_invoice_id` unchanged.
Same EE invoice_id → same SI.

**IRN passthrough** — if EE sends `irn`/`ack_no`/`ack_dt` in the
payload (rare — usually only Mode 1 mints IRN), we mirror those to
the SI's India Compliance fields.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt


# Variance threshold. Per user decision (post-refactor): ANY non-trivial
# divergence between SI grand_total (from SO) and EE's total_amount must
# raise for human review — even a 1% delta is unacceptable, because at
# B2B invoice scale 1% is real money (₹4,800 SO × 1% = ₹48 gone). The
# tolerance below is set at 0.01% (1 basis point) — enough headroom for
# float / paise rounding, but any real divergence (any change to the
# rupee value) surfaces loudly.
VARIANCE_THRESHOLD_PCT = 0.01


class InvoiceMirrorError(Exception):
    """Raised when mirror cannot proceed (missing prerequisites)."""


class InvoiceMirrorVariance(Exception):
    """Raised when SI total vs EE total differs > VARIANCE_THRESHOLD_PCT.

    Post-gh#218 the gsp_handler catches this and:
      - Comments on the (Draft) SI explaining the variance
      - Raises GSPHandlerError so EE receives an HTTP error
      - Leaves the SI in Draft for FDE review

    The SI is not "wrong" per se — it reflects the SO faithfully. The
    variance is a signal that EE and our SO disagree about the money;
    a human decides whether to update the SO, update the SI, or ask
    EE to regenerate.
    """


def mirror_si_from_ee_response(
    *,
    map_doc: Any,
    ee_row: dict,
) -> dict[str, Any]:
    """Create or return existing Draft Sales Invoice from EE response row.

    Args:
        map_doc: EasyEcom B2B Order Map doc — already loaded.
        ee_row: One row from getOrderDetails.data OR the inbound
            /einvoice/update payload — carries EE's invoice fields
            (invoice_id, invoice_number, invoice_date, total_amount,
            order_items[], and possibly IRN fields).

    Returns:
        dict with keys:
          - sales_invoice: str (SI docname)
          - operation: "created" | "already_exists" | "adopted_legacy"
              adopted_legacy = the Map already pointed at an SI that
              lacked ecs_easyecom_invoice_id; we stamped it with this
              call's invoice_id and returned it (see gh#227 shim).
          - variance_pct: float
          - ee_total: float
          - si_total: float

    Raises:
        InvoiceMirrorError on missing prerequisites (no SO on Map,
            SO deleted, EE payload missing invoice_id).
        InvoiceMirrorVariance when SI grand_total diverges from EE's
            total_amount by more than VARIANCE_THRESHOLD_PCT.
    """
    ee_invoice_id = str(ee_row.get("invoice_id") or "").strip()
    if not ee_invoice_id:
        raise InvoiceMirrorError(
            "EE row has no invoice_id — cannot mirror SI."
        )

    # Idempotency — if an SI already carries this invoice_id, reuse it.
    existing = frappe.db.get_value(
        "Sales Invoice",
        {"ecs_easyecom_invoice_id": ee_invoice_id, "docstatus": ["!=", 2]},
        "name",
    )
    if existing:
        ee_total = float(ee_row.get("total_amount") or 0)
        si_total = float(frappe.db.get_value(
            "Sales Invoice", existing, "grand_total"
        ) or 0)
        return {
            "sales_invoice": existing,
            "operation": "already_exists",
            "variance_pct": _variance_pct(si_total, ee_total),
            "ee_total": ee_total,
            "si_total": si_total,
        }

    # Legacy adoption — the Map may point at an SI that was created
    # before we started stamping `ecs_easyecom_invoice_id` (or by a
    # manual FDE workflow). If we find one, adopt it: stamp with the
    # current invoice_id so future lookups hit the idempotency path
    # above, and return it as if it had always belonged to this
    # invoice_id. Prevents creating a duplicate SI on the first Mode 1
    # /einvoice/update or Mode 2 poll that lands after this shim ships.
    #
    # Fires at most ONCE per legacy SI (the stamp we write makes the
    # invoice_id lookup above hit next time). If MMPL has zero legacy
    # unstamped SIs (the expected case), this branch is dead weight
    # that never executes.
    legacy_si = (map_doc.get("sales_invoice") or "").strip()
    if legacy_si and frappe.db.exists("Sales Invoice", legacy_si):
        legacy_stamp = frappe.db.get_value(
            "Sales Invoice", legacy_si, "ecs_easyecom_invoice_id"
        )
        if not legacy_stamp:
            frappe.db.set_value(
                "Sales Invoice", legacy_si,
                "ecs_easyecom_invoice_id", ee_invoice_id,
                update_modified=False,
            )
            frappe.db.commit()
            ee_total = float(ee_row.get("total_amount") or 0)
            si_total = float(frappe.db.get_value(
                "Sales Invoice", legacy_si, "grand_total"
            ) or 0)
            return {
                "sales_invoice": legacy_si,
                "operation": "adopted_legacy",
                "variance_pct": _variance_pct(si_total, ee_total),
                "ee_total": ee_total,
                "si_total": si_total,
            }
        # If legacy_stamp is set AND differs from ee_invoice_id, this
        # is a new invoice for the same SO — fall through and create
        # a fresh SI. The Map.sales_invoice pointer will be overwritten
        # by the caller to the new SI (existing behavior); the old SI
        # remains discoverable via the SO ↔ SI Connections tab and via
        # its own ecs_easyecom_b2b_order_map back-ref.

    # Load the source SO — required. Every B2B Order Map has one in
    # the §11 flow; a Map without a sales_order is a corrupted state.
    if not (map_doc.sales_order or "").strip():
        raise InvoiceMirrorError(
            f"B2B Order Map {map_doc.name!r} has no sales_order — cannot "
            f"mirror without a source Sales Order."
        )
    if not frappe.db.exists("Sales Order", map_doc.sales_order):
        raise InvoiceMirrorError(
            f"Source Sales Order {map_doc.sales_order!r} not found — "
            f"cannot mirror SI for Map {map_doc.name!r}."
        )

    # --- Build the SI via ERPNext's native make_sales_invoice ---
    # This one call replaces ~200 lines of hand-copy logic. It brings
    # over EVERY field ERPNext knows how to map: customer, addresses,
    # both GSTINs, tax_category, place_of_supply, gst_category, all
    # item fields including item_tax_template + gst_treatment +
    # item_tax_rate + gst_hsn_code, plus the Sales Taxes and Charges
    # rows (values reset for recomputation, structure preserved).
    #
    # Each SI Item gets `sales_order = <SO.name>` and
    # `so_detail = <SO Item.name>` natively — this is what wires the
    # SO ↔ SI Connections tab (previously missing on hand-built SIs).
    from erpnext.selling.doctype.sales_order.sales_order import (
        make_sales_invoice,
    )

    si = make_sales_invoice(
        source_name=map_doc.sales_order,
        ignore_permissions=True,
    )

    # --- Override item qtys with EE's actual invoiced quantities ---
    # ERPNext's make_sales_invoice auto-computes qty = so.qty -
    # billed_qty - returned_qty, which is the right default for
    # "manual FDE clicks Get Items From SO." For the mirror we want
    # EE's authoritative per-line quantities instead — including
    # "this line wasn't in this invoice" (qty = 0 → drop the line).
    _apply_ee_qtys_and_drop_zero_lines(si, ee_row)

    # --- Copy payment_terms_template from SO ---
    # ERPNext's make_sales_invoice deliberately excludes this in its
    # `field_no_map` (line 1454 of sales_order.py). Per user decision
    # (PR #226, design Q4) we WANT the SO's payment terms flowed through to
    # the SI so the customer sees the same 90-day / net-30 / whatever
    # they agreed to on the SO. Override after the mapping.
    source_terms = frappe.db.get_value(
        "Sales Order", map_doc.sales_order, "payment_terms_template"
    )
    if source_terms:
        si.payment_terms_template = source_terms

    # --- EE-specific overrides ---
    si.posting_date = _parse_posting_date(ee_row)
    # set_posting_time=1 freezes posting_date across re-validates
    # (gh#161 v2). Without it, ERPNext resets posting_date to today on
    # every validate, breaking due_date invariants for pre-existing
    # Drafts.
    si.set_posting_time = 1
    # gh#160 — §11.5.1 Mode 1 is invoice-first. update_stock=1 makes
    # ERPNext write Stock Ledger entries inline so India Compliance's
    # e-invoicing validator doesn't refuse with "Delivery Note is
    # mandatory."
    si.update_stock = 1
    # Warehouse for stock movement — EE-derived (may be a different
    # warehouse than the SO's set_warehouse if EE routed fulfillment).
    warehouse = _resolve_warehouse(ee_row)
    if warehouse:
        si.set_warehouse = warehouse

    # Back-references — EE's identifiers land on our custom fields for
    # idempotency lookups + audit.
    si.ecs_easyecom_invoice_id = ee_invoice_id
    si.ecs_easyecom_invoice_number = (
        ee_row.get("invoice_number") or ""
    ).strip() or None
    si.ecs_easyecom_invoice_pdf_url = _resolve_pdf_url(ee_row)
    si.ecs_easyecom_b2b_order_map = map_doc.name

    # Defensive IRN capture — if EE has already minted an IRN and
    # sent it in the payload (rare — usually only Mode 1 fires mint),
    # mirror the fields IC would have populated. Prevents double-mint
    # attempts on the same NIC IRN.
    irn_fields = _extract_irn_fields(ee_row)
    for field, value in irn_fields.items():
        if value:
            setattr(si, field, value)

    si.flags.ignore_permissions = True
    si.insert()

    # --- Variance check ---
    ee_total = float(ee_row.get("total_amount") or 0)
    si_total = float(si.grand_total or 0)
    variance = _variance_pct(si_total, ee_total)

    result = {
        "sales_invoice": si.name,
        "operation": "created",
        "variance_pct": variance,
        "ee_total": ee_total,
        "si_total": si_total,
    }

    if abs(variance) > VARIANCE_THRESHOLD_PCT:
        raise InvoiceMirrorVariance(
            f"SI {si.name} total ₹{si_total:.2f} vs EE total "
            f"₹{ee_total:.2f} — {variance:+.2f}% variance exceeds "
            f"{VARIANCE_THRESHOLD_PCT}% threshold. Sales Invoice was "
            "created (in Draft) but flagged for FDE review. SO built "
            "the SI; EE-side numbers disagree — human decides which "
            "is correct."
        )

    return result


# ============================================================
# Helpers — EE-specific translation only. All hand-copy of SO fields
# is DELETED (make_sales_invoice does it correctly).
# ============================================================


def _apply_ee_qtys_and_drop_zero_lines(si: Any, ee_row: dict) -> None:
    """Override each SI item's qty with EE's per-line invoiced qty.
    Remove items where EE qty = 0 (they weren't in this invoice).

    EE's payload uses `sku` as the item identifier; we match against
    the SI item's `item_code` (which came from `make_sales_invoice`'s
    copy of the SO's item_code) via the EasyEcom Item Map lookup.

    If EE returns a line with an sku that no SI item maps to (very
    rare — implies EE invoiced an item not on the SO), we log and
    skip; SO wins.
    """
    order_items = ee_row.get("order_items")
    if not order_items or not isinstance(order_items, list):
        # No line-level override info from EE — trust msi's items
        # as-is (they came from the SO). Prevents "silently drop
        # everything" when EE payload lacks order_items.
        return

    # Build sku → qty map from EE payload. Multiple lines with same
    # sku sum (defensive against future EE shapes).
    ee_qty_by_sku: dict[str, int] = {}
    for line in order_items:
        sku = (line.get("sku") or "").strip()
        if not sku:
            continue
        try:
            qty = int(line.get("item_quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        ee_qty_by_sku[sku] = ee_qty_by_sku.get(sku, 0) + qty

    # Defensive: if we somehow ended up with an empty map (all lines
    # had no sku), don't drop anything. Better to keep msi's items
    # than to blank the SI.
    if not ee_qty_by_sku:
        return

    # Resolve each SI item's ERPNext item_code → EE sku via Item Map,
    # then look up the qty. Cache lookups to avoid N queries.
    item_code_to_sku: dict[str, str] = {}
    for it in list(si.items or []):
        code = it.item_code
        if code not in item_code_to_sku:
            item_code_to_sku[code] = (
                frappe.db.get_value(
                    "EasyEcom Item Map",
                    {"erpnext_name": code},
                    "ee_sku",
                ) or ""
            )
        sku = item_code_to_sku[code]

        # If we can't resolve to an EE sku, leave the SI item as-is
        # (from make_sales_invoice) — trust the SO. This is a
        # defensive path; typical B2B mirrors have every item mapped.
        if not sku:
            continue

        ee_qty = ee_qty_by_sku.get(sku)
        if ee_qty is None:
            # SO had this item but EE's invoice doesn't — drop it.
            si.items.remove(it)
            continue

        if ee_qty <= 0:
            # EE explicitly invoiced 0 of this item — drop it.
            si.items.remove(it)
            continue

        # Override qty with EE's authoritative value.
        it.qty = ee_qty
        # Amount recomputes when calculate_taxes_and_totals runs on
        # insert (rate stays from SO; amount = rate × qty).
        # Force amount refresh so any downstream reads see the new
        # value before insert triggers the full recompute.
        if getattr(it, "rate", None) is not None:
            it.amount = flt(it.rate) * flt(it.qty)

    # Re-index items after removals (idx should be sequential).
    for idx, it in enumerate(si.items or [], start=1):
        it.idx = idx


def _resolve_warehouse(ee_row: dict) -> str | None:
    """Find the ERPNext Warehouse mapped to EE's warehouse_id.

    Returns None if not resolved — mirror falls back to whatever
    make_sales_invoice inherited from the SO (source SO's set_warehouse).
    """
    ee_company_id = ee_row.get("warehouse_id") or ee_row.get(
        "assigned_warehouse_id"
    )
    if not ee_company_id:
        return None
    return frappe.db.get_value(
        "EasyEcom Location",
        {"ee_company_id": ee_company_id},
        "mapped_warehouse",
    )


def _resolve_pdf_url(ee_row: dict) -> str | None:
    """Extract PDF URL from EE's `documents` block."""
    docs = ee_row.get("documents") or ee_row.get("invoice_documents") or {}
    if isinstance(docs, dict):
        return (docs.get("easyecom_invoice") or "").strip() or None
    return None


def _parse_posting_date(ee_row: dict) -> str:
    """EE gives `invoice_date` as an ISO date string (YYYY-MM-DD)."""
    from frappe.utils import today
    return (ee_row.get("invoice_date") or today()).strip() or today()


def _variance_pct(si_total: float, ee_total: float) -> float:
    """Return signed % variance of SI relative to EE.
    Positive = SI higher than EE; negative = SI lower.
    Defensive against divide-by-zero (returns 0 when EE total is 0)."""
    if not ee_total:
        return 0.0
    return round(((si_total - ee_total) / ee_total) * 100, 4)


def _extract_irn_fields(ee_row: dict) -> dict[str, Any]:
    """Defensive IRN discovery. EE's Mode 2 responses typically do NOT
    carry IRN fields — Mode 1 (Custom GSP) mints on our side. But if
    a payload does include IRN, mirror it to IC's fields so we don't
    attempt a duplicate mint.

    Scans candidate names at the row level AND inside common nested
    blocks (documents, invoice_documents, meta, einvoice).

    Returns a dict of {ic_field_name: value} suitable for setattr on SI.
    """
    from typing import Iterable

    IRN_CANDIDATES = ("irn", "IRN", "einvoice_irn", "signed_irn")
    ACK_NO_CANDIDATES = (
        "ack_no", "ack_number", "acknowledgement_number", "einvoice_ack_no",
    )
    ACK_DT_CANDIDATES = (
        "ack_dt", "ack_date", "acknowledgement_date", "einvoice_ack_dt",
    )
    QR_CANDIDATES = ("signed_qr_code", "qr_code", "einvoice_qr", "irn_qr")

    NESTED_BLOCK_NAMES = (
        "documents", "invoice_documents", "meta", "einvoice", "irn_details",
    )

    def _scan(source: dict, keys: Iterable[str]) -> str | None:
        for k in keys:
            v = source.get(k)
            if v:
                return str(v).strip() or None
        return None

    scan_scopes: list[dict] = [ee_row]
    for nested_key in NESTED_BLOCK_NAMES:
        nested = ee_row.get(nested_key)
        if isinstance(nested, dict):
            scan_scopes.append(nested)

    result: dict[str, Any] = {}
    for scope in scan_scopes:
        if not result.get("irn"):
            found = _scan(scope, IRN_CANDIDATES)
            if found:
                result["irn"] = found
        if not result.get("ack_no"):
            found = _scan(scope, ACK_NO_CANDIDATES)
            if found:
                result["ack_no"] = found
        if not result.get("ack_dt"):
            found = _scan(scope, ACK_DT_CANDIDATES)
            if found:
                result["ack_dt"] = found
        if not result.get("signed_qr_code"):
            found = _scan(scope, QR_CANDIDATES)
            if found:
                result["signed_qr_code"] = found
    return result
