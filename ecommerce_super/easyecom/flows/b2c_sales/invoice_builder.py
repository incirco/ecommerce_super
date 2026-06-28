"""§12 — B2C marketplace SI builder.

Takes one EE order row (from the polling walker) and creates:
  1. A Sales Invoice in ERPNext (Draft) — per Path 2 (locked
     2026-06-29): EE-supplied tax in SI.taxes; ERPNext-computed tax
     stored separately as variance check
  2. An EasyEcom Marketplace Order Map row — the recon-engine join
     target for future Settlement Lines

If the ERPNext-computed tax check diverges from EE's tax by more
than 1%, raises an Integration Discrepancy as an upstream-issue
alert. SI data is NOT amended — the Discrepancy is informational.

Idempotency: caller (polling walker) already deduped on EE Invoice_id.
This function will fail loudly if a duplicate slips through —
`ecs_easyecom_invoice_id` is the SI unique key.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import getdate, now_datetime


class B2CBuilderError(Exception):
    """Raised when the builder cannot produce an SI for an EE order."""


def build_si_from_ee_order(
    *,
    order_row: dict,
    marketplace_account: Any,
    correlation_id: str,
) -> dict:
    """Create SI + Marketplace Order Map from one EE order row.

    Returns:
        {
          "sales_invoice": <docname>,
          "marketplace_order_map": <docname>,
          "tax_variance_pct": <float>,
          "discrepancy_raised": <bool>,
        }

    Raises B2CBuilderError on any unrecoverable failure (missing Item
    Map, missing pseudo-customer, missing tax account, etc.). Caller
    catches per-record so the batch continues.
    """
    # ---- 0. Sanity checks ----
    ee_invoice_id = str(order_row.get("invoice_id") or order_row.get("invoiceId") or "").strip()
    ee_order_id = str(order_row.get("order_id") or order_row.get("orderId") or "").strip()
    marketplace_order_id = (
        order_row.get("reference_code")
        or order_row.get("order_no")
        or ""
    )
    if not ee_invoice_id:
        raise B2CBuilderError("Order row missing invoice_id.")
    if not ee_order_id:
        raise B2CBuilderError(f"Order row {ee_invoice_id} missing order_id.")
    if not marketplace_order_id:
        raise B2CBuilderError(
            f"Order row {ee_invoice_id} missing reference_code / order_no "
            "— recon engine cannot join Settlement Lines without it."
        )

    if not marketplace_account.pseudo_customer:
        raise B2CBuilderError(
            f"Marketplace Account {marketplace_account.name} has no "
            "pseudo_customer. Resave the row to trigger bootstrap."
        )

    # ---- 1. Line items ----
    line_items = _resolve_line_items(order_row)

    # ---- 2. Warehouse ----
    warehouse = _resolve_warehouse(order_row, marketplace_account.company)

    # ---- 3. EE-supplied financials (the source of truth per Path 2) ----
    ee_grand_total = float(
        order_row.get("invoice_amount")
        or order_row.get("grand_total")
        or order_row.get("total")
        or 0
    )
    ee_tax_total = float(
        order_row.get("tax_amount")
        or order_row.get("total_tax")
        or 0
    )

    # ---- 4. ERPNext cross-check (Path 2 variance signal) ----
    erpnext_tax_check = _compute_erpnext_tax_check(line_items)

    # ---- 5. Build the SI ----
    posting_date = _resolve_posting_date(order_row)

    si_dict: dict[str, Any] = {
        "doctype": "Sales Invoice",
        "customer": marketplace_account.pseudo_customer,
        "company": marketplace_account.company,
        "posting_date": posting_date,
        "update_stock": 1,
        "items": [
            {
                "item_code": li["item_code"],
                "qty": li["qty"],
                "rate": li["rate"],
                "warehouse": warehouse,
                "gst_hsn_code": li.get("gst_hsn_code"),
            }
            for li in line_items
        ],
        # Custom Fields
        "ecs_marketplace": marketplace_account.marketplace,
        "ecs_marketplace_order_id": marketplace_order_id,
        "ecs_easyecom_order_id": ee_order_id,
        "ecs_easyecom_invoice_id": ee_invoice_id,
        "ecs_payment_mode": order_row.get("payment_mode") or order_row.get("paymentMode"),
        "ecs_awb_number": order_row.get("awb_number") or order_row.get("awbNumber"),
        "ecs_courier": order_row.get("courier") or order_row.get("courier_name"),
        "ecs_ee_invoice_total": ee_grand_total,
        "ecs_ee_invoice_tax_total": ee_tax_total,
        "ecs_erpnext_tax_check_total": erpnext_tax_check,
    }

    if warehouse:
        si_dict["set_warehouse"] = warehouse

    # EE-supplied tax → SI.taxes (a single 'Actual' row carrying EE's
    # total). Per Path 2: this is the GL truth, not ERPNext-derived.
    if ee_tax_total:
        tax_account = _resolve_default_sales_tax_account(marketplace_account.company)
        si_dict["taxes"] = [
            {
                "charge_type": "Actual",
                "account_head": tax_account,
                "tax_amount": ee_tax_total,
                "description": (
                    f"EE-supplied tax (Path 2 — source: marketplace adapter; "
                    f"see ecs_erpnext_tax_check_total for variance signal)"
                ),
            }
        ]

    si = frappe.get_doc(si_dict)
    si.flags.ignore_permissions = True
    si.insert()
    frappe.db.commit()

    # ---- 6. Marketplace Order Map ----
    mom = frappe.get_doc({
        "doctype": "EasyEcom Marketplace Order Map",
        "marketplace": marketplace_account.marketplace,
        "marketplace_account": marketplace_account.name,
        "sales_invoice": si.name,
        "ecs_easyecom_order_id": ee_order_id,
        "ecs_easyecom_invoice_id": ee_invoice_id,
        "ecs_marketplace_order_id": marketplace_order_id,
        "settlement_status": "Forecast",
        "ee_payload_hash": _hash_payload(order_row),
        "ee_payload": json.dumps(order_row, default=str)[:60000],
    })
    mom.flags.ignore_permissions = True
    mom.insert()
    frappe.db.commit()

    # ---- 7. Variance check (Path 2 alert mechanism) ----
    variance_outcome = _check_variance(
        si=si,
        marketplace_account=marketplace_account,
        ee_invoice_id=ee_invoice_id,
        ee_tax_total=ee_tax_total,
        erpnext_tax_check=erpnext_tax_check,
        correlation_id=correlation_id,
    )

    return {
        "sales_invoice": si.name,
        "marketplace_order_map": mom.name,
        "tax_variance_pct": variance_outcome["tax_variance_pct"],
        "discrepancy_raised": variance_outcome["discrepancy_raised"],
    }


# ============================================================
# Line item resolution
# ============================================================


def _resolve_line_items(order_row: dict) -> list[dict]:
    """Resolve EE order_items → list of (item_code, qty, rate, hsn).

    Reuses the §11.5.2 Item Map pattern: EE SKU → erpnext_name via
    EasyEcom Item Map. Raises B2CBuilderError listing all unmapped
    SKUs in one go (so the FDE fixes them in a single round-trip).
    """
    items = order_row.get("order_items") or order_row.get("orderItems") or []
    if not items:
        raise B2CBuilderError(
            "EE order row has no order_items — cannot build SI with zero lines."
        )

    out: list[dict] = []
    unmapped: list[str] = []

    for line in items:
        sku = (line.get("sku") or line.get("SKU") or "").strip()
        if not sku:
            raise B2CBuilderError(f"order_items row missing sku: {line!r}")

        item_code = frappe.db.get_value(
            "EasyEcom Item Map",
            {"ee_sku": sku},
            "erpnext_name",
        )
        if not item_code:
            unmapped.append(sku)
            continue

        qty = int(line.get("item_quantity") or line.get("quantity") or 0)
        if qty <= 0:
            continue  # skip zero-qty rows (cancelled lines)

        # Per-unit net price. EE breakup gives taxable_value; if absent,
        # fall back to selling_price minus tax.
        line_breakup = line.get("breakup_types") or {}
        taxable_value = float(line_breakup.get("Item Amount Excluding Tax") or 0)
        if taxable_value > 0:
            rate = round(taxable_value / qty, 2)
        else:
            selling_price = float(line.get("selling_price") or line.get("unit_price") or 0)
            tax_rate = float(line.get("tax_rate") or 0)
            gross_per_unit = selling_price / qty if qty else 0
            rate = (
                round(gross_per_unit / (1 + tax_rate / 100), 2)
                if (1 + tax_rate / 100) else gross_per_unit
            )

        hsn = frappe.db.get_value("Item", item_code, "gst_hsn_code")
        out.append({
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "gst_hsn_code": hsn,
        })

    if unmapped:
        raise B2CBuilderError(
            f"EE SKU(s) {unmapped!r} have no EasyEcom Item Map. "
            "Run §8d Item Push / Pull for these SKUs first, then re-poll."
        )
    if not out:
        raise B2CBuilderError(
            "All order_items had zero qty — nothing to invoice."
        )
    return out


# ============================================================
# Warehouse resolution
# ============================================================


def _resolve_warehouse(order_row: dict, company: str) -> str | None:
    """Resolve EE warehouse_id → ERPNext Warehouse via §8a Source-of-Truth
    Map. Returns None if not resolved — SI uses Company default."""
    ee_company_id = (
        order_row.get("warehouse_id")
        or order_row.get("assigned_warehouse_id")
        or order_row.get("warehouseId")
    )
    if not ee_company_id:
        return None

    ee_location = frappe.db.get_value(
        "EasyEcom Location",
        {"ee_company_id": str(ee_company_id)},
        "name",
    )
    if not ee_location:
        return None

    # §8a Source-of-Truth Map: EE Location → ERPNext Warehouse
    return frappe.db.get_value(
        "EasyEcom Source Of Truth Map",
        {
            "ee_location": ee_location,
            "company": company,
        },
        "erpnext_warehouse",
    )


# ============================================================
# Tax handling
# ============================================================


def _compute_erpnext_tax_check(line_items: list[dict]) -> float:
    """Approximate ERPNext-computed tax via the per-line HSN's default
    GST rate. v1 heuristic — looks up GST HSN Code's tax rate field if
    available; falls back to 0 if HSN unresolved (no variance signal
    rather than a false-positive alert).

    Path 2 variance check: this value is compared against EE's
    tax_total. >1% delta → Discrepancy. So coarse approximation is
    acceptable for v1 — sharpening the cross-check is a follow-up
    when real variance patterns surface.
    """
    total = 0.0
    for li in line_items:
        line_total = float(li.get("qty") or 0) * float(li.get("rate") or 0)
        hsn = li.get("gst_hsn_code")
        rate = _hsn_default_rate(hsn) if hsn else 0.0
        total += line_total * rate / 100.0
    return round(total, 2)


def _hsn_default_rate(hsn: str) -> float:
    """Best-effort lookup of the HSN's default GST rate. India Compliance
    exposes this via the GST HSN Code DocType with a rate field. If the
    field doesn't exist (older IC version) or HSN row absent, return 0
    so the variance check doesn't false-alert."""
    try:
        rate = frappe.db.get_value("GST HSN Code", hsn, "tax_rate")
    except Exception:
        return 0.0
    return float(rate) if rate is not None else 0.0


