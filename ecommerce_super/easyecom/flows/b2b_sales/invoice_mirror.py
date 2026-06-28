"""§11.5.2 Mode 2 — Mirror EE-generated invoice into ERPNext Sales Invoice.

When EE generates the GST invoice on its own side (via EE's own GSP
integration or marketplace invoicing), the polling response carries
the invoice data:

  invoice_number  — EE's GST invoice series number (e.g. "BMH1-2526-8")
  invoice_date    — when EE invoiced
  invoice_id      — EE's internal row identifier (always populated)
  documents.easyecom_invoice  — PDF URL
  breakup_types   — tax breakup (Item Amount Excluding Tax + CGST/SGST or IGST)
  order_items[]   — line items with sku, item_quantity, selling_price,
                    tax_rate, breakup_types per line

This module creates a corresponding ERPNext Sales Invoice in Draft.
ERP User reviews and submits. The 1% variance check between the
ERPNext-computed SI total and EE's total_amount raises a Discrepancy
when computed amounts diverge — usually a sign of Item Tax Template
configuration drift or different rounding rules.

**IRN handling — defensive**: as of 2026-06-28 probe against Thuraya
(SAL-ORD-2026-00023 + variants of include_einvoice/expand params),
the standard getOrderDetails response does NOT expose `irn` /
`ack_no` / `ack_dt` even after invoicing. BUT — the user flagged
that IRN might appear in some payloads. So we scan defensively:
`_extract_irn_fields` looks for candidate field names (irn, IRN,
ack_no, ack_number, einvoice_irn, signed_qr_code) at the row level
AND inside nested blocks (documents, invoice_documents, meta,
einvoice). If found, we write to the SI's India-Compliance fields
(irn, ack_no, ack_dt) — same fields India Compliance writes when
WE mint via Mode 1. If not found, SI is left without IRN — clients
needing IRN should use Mode 1 (Custom GSP, we mint via India
Compliance).

The function is pure-ish: reads from EE response + DB lookups
(Customer Map, Item Map, Item Tax Templates), writes one Sales
Invoice. Idempotent via the SI's ecs_easyecom_invoice_id field —
re-running with the same invoice_id returns the existing SI.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import getdate, today


# Variance threshold per packet. If ERPNext SI total differs from
# EE's total_amount by more than this, raise a Discrepancy instead
# of silently mirroring.
VARIANCE_THRESHOLD_PCT = 1.0


class InvoiceMirrorError(Exception):
    """Raised when mirror cannot proceed (missing prerequisites)."""


class InvoiceMirrorVariance(Exception):
    """Raised when SI total vs EE total differs > VARIANCE_THRESHOLD_PCT."""


def mirror_si_from_ee_response(
    *,
    map_doc: Any,
    ee_row: dict,
) -> dict[str, Any]:
    """Create or return existing Draft Sales Invoice from EE response row.

    Args:
        map_doc: EasyEcom B2B Order Map doc — already loaded.
        ee_row: One row from getOrderDetails.data — the businessorder
            row carrying invoice fields.

    Returns:
        dict with keys:
          - sales_invoice: str (SI docname)
          - operation: "created" | "already_exists"
          - variance_pct: float
          - ee_total: float
          - si_total: float

    Raises:
        InvoiceMirrorError on missing prerequisites (Customer Map,
            Item Map, HSN, etc.).
        InvoiceMirrorVariance when computed totals differ > 1%.
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

    # --- Resolve required prerequisites ---
    customer = _resolve_customer(ee_row)
    if not customer:
        raise InvoiceMirrorError(
            f"No EasyEcom Customer Map for ee_c_id "
            f"{ee_row.get('merchant_c_id')!r} — cannot resolve buyer."
        )

    line_items = _resolve_line_items(ee_row)

    company = map_doc.company or frappe.db.get_value(
        "Sales Order", map_doc.sales_order, "company"
    )
    if not company:
        raise InvoiceMirrorError(
            f"Cannot resolve Company for Map {map_doc.name}."
        )

    # --- Build the SI ---
    si = frappe.new_doc("Sales Invoice")
    si.customer = customer
    si.company = company
    si.currency = (ee_row.get("invoice_currency_code") or "INR").strip()
    si.posting_date = _parse_posting_date(ee_row)
    si.due_date = si.posting_date  # B2B can have terms, but default to posting
    si.set_warehouse = _resolve_warehouse(ee_row)

    # Back-references
    si.ecs_easyecom_invoice_id = ee_invoice_id
    si.ecs_easyecom_invoice_number = (
        ee_row.get("invoice_number") or ""
    ).strip() or None
    si.ecs_easyecom_invoice_pdf_url = _resolve_pdf_url(ee_row)
    si.ecs_easyecom_b2b_order_map = map_doc.name

    # Defensive IRN capture — see module docstring for context.
    # If EE returns irn/ack_no/ack_dt anywhere in the row, mirror
    # them to the same fields India Compliance writes when WE mint.
    irn_fields = _extract_irn_fields(ee_row)
    for field, value in irn_fields.items():
        if value:
            setattr(si, field, value)

    # Lines
    for line in line_items:
        si.append("items", line)

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
            "created (in Draft) but flagged for FDE review."
        )

    return result


