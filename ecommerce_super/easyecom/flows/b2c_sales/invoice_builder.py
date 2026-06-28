"""§12 — B2C marketplace SI builder.

Takes one EE order row (from the polling walker) and creates:
  1. A Sales Invoice in ERPNext (Draft) — per Path 2 (locked
     2026-06-29): EE-supplied tax in SI.taxes; ERPNext-computed tax
     stored separately as variance check
  2. An EasyEcom Sync Record (direction=Pull, entity=SI) — the audit
     trail carrying the EE payload (replaces the original Marketplace
     Order Map DocType; recon engine reads settlement state directly
     from the SI's Custom Fields)

If the ERPNext-computed tax check diverges from EE's tax by more
than 1%, raises an Integration Discrepancy as an upstream-issue
alert. SI data is NOT amended — the Discrepancy is informational.

Customer resolution: the Marketplace Account holds TWO pool Customers
(in-state + out-of-state). The builder resolves the buyer's shipping
state vs the Company's state and picks the appropriate pool so the
SI's tax_category drives the correct GST split (CGST+SGST vs IGST).

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
    """Create SI + Sync Record audit trail from one EE order row.

    Returns:
        {
          "sales_invoice": <docname>,
          "sync_record": <docname>,
          "customer_pool_used": "in_state" | "out_of_state",
          "tax_variance_pct": <float>,
          "discrepancy_raised": <bool>,
        }

    Raises B2CBuilderError on any unrecoverable failure (missing Item
    Map, missing pool customer, missing tax account, etc.). Caller
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

    # ---- 1. Pool customer resolution (in-state vs out-of-state) ----
    pool_choice = _resolve_pool_customer(
        marketplace_account=marketplace_account,
        order_row=order_row,
    )

    # ---- 2. Line items ----
    line_items = _resolve_line_items(order_row)

    # ---- 3. Warehouse ----
    warehouse = _resolve_warehouse(order_row, marketplace_account.company)

    # ---- 4. EE-supplied financials (the source of truth per Path 2) ----
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

    # ---- 5. ERPNext cross-check (Path 2 variance signal) ----
    erpnext_tax_check = _compute_erpnext_tax_check(line_items)

    # ---- 6. Build the SI ----
    posting_date = _resolve_posting_date(order_row)

    si_dict: dict[str, Any] = {
        "doctype": "Sales Invoice",
        "customer": pool_choice["customer"],
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
        # Custom Fields (recon source-of-truth values)
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
        # Settlement lifecycle — recon engine mutates these post-insert
        "ecs_settlement_status": "Forecast",
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

    # ---- 7. Sync Record — audit trail (replaces Marketplace Order Map) ----
    sync_record_name = _write_sync_record(
        si=si,
        marketplace_account=marketplace_account,
        order_row=order_row,
        ee_invoice_id=ee_invoice_id,
        correlation_id=correlation_id,
    )

    # ---- 8. Variance checks (Path 2 alert mechanism) ----
    tax_variance = _check_variance(
        si=si,
        marketplace_account=marketplace_account,
        ee_invoice_id=ee_invoice_id,
        ee_tax_total=ee_tax_total,
        erpnext_tax_check=erpnext_tax_check,
        correlation_id=correlation_id,
    )

    # §12.9 line 2821: 1-paisa total variance check.
    # EE order amount vs ERPNext SI.grand_total — must match within
    # 1 paisa or raises a Discrepancy. Independent of the tax check
    # (catches discount mishandling, missing line items, rounding bugs).
    total_variance = _check_total_variance(
        si=si,
        marketplace_account=marketplace_account,
        ee_invoice_id=ee_invoice_id,
        ee_grand_total=ee_grand_total,
        correlation_id=correlation_id,
    )

    return {
        "sales_invoice": si.name,
        "sync_record": sync_record_name,
        "customer_pool_used": pool_choice["kind"],
        "tax_variance_pct": tax_variance["tax_variance_pct"],
        "tax_discrepancy_raised": tax_variance["discrepancy_raised"],
        "total_variance_paise": total_variance["total_variance_paise"],
        "total_discrepancy_raised": total_variance["discrepancy_raised"],
        # Back-compat: any variance => discrepancy_raised True
        "discrepancy_raised": (
            tax_variance["discrepancy_raised"]
            or total_variance["discrepancy_raised"]
        ),
    }


# ============================================================
# Pool customer resolution (in-state vs out-of-state)
# ============================================================


def _resolve_pool_customer(
    *,
    marketplace_account: Any,
    order_row: dict,
) -> dict:
    """Pick the right pool Customer based on shipping address state
    vs Company's state.

    Returns: {"customer": <docname>, "kind": "in_state" | "out_of_state"}

    Raises B2CBuilderError if neither pool customer is configured on
    the Marketplace Account (bootstrap failed at insert + FDE hasn't
    fixed it manually).
    """
    in_state_customer = marketplace_account.get("pseudo_customer_in_state")
    out_of_state_customer = marketplace_account.get("pseudo_customer_out_of_state")

    if not in_state_customer and not out_of_state_customer:
        raise B2CBuilderError(
            f"Marketplace Account {marketplace_account.name} has no pool "
            "customers configured. Resave the row to trigger bootstrap."
        )

    company_state = _resolve_company_state(marketplace_account.company)
    shipping_state = _resolve_shipping_state(order_row)

    # If we can't determine either side, default to in-state pool
    # (safer GST-wise — over-charges CGST+SGST and the recon variance
    # surfaces, vs under-charging IGST silently).
    if not company_state or not shipping_state:
        customer = in_state_customer or out_of_state_customer
        kind = "in_state" if customer == in_state_customer else "out_of_state"
        return {"customer": customer, "kind": kind}

    if _normalise_state(company_state) == _normalise_state(shipping_state):
        if not in_state_customer:
            raise B2CBuilderError(
                f"Order ships to in-state ({shipping_state}) but "
                f"Marketplace Account {marketplace_account.name} has no "
                "pseudo_customer_in_state."
            )
        return {"customer": in_state_customer, "kind": "in_state"}

    if not out_of_state_customer:
        raise B2CBuilderError(
            f"Order ships out-of-state ({shipping_state}, company is "
            f"{company_state}) but Marketplace Account "
            f"{marketplace_account.name} has no pseudo_customer_out_of_state."
        )
    return {"customer": out_of_state_customer, "kind": "out_of_state"}


def _resolve_company_state(company: str) -> str | None:
    """Get the Company's state. Tries Company.state field first; falls
    back to deriving from the Company's GSTIN (first 2 chars → state
    code → state name via India Compliance's state list)."""
    try:
        state = frappe.db.get_value("Company", company, "state")
    except Exception:
        return None
    if state:
        return state

    try:
        gstin = frappe.db.get_value("Company", company, "gstin")
    except Exception:
        return None
    if not gstin or len(gstin) < 2:
        return None

    # GSTIN state code → state name. India Compliance ships this map;
    # we read it via the Address DocType's gst_state field convention.
    state_code = gstin[:2]
    return _gstin_state_code_to_name(state_code)


def _resolve_shipping_state(order_row: dict) -> str | None:
    """Extract the buyer's shipping address state from the EE payload.

    EE payloads vary on key names; scan plausible shapes. Returns None
    if no state can be resolved (caller defaults to in-state pool).
    """
    # Common: order_row.shipping_address as a nested dict
    shipping = order_row.get("shipping_address") or order_row.get("shippingAddress")
    if isinstance(shipping, dict):
        for key in ("state", "state_name", "shipping_state", "stateName"):
            if shipping.get(key):
                return str(shipping[key]).strip()

    # Flat fields at order_row top level
    for key in (
        "shipping_state", "ship_state", "buyer_state",
        "customer_state", "state_name",
    ):
        if order_row.get(key):
            return str(order_row[key]).strip()

    return None


def _normalise_state(state: str) -> str:
    """Case- and whitespace-insensitive state comparison."""
    return (state or "").strip().lower()


def _gstin_state_code_to_name(code: str) -> str | None:
    """Minimal GSTIN state-code → state-name lookup. Covers the
    Indian-state GSTIN prefixes; returns None for unknown codes."""
    # GSTIN state codes per Income Tax / GST Council assignment.
    mapping = {
        "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
        "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
        "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
        "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
        "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
        "16": "Tripura", "17": "Meghalaya", "18": "Assam",
        "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
        "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
        "25": "Daman and Diu", "26": "Dadra and Nagar Haveli",
        "27": "Maharashtra", "28": "Andhra Pradesh", "29": "Karnataka",
        "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
        "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands",
        "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
        "97": "Other Territory", "99": "Centre Jurisdiction",
    }
    return mapping.get(code)


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
# Sync Record (audit trail — replaces Marketplace Order Map)
# ============================================================


def _write_sync_record(
    *,
    si: Any,
    marketplace_account: Any,
    order_row: dict,
    ee_invoice_id: str,
    correlation_id: str,
) -> str | None:
    """Write the §6/§7 Sync Record for this polled order — captures
    the EE payload + hash for audit / replay. Never raises (audit
    failure must not break the SI creation).
    """
    try:
        payload_canonical = json.dumps(order_row, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_canonical.encode("utf-8")).hexdigest()
        idempotency_key = f"§12-b2c-pull:{marketplace_account.name}:{ee_invoice_id}"

        sync = frappe.get_doc({
            "doctype": "EasyEcom Sync Record",
            "company": marketplace_account.company,
            "entity_doctype": "Sales Invoice",
            "entity_name": si.name,
            "entity_type": "Sales Invoice",
            "direction": "Pull",
            "status": "Success",
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "attempts": 1,
            "last_attempt_at": now_datetime(),
            "pull_payload_hash": payload_hash,
            "last_response_payload": payload_canonical[:60000],
        })
        sync.flags.ignore_permissions = True
        sync.insert(ignore_if_duplicate=True)
        frappe.db.commit()
        return sync.name
    except Exception as exc:
        frappe.log_error(
            title=f"§12 Sync Record write failed for {si.name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return None


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
        frappe.log_error(
            title=f"§12 variance Discrepancy raise failed for {si.name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"tax_variance_pct": round(variance_pct, 2), "discrepancy_raised": False}


def _check_total_variance(
    *,
    si: Any,
    marketplace_account: Any,
    ee_invoice_id: str,
    ee_grand_total: float,
    correlation_id: str,
) -> dict:
    """§12.9 line 2821 — EE order total vs ERPNext SI.grand_total must
    match within 1 paisa (₹0.01). Mismatch raises an Integration
    Discrepancy as an upstream alert.

    Independent of the tax variance check (Path 2): catches discount
    mishandling, missing line items, rounding bugs that the tax
    check would miss because EE-supplied tax is applied directly.

    Skipped when ee_grand_total is 0 (zero-amount orders are edge
    cases — refunds-only, promotional, etc.; no alert).

    Returns:
        {"total_variance_paise": <int>, "discrepancy_raised": <bool>}
    """
    if not ee_grand_total:
        return {"total_variance_paise": 0, "discrepancy_raised": False}

    si_grand_total = float(si.get("grand_total") or 0)
    delta = abs(ee_grand_total - si_grand_total)
    variance_paise = round(delta * 100)  # 1 paisa = ₹0.01

    if variance_paise <= 1:
        return {"total_variance_paise": variance_paise, "discrepancy_raised": False}

    try:
        from ecommerce_super.easyecom.flows.grn_pull import _raise_discrepancy

        _raise_discrepancy(
            kind="B2C total variance — EE vs SI > 1 paisa (§12.9)",
            reference_doctype="Sales Invoice",
            reference_name=si.name,
            company=si.company,
            reason=(
                f"§12 B2C SI build: EE order total ₹{ee_grand_total:.2f} "
                f"vs ERPNext SI.grand_total ₹{si_grand_total:.2f} "
                f"(delta {variance_paise} paise). EE invoice_id="
                f"{ee_invoice_id}, Marketplace Account "
                f"{marketplace_account.name}, correlation_id="
                f"{correlation_id}. "
                f"\n\nThis is an UPSTREAM-ISSUE alert per Path 2. "
                f"SI carries EE-supplied tax + rates derived from EE "
                f"breakup_types; a > 1 paisa delta from EE's reported "
                f"total typically means: (a) line.discount handling "
                f"differs between EE and ERPNext; (b) a line item was "
                f"missing in the payload; (c) rounding mode mismatch "
                f"(per-line vs per-invoice). SI data is immutable; "
                f"FDE investigates the EE-side payload structure."
            ),
        )
        return {"total_variance_paise": variance_paise, "discrepancy_raised": True}
    except Exception as exc:
        frappe.log_error(
            title=f"§12 total-variance Discrepancy raise failed for {si.name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"total_variance_paise": variance_paise, "discrepancy_raised": False}


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
    """SHA-256 of the canonical-JSON order payload — kept for tests +
    future use (not currently called by builder since the Sync Record
    write handles hashing inline)."""
    canonical = json.dumps(order_row, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
