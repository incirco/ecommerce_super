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
    # gh#161 v2 (2026-07-13, SI-2603815 root cause): pin
    # set_posting_time=1 so ERPNext's set_posting_time_and_date()
    # doesn't reset posting_date to today on every validate call.
    # Without this flag, an SI created on day N and re-validated (via
    # submit / re-save) on day N+M would have posting_date jump forward
    # to day N+M while due_date stays at day N → validate refuses with
    # "Due Date cannot be before Posting Date". Observed on SI-2603815
    # created 2026-07-11 by initial mirror, submit re-attempted 2026-07-13.
    si.set_posting_time = 1
    # NOTE: do NOT set `si.transaction_date` here. Sales Invoice does
    # not have a native `transaction_date` field — that's a Sales Order
    # field. Setting it here is a silent no-op on sites without the
    # custom field, and on sites with it (via app copy-paste) creates
    # a shadow date that has zero effect on ERPNext's own date
    # validation. See gh#205 for the audit + removal. Standard ERPNext
    # primitive for freezing dates is `set_posting_time = 1` (above)
    # plus `payment_terms_template = ""` (below) — that's the whole
    # rule; no invented fields required.
    si.due_date = si.posting_date  # B2B can have terms, but default to posting
    # Clear any payment_terms_template that would reset payment_schedule
    # to a date before posting_date. Mirror is invoice-first from an
    # already-invoiced EE order; no terms apply.
    si.payment_terms_template = ""
    si.set_warehouse = _resolve_warehouse(ee_row)
    # gh#160: §11.5.1 Mode 1 is invoice-first — there is no separately-
    # tracked Delivery Note yet. Setting update_stock=1 tells ERPNext
    # to do the stock movement inline via Stock Ledger entries so
    # India Compliance's e-invoicing validator doesn't refuse with
    # "Delivery Note is mandatory for Item X" on stock items.
    si.update_stock = 1

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

    # gh#181 part 2 — populate SI.taxes from EE's per-item tax breakdown.
    # Pre-fix the taxes child table was left empty because we didn't set
    # a taxes_and_charges template — ERPNext then reported IGST 0% on
    # the SI even though the item had item_tax_template=GST5. Result:
    # SI grand_total = net_total, missing the actual tax amount.
    _append_taxes_from_ee_row(
        si, ee_row=ee_row, company=company
    )

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

    Lookup priority (gh#144):
      1. EE Customer Map keyed on ee_c_id      (= merchant_c_id / customer_code)
      2. EE Customer Map keyed on ee_customer_id (write-side alias — same
         value on EE, but historical rows may have only one populated)
      3. (Future: per-marketplace generic customer fallback for B2C)

    Payload may carry either `merchant_c_id` or `customer_code` (EE
    sends both for the same underlying id). Try both.
    """
    ee_c_id = str(
        ee_row.get("merchant_c_id")
        or ee_row.get("customer_code")
        or ""
    ).strip()
    if not ee_c_id:
        return None

    erpnext_name = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"ee_c_id": ee_c_id},
        "erpnext_name",
    )
    if erpnext_name:
        return erpnext_name
    # gh#144 fallback: pre-fix map rows have ee_c_id="flagged-<docname>"
    # placeholder and ee_customer_id=<real_id>. Try the write-side field
    # so the resolver survives until the backfill patch runs.
    return frappe.db.get_value(
        "EasyEcom Customer Map",
        {"ee_customer_id": ee_c_id},
        "erpnext_name",
    )


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

        # Per-unit net (post-promotion) price.
        #
        # gh#181: source priority — the item-level `taxable_value` field
        # is EE's authoritative post-promotion net amount. It already
        # nets out any `Promotion Discount` from the breakup. Using it
        # directly makes the SI match EE's grand_total on promo orders
        # (SO-2610392 was ₹0 on EE but ₹285.71 on our SI because we
        # were reading pre-discount `Item Amount Excluding Tax` alone).
        #
        # gh#207: tiers 2 and 3 below are defensive fallbacks added in
        # case EE ever omits `taxable_value`. In practice (as of 2026-07),
        # every observed EE response has included `taxable_value` on
        # every line. Both fallback tiers now emit an Error Log entry
        # when they fire so we can catch any live production hit and
        # either add a locked-behavior test for that shape or delete
        # the tier as dead code. See gh#207 for the audit plan.
        rate = None
        # 1. Preferred: EE's own post-promo net.
        if "taxable_value" in line and line.get("taxable_value") is not None:
            try:
                tv = float(line.get("taxable_value") or 0)
                rate = round(tv / qty, 2) if qty else 0.0
            except (TypeError, ValueError):
                rate = None
        # 2. Fallback: sum the breakup_types entries — Item Amount
        # Excluding Tax + Promotion Discount Excluding Tax (negative,
        # so subtracts). Handles any future discount categories the
        # same way — we just sum all "Excluding Tax" keys.
        if rate is None:
            line_breakup = line.get("breakup_types") or {}
            net = 0.0
            for k, v in line_breakup.items():
                if k.endswith("Excluding Tax"):
                    try:
                        net += float(v or 0)
                    except (TypeError, ValueError):
                        continue
            if net > 0:
                rate = round(net / qty, 2)
                # gh#207 instrumentation — silent fallback made loud.
                # If this fires in production, either (a) add a test
                # locking the observed payload shape or (b) delete the
                # tier as dead code. Cheap to log (Error Log is bounded
                # by Frappe's rotation), no user impact.
                frappe.log_error(
                    title=(
                        f"gh#207: invoice mirror fell back to tier 2 "
                        f"(breakup_types sum) for sku={sku!r} — EE "
                        f"payload lacked taxable_value"
                    ),
                    message=(
                        f"item_code={item_code!r}, qty={qty}, "
                        f"derived rate={rate}, line payload keys="
                        f"{sorted(line.keys())!r}, breakup_types="
                        f"{line_breakup!r}"
                    ),
                )
        # 3. Final fallback: derive from selling_price + tax_rate.
        # Coarser rounding but always lands a number when EE omits both
        # taxable_value and breakup_types.
        if rate is None:
            selling_price = float(line.get("selling_price") or 0)
            tax_rate = float(line.get("tax_rate") or 0)
            gross_per_unit = selling_price / qty if qty else 0
            rate = round(
                gross_per_unit / (1 + tax_rate / 100), 2
            ) if (1 + tax_rate / 100) else gross_per_unit
            # gh#207 instrumentation — same reasoning as tier 2 above.
            # This is the coarsest tier and most suspect if it ever
            # fires in production (rounds gross-to-net with less
            # precision than tier 1/2).
            frappe.log_error(
                title=(
                    f"gh#207: invoice mirror fell back to tier 3 "
                    f"(selling_price / (1 + tax_rate/100)) for "
                    f"sku={sku!r} — EE payload lacked both taxable_value "
                    f"AND breakup_types with 'Excluding Tax' entries"
                ),
                message=(
                    f"item_code={item_code!r}, qty={qty}, "
                    f"selling_price={selling_price}, tax_rate={tax_rate}, "
                    f"derived rate={rate}, line payload keys="
                    f"{sorted(line.keys())!r}"
                ),
            )

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


def _append_taxes_from_ee_row(si: Any, *, ee_row: dict, company: str) -> None:
    """gh#181 part 2 — populate SI.taxes from EE's per-item tax breakdown.

    Sums EE's per-item `igst` / `cgst` / `sgst` / `utgst` across all
    order_items, looks up the company's Output GST account heads via
    India Compliance's GST Settings, and appends one Sales Taxes and
    Charges row per non-zero tax bucket. Uses charge_type=\"Actual\"
    with the exact tax_amount from EE — no re-computation, no
    rounding drift.

    Skips silently when:
      - No GST Settings gst_accounts row exists for this company + Output
      - All tax buckets sum to zero
    """
    order_items = ee_row.get("order_items") or []
    if not isinstance(order_items, list) or not order_items:
        return

    taxable_total = 0.0
    tax_totals: dict[str, float] = {"igst": 0.0, "cgst": 0.0, "sgst": 0.0, "utgst": 0.0}
    for it in order_items:
        try:
            taxable_total += float(it.get("taxable_value") or 0)
            for k in tax_totals:
                tax_totals[k] += float(it.get(k) or 0)
        except (TypeError, ValueError):
            continue

    if all(v == 0 for v in tax_totals.values()):
        return  # zero-tax order — leave taxes empty

    gst_accounts = _lookup_output_gst_accounts(company)
    if not gst_accounts:
        # No account map on this company — surface via Comment on the
        # SI so FDE knows why taxes are missing; don't break the mirror.
        frappe.log_error(
            title=(
                f"gh#181: no Output GST accounts on GST Settings for "
                f"{company!r}; SI {si.get('name') or '(unsaved)'} left "
                "without tax rows."
            ),
            message=f"tax_totals: {tax_totals}",
        )
        return

    account_map = {
        "igst": gst_accounts.get("igst_account"),
        "cgst": gst_accounts.get("cgst_account"),
        "sgst": gst_accounts.get("sgst_account"),
        "utgst": gst_accounts.get("utgst_account"),
    }

    for bucket, amount in tax_totals.items():
        if amount <= 0:
            continue
        account_head = account_map.get(bucket)
        if not account_head:
            continue
        # Derive tax rate from EE's amounts. Using `On Net Total`
        # charge_type (not `Actual`) so:
        #   1. ERPNext auto-computes tax_amount = rate% * net_total
        #      → grand_total updates correctly on validate.
        #   2. Print formats read `rate` for display (e.g. "IGST @ 5%").
        #      `Actual` sets rate=0 internally so the print shows 0%.
        # On multi-item SOs with mixed rates this collapses to a
        # weighted average — acceptable for the mirror since the
        # per-item breakdown is preserved on each line's
        # item_tax_template.
        rate_pct = (
            round((amount / taxable_total) * 100, 2)
            if taxable_total > 0
            else 0
        )
        si.append("taxes", {
            "charge_type": "On Net Total",
            "account_head": account_head,
            "description": f"{bucket.upper()} @ {rate_pct}%",
            "rate": rate_pct,
            "included_in_print_rate": 0,
        })


def _lookup_output_gst_accounts(company: str) -> dict | None:
    """Return the {igst_account, cgst_account, sgst_account,
    utgst_account} row from GST Settings for this company's Output.

    gh#181 part 2 followup (2026-07-13): the previous version used
    `frappe.db.get_value("GST Account", {parent: ..., company: ..., ...})`
    which returned None on mmpl16 even though the row existed and matched
    all filters (verified by direct REST API query). Frappe's ORM query
    on a child DocType via dict filters is unreliable. Rewritten to
    load the parent GST Settings Single doc and iterate its child
    table — same result, no ORM quirk.
    """
    try:
        gst_settings = frappe.get_cached_doc("GST Settings")
    except Exception as exc:
        frappe.log_error(
            title=f"gh#181: GST Settings load failed for {company!r}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return None
    for row in gst_settings.get("gst_accounts") or []:
        if (
            getattr(row, "company", None) == company
            and getattr(row, "account_type", None) == "Output"
        ):
            return {
                "igst_account": getattr(row, "igst_account", None),
                "cgst_account": getattr(row, "cgst_account", None),
                "sgst_account": getattr(row, "sgst_account", None),
                "utgst_account": getattr(row, "utgst_account", None),
            }
    return None


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