# ============================================================
# Resolution helpers
# ============================================================


def _resolve_customer(ee_row: dict) -> str | None:
    """Find the ERPNext Customer via EasyEcom Customer Map.

    Lookup priority:
      1. EE Customer Map keyed on ee_c_id (= merchant_c_id from response)
      2. (Future: per-marketplace generic customer fallback for B2C)
    """
    ee_c_id = str(ee_row.get("merchant_c_id") or "").strip()
    if not ee_c_id:
        return None

    erpnext_name = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"ee_c_id": ee_c_id},
        "erpnext_name",
    )
    return erpnext_name


def _resolve_line_items(ee_row: dict) -> list[dict]:
    """Build the SI items child rows from EE order_items.

    Each line resolves:
      - item_code via EasyEcom Item Map (sku → erpnext_name)
      - qty from item_quantity
      - rate as per-unit NET price (taxable_value / qty)
      - gst_hsn_code from the resolved Item

    Raises InvoiceMirrorError if any line's SKU has no Item Map.
    """
    items = ee_row.get("order_items") or []
    if not items:
        raise InvoiceMirrorError(
            "EE row has no order_items — cannot mirror SI with zero lines."
        )

    out: list[dict] = []
    unmapped: list[str] = []
    for line in items:
        sku = (line.get("sku") or "").strip()
        if not sku:
            raise InvoiceMirrorError(
                f"EE order_items row missing sku: {line!r}"
            )

        item_code = frappe.db.get_value(
            "EasyEcom Item Map",
            {"ee_sku": sku},
            "erpnext_name",
        )
        if not item_code:
            unmapped.append(sku)
            continue

        qty = int(line.get("item_quantity") or 0)
        if qty <= 0:
            continue  # skip zero-qty rows

        # Per-unit net price from breakup_types (preferred over selling_price
        # which is the gross amount for the whole line).
        line_breakup = line.get("breakup_types") or {}
        taxable_value = float(
            line_breakup.get("Item Amount Excluding Tax") or 0
        )
        if taxable_value > 0:
            rate = round(taxable_value / qty, 2)
        else:
            # Fallback — derive from selling_price + tax_rate. Coarser
            # rounding but always lands a number.
            selling_price = float(line.get("selling_price") or 0)
            tax_rate = float(line.get("tax_rate") or 0)
            gross_per_unit = selling_price / qty if qty else 0
            rate = round(
                gross_per_unit / (1 + tax_rate / 100), 2
            ) if (1 + tax_rate / 100) else gross_per_unit

        hsn = frappe.db.get_value("Item", item_code, "gst_hsn_code")

        out.append({
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "gst_hsn_code": hsn,
        })

    if unmapped:
        raise InvoiceMirrorError(
            f"EE SKU(s) {unmapped!r} have no EasyEcom Item Map. "
            "Run §8d Item Push or §8d Item Pull for these SKUs first, "
            "then re-run mirror."
        )

    return out