def _resolve_default_sales_tax_account(company: str) -> str:
    """Resolve the Company's default sales tax account for SI.taxes
    rows. Looks at Company.default_tax_account, falls back to a
    'Sales Taxes' account in the Company's CoA, raises if neither
    exists."""
    candidate = frappe.db.get_value("Company", company, "default_tax_account")
    if candidate:
        return candidate

    # Fall back to any account named like "Output Tax" or "Sales Taxes"
    # under the company.
    for label in ("Output Tax - ", "Sales Taxes - "):
        match = frappe.db.get_value(
            "Account",
            {"company": company, "name": ["like", f"{label}%"]},
            "name",
        )
        if match:
            return match

    raise B2CBuilderError(
        f"Company {company!r} has no default tax account configured. "
        "Set Company.default_tax_account OR create an 'Output Tax' / "
        "'Sales Taxes' account in the Chart of Accounts."
    )


# ============================================================
# Variance / Discrepancy
# ============================================================


def _check_variance(
    *,
    si: Any,
    marketplace_account: Any,
    ee_invoice_id: str,
    ee_tax_total: float,
    erpnext_tax_check: float,
    correlation_id: str,
) -> dict:
    """Path 2 variance check: compare EE-supplied tax against the
    ERPNext-computed cross-check. >1% delta raises an Integration
    Discrepancy as an upstream-issue alert.

    If erpnext_tax_check is 0 (we couldn't compute — HSN missing,
    GST HSN Code rows incomplete), we skip the alert — better than
    a false-positive flood on fresh installs.

    Returns:
        {"tax_variance_pct": <float>, "discrepancy_raised": <bool>}
    """
    if not erpnext_tax_check or not ee_tax_total:
        return {"tax_variance_pct": 0.0, "discrepancy_raised": False}

    variance_pct = abs(ee_tax_total - erpnext_tax_check) / ee_tax_total * 100
    if variance_pct <= 1.0:
        return {"tax_variance_pct": round(variance_pct, 2), "discrepancy_raised": False}

    # Variance > 1% — raise as upstream alert
    try:
        from ecommerce_super.easyecom.flows.grn_pull import _raise_discrepancy

        _raise_discrepancy(
            kind="B2C tax variance — EE vs ERPNext > 1%",
            reference_doctype="Sales Invoice",
            reference_name=si.name,
            company=si.company,
            reason=(
                f"§12 B2C SI build: EE-supplied tax {ee_tax_total:.2f} "
                f"vs ERPNext-computed tax {erpnext_tax_check:.2f} "
                f"(delta {variance_pct:.2f}%). EE invoice_id={ee_invoice_id}, "
                f"Marketplace Account {marketplace_account.name}, "
                f"correlation_id={correlation_id}. "
                f"\n\nThis is an UPSTREAM-ISSUE alert per Path 2 (locked "
                f"2026-06-29). SI carries EE's tax (the system that "
                f"generated the invoice = source of truth); the ERPNext "
                f"computation is a cross-check. SI data is immutable; "
                f"FDE investigates the root cause: HSN code on Items, "
                f"GST HSN Code default rate config, marketplace adapter "
                f"tax mapping, or composition / reverse-charge edge case."
            ),
        )
        return {"tax_variance_pct": round(variance_pct, 2), "discrepancy_raised": True}
    except Exception as exc:
        # Discrepancy raising must never break the SI creation flow.
        # Log it for the FDE and return.
        frappe.log_error(
            title=f"§12 variance Discrepancy raise failed for {si.name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"tax_variance_pct": round(variance_pct, 2), "discrepancy_raised": False}


# ============================================================
# Misc helpers
# ============================================================


def _resolve_posting_date(order_row: dict):
    """Posting date for the SI — prefer order_date, fall back to today."""
    raw = (
        order_row.get("order_date")
        or order_row.get("orderDate")
        or order_row.get("invoice_date")
        or order_row.get("invoiceDate")
    )
    if raw:
        try:
            return getdate(raw)
        except Exception:
            pass
    return getdate(now_datetime())


def _hash_payload(order_row: dict) -> str:
    """SHA-256 of the canonical-JSON order payload — for the Marketplace
    Order Map's audit hash."""
    canonical = json.dumps(order_row, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