def _resolve_warehouse(ee_row: dict) -> str | None:
    """Find the ERPNext Warehouse mapped to EE's warehouse_id.

    Returns None if not resolved — SI will use Company default.
    """
    ee_company_id = ee_row.get("warehouse_id") or ee_row.get(
        "assigned_warehouse_id"
    )
    if not ee_company_id:
        return None
    return frappe.db.get_value(
        "EasyEcom Location",
        {"ee_company_id": str(ee_company_id)},
        "mapped_warehouse",
    )


def _resolve_pdf_url(ee_row: dict) -> str | None:
    """Pull the EE invoice PDF URL from documents or invoice_documents."""
    for key in ("documents", "invoice_documents"):
        block = ee_row.get(key)
        if isinstance(block, dict) and block.get("easyecom_invoice"):
            return str(block["easyecom_invoice"])
    return None


def _parse_posting_date(ee_row: dict) -> str:
    """Parse EE's invoice_date. Falls back to today if absent/empty."""
    raw = (ee_row.get("invoice_date") or "").strip()
    if not raw:
        return today()
    try:
        return str(getdate(raw))
    except Exception:
        return today()


def _variance_pct(si_total: float, ee_total: float) -> float:
    """Compute (si_total - ee_total) / ee_total * 100. Signed."""
    if ee_total == 0:
        return 0.0
    return ((si_total - ee_total) / ee_total) * 100


# Defensive IRN capture — see module docstring.
# Map EE candidate field names → India Compliance SI field they
# should land on. Order matters: first match wins per IC field.
_IRN_FIELD_CANDIDATES: dict[str, list[str]] = {
    "irn": ["irn", "IRN", "Irn", "einvoice_irn", "e_invoice_irn", "ack_irn"],
    "ack_no": [
        "ack_no", "ack_number", "ack_num", "acknowledgement_number",
        "AckNo", "AckNum", "einvoice_ack_no",
    ],
    "ack_dt": [
        "ack_dt", "ack_date", "ack_datetime", "AckDt", "AckDate",
        "einvoice_ack_dt", "irn_date",
    ],
    "signed_qr_code": [
        "signed_qr_code", "qr_code", "irn_qr", "einvoice_qr",
        "signed_qr", "SignedQRCode",
    ],
}

# Nested blocks where EE might put e-invoice data
_IRN_NESTED_BLOCKS: list[str] = [
    "documents", "invoice_documents", "meta",
    "einvoice", "e_invoice", "irn_details",
]


def _extract_irn_fields(ee_row: dict) -> dict[str, Any]:
    """Defensive: scan ee_row for IRN/ack candidate fields.

    Returns dict of {si_field_name: value} for any matches found.
    Empty dict when EE doesn't return e-invoice data (the common
    case as of 2026-06-28 grounding).

    Scans:
      1. Top-level row fields (against _IRN_FIELD_CANDIDATES)
      2. Nested blocks (documents, invoice_documents, meta, etc.)
    """
    found: dict[str, Any] = {}

    def _scan_block(block: dict) -> None:
        for si_field, candidates in _IRN_FIELD_CANDIDATES.items():
            if si_field in found:
                continue  # already matched at a higher-priority level
            for candidate in candidates:
                if candidate in block and block[candidate]:
                    found[si_field] = block[candidate]
                    break

    # Top-level first (higher priority than nested)
    if isinstance(ee_row, dict):
        _scan_block(ee_row)

    # Nested blocks
    for block_key in _IRN_NESTED_BLOCKS:
        block = ee_row.get(block_key) if isinstance(ee_row, dict) else None
        if isinstance(block, dict):
            _scan_block(block)

    return found
