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

    # Marketplace match guard — getAllOrders returns orders from ALL
    # marketplaces on the EE Account (B2B businessorder rows leak through
    # the status=Manifested filter; live-verified 2026-06-29 Harmony
    # smoke). Skip rows whose marketplace_id doesn't match this Account's
    # configured Marketplace. Per §12.9 line 2824 — should be a per-record
    # Failed Sync Record, but a B2CBuilderError + per-record dispatch
    # catch is functionally equivalent (caller logs + continues batch).
    ee_marketplace_id = str(order_row.get("marketplace_id") or "").strip()
    if ee_marketplace_id and str(marketplace_account.marketplace) != ee_marketplace_id:
        raise B2CBuilderError(
            f"Order row {ee_invoice_id} marketplace_id={ee_marketplace_id!r} "
            f"does not match Marketplace Account "
            f"{marketplace_account.name}'s marketplace="
            f"{marketplace_account.marketplace!r}. "
            "Skipping — likely a different marketplace's order leaking "
            "through the polling status filter."
        )

    # Skip non-B2C order types — §10 (stocktransferorder) and §11
    # (businessorder) flows land these on the same EE Account when
    # marketplace_id=64 (internal B2B/STN channel). The marketplace_id
    # guard above catches most leaks, but this defends against an FDE
    # misconfiguring a Marketplace Account at marketplace_id=64.
    order_type_key = str(order_row.get("order_type_key") or "").strip().lower()
    non_b2c_keys = {"businessorder", "stocktransferorder"}
    if order_type_key in non_b2c_keys:
        raise B2CBuilderError(
            f"Order row {ee_invoice_id} is order_type_key={order_type_key!r} "
            "(§10/§11 territory). §12 only handles B2C marketplace orders. "
            "Skipping."
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
    # EE getAllOrders field for the order total is `total_amount`
    # (live-verified 2026-06-29 Harmony — earlier candidates
    # `invoice_amount`, `grand_total`, `total` are all absent).
    # Kept as fallbacks for portability across EE versions / endpoints.
    ee_grand_total = float(
        order_row.get("total_amount")
        or order_row.get("invoice_amount")
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
                # Pre-populate price_list_rate = rate so ERPNext's
                # set_missing_values() doesn't fetch the Price List and
                # overwrite our EE-sourced rate. Without this, Item Price
                # from any active Price List (e.g. 199 for a BOGO item)
                # replaces our rate=0 (verified 2026-07-01 on
                # SQ-388100821).
                "price_list_rate": li["rate"],
                "discount_amount": 0,
                "discount_percentage": 0,
                "warehouse": warehouse,
                "gst_hsn_code": li.get("gst_hsn_code"),
                # Batch tracking — for Items with has_batch_no=1, set
                # batch_no from the EE-supplied batch code. Batch is
                # find-or-created via _ensure_batch. When the Item
                # isn't batch-tracked, batch_no key is omitted (empty
                # string would fail SI Item validation).
                #
                # ERPNext v16 note: v16 defaults to the Serial and
                # Batch Bundle DocType for batch tracking (rich child
                # link) and the plain batch_no field is only respected
                # when use_serial_batch_fields=1. Without that flag,
                # the "Pick Serial / Batch No" button on the SI form
                # remains active and batch_no is treated as a hint,
                # not the source of truth. Set both together.
                **(
                    {
                        "batch_no": _ensure_batch(
                            item_code=li["item_code"],
                            batch_code=li["ee_batch_code"],
                            expiry_date=li.get("ee_batch_expiry"),
                            ee_invoice_id=ee_invoice_id,
                        ),
                        "use_serial_batch_fields": 1,
                    }
                    if li.get("ee_batch_code")
                    and frappe.db.get_value("Item", li["item_code"], "has_batch_no")
                    else {}
                ),
                # Also mark 0-rate lines as free items — extra defense
                # against downstream pricing hooks
                **({"is_free_item": 1} if li.get("is_free_item") else {}),
            }
            for li in line_items
        ],
        # Prevent Pricing Rules from firing on this SI — EE is source of
        # truth for rates (Path 2), we don't want ERPNext-side promo
        # rules mangling the numbers.
        "ignore_pricing_rule": 1,
        # Custom Fields (recon source-of-truth values)
        "ecs_marketplace": marketplace_account.marketplace,
        "ecs_marketplace_order_id": marketplace_order_id,
        "ecs_easyecom_order_id": ee_order_id,
        "ecs_easyecom_invoice_id": ee_invoice_id,
        # EE-generated GST invoice number (e.g. "CHR62627-5091") — this
        # is the number printed on the invoice PDF the customer receives.
        # For §12 B2C, EE is the invoice-numbering authority (they run
        # the marketplace-side GSP for these orders), so we capture it
        # here AND use it as the SI's canonical name (see rename below).
        "ecs_easyecom_invoice_number": (
            order_row.get("invoice_number") or order_row.get("invoiceNumber")
        ),
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

    # Rename to the canonical customer-facing invoice identifier. Who
    # generates the invoice differs by channel type (user directive
    # 2026-07-01):
    #   - Own Storefront (Shopify) / POS-Offline: EE's GSP or seller
    #     generates the invoice → use invoice_number (e.g. CHR62627-5158)
    #   - B2C Marketplace (Flipkart, Amazon, Myntra, FBA, etc.): the
    #     MARKETPLACE generates the invoice → use reference_code
    #     (e.g. OD337715325683285400 for Flipkart, 405-… for Amazon)
    # The corresponding EE-side field is stored raw in
    # ecs_easyecom_invoice_number regardless, for downstream tracking.
    channel_type = frappe.db.get_value(
        "Marketplace", marketplace_account.marketplace, "channel_type"
    )
    if channel_type in ("Own Storefront", "POS-Offline"):
        canonical_name = (
            order_row.get("invoice_number") or order_row.get("invoiceNumber")
        )
    else:
        # B2C Marketplace and everything else — marketplace is the
        # invoicing authority; reference_code is what the customer +
        # marketplace use to refer to the order/invoice.
        canonical_name = marketplace_order_id

    if canonical_name and canonical_name.strip() and si.name != canonical_name:
        old_name = si.name
        try:
            frappe.rename_doc(
                "Sales Invoice", old_name, canonical_name,
                force=True, merge=False,
            )
            si = frappe.get_doc("Sales Invoice", canonical_name)
        except Exception as exc:
            frappe.logger().warning(
                f"§12 SI rename failed for {old_name!r} → "
                f"{canonical_name!r}: {type(exc).__name__}: {exc}. "
                "Keeping auto-generated name; canonical invoice number "
                "still available in ecs_easyecom_invoice_number / "
                "ecs_marketplace_order_id."
            )

    frappe.db.commit()

    # ---- 6b. Attach EE-hosted PDFs (invoice + shipping label) ----
    # documents dict from getAllOrders payload — presigned S3 URLs
    # that can be fetched without EE auth. Two attachments per SI:
    # the customer's invoice PDF and the courier shipping label.
    _attach_ee_documents(si, order_row)

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

    # Flat fields at order_row top level. EE's getAllOrders flattens
    # shipping address into the order_row root with bare field names —
    # `state`, `state_code`, `city`, etc. (live-verified 2026-06-29
    # Harmony retailorder payload).
    for key in (
        "shipping_state", "ship_state", "buyer_state",
        "customer_state", "state_name",
        "state",  # EE getAllOrders flat-address convention
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
    """Resolve EE order line array → list of (item_code, qty, rate, hsn).

    Reuses the §11.5.2 Item Map pattern: EE SKU → erpnext_name via
    EasyEcom Item Map. Raises B2CBuilderError listing all unmapped
    SKUs in one go (so the FDE fixes them in a single round-trip).

    EE payload field name varies per endpoint:
      - /orders/V2/getAllOrders returns `suborders` (live-verified
        2026-06-29 Harmony smoke)
      - /orders/V2/getOrderDetails returns `order_items` (§11 patch
        note 1, live-verified 2026-06-23)
    We scan both keys (plus camelCase variants) for portability.
    """
    items = (
        order_row.get("suborders")
        or order_row.get("order_items")
        or order_row.get("orderItems")
        or []
    )
    if not items:
        raise B2CBuilderError(
            "EE order row has no suborders / order_items — "
            "cannot build SI with zero lines."
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

        item_meta = frappe.db.get_value(
            "Item", item_code,
            ["gst_hsn_code", "has_batch_no", "is_stock_item"],
            as_dict=True,
        ) or {}
        hsn = item_meta.get("gst_hsn_code")
        # Only real stock items with batch tracking get batch handling.
        # Non-stock combos / bundles (is_stock_item=0) ignore batch_no
        # even if has_batch_no=1 — ERPNext strips it silently — so
        # don't waste rows splitting them per batch. Also don't call
        # _ensure_batch (would create orphan Batches).
        item_takes_batch = bool(
            item_meta.get("has_batch_no") and item_meta.get("is_stock_item")
        )

        # Batch codes — EE returns them per suborder when the polling
        # request includes get_batch_codes=1 (live-verified 2026-07-01
        # against Puresta). Format (single or multi-batch pick):
        #   batch_codes:       "`<code>" or "`<code1>,<code2>,..."
        #   batchcode_expiry:  "<code>:<date>" or "<c1>:<d1>,<c2>:<d2>,..."
        batch_codes_raw = line.get("batch_codes") or ""
        batchcode_expiry_raw = line.get("batchcode_expiry") or ""
        batch_codes = (
            _parse_all_batch_codes(batch_codes_raw) if item_takes_batch else []
        )
        n_batches = len(batch_codes)

        # Multi-batch handling: when EE picked from N batches to fulfil
        # a qty (typically one unit per batch — verified against
        # Combo_Acne on CHR62627-5049), we SPLIT the SI line into N
        # rows of qty distributed as evenly as possible. Keeps the
        # batch → row mapping unambiguous for stock-ledger accounting.
        # For non-stock items this loop runs once with batch=None.
        batch_qty_pairs: list[tuple[str | None, int]] = []
        if n_batches == 0:
            batch_qty_pairs = [(None, qty)]
        elif n_batches == 1:
            batch_qty_pairs = [(batch_codes[0], qty)]
        else:
            base = qty // n_batches
            remainder = qty - (base * n_batches)
            for i, code in enumerate(batch_codes):
                extra = remainder if i == n_batches - 1 else 0
                batch_qty_pairs.append((code, base + extra))

        for batch_code, row_qty in batch_qty_pairs:
            if row_qty <= 0:
                continue
            expiry_date = (
                _parse_batch_expiry(batchcode_expiry_raw, batch_code)
                if batch_code else None
            )

            item_out = {
                "item_code": item_code,
                "qty": row_qty,
                "rate": rate,
                "gst_hsn_code": hsn,
                "ee_batch_code": batch_code,
                "ee_batch_expiry": expiry_date,
            }
            # BOGO / promo / bundle free items — EE sends selling_price=None
            # (rate resolves to 0). Without is_free_item=1, ERPNext's
            # set_missing_values() auto-fetches the Item Price from the Price
            # List (e.g. 199) and overwrites our rate=0.
            if rate == 0:
                item_out["is_free_item"] = 1
            out.append(item_out)

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
# Batch parsing helpers (EE payload → ERPNext Batch)
# ============================================================


def _parse_all_batch_codes(raw: str) -> list[str]:
    """EE's batch_codes field on a suborder — verified format
    (Puresta 2026-07-01):

        "`MD01"                    → one batch (single-batch pick)
        "`N72600135,MD36"          → two batches (multi-GRN pick, qty=2)
        "" | None | "NA"           → no batch

    Leading backtick is always present when batches exist; multiple
    batches are then COMMA-separated (not backtick-delimited as an
    earlier guess assumed). Order matches picking order — the k-th
    batch corresponds to the k-th unit of the qty.

    Returns the list of batch codes in order, or empty list.
    """
    if not raw or raw in ("NA", "N/A"):
        return []
    if not isinstance(raw, str):
        return []
    # Strip leading backtick(s), then split on comma
    stripped = raw.lstrip("`")
    return [p.strip() for p in stripped.split(",") if p.strip()]


def _parse_first_batch_code(raw: str) -> str | None:
    """Backwards-compat wrapper — returns first batch or None."""
    codes = _parse_all_batch_codes(raw)
    return codes[0] if codes else None


def _parse_batch_expiry(raw: str, batch_code: str | None):
    """EE's batchcode_expiry field:
      "N72600117:2028-02-28"                  → single {code: date}
      "N72600117:2028-02-28,MD02:2027-05-15"  → multi
      "" | None | "NA"                        → no expiry
    Returns the expiry for the given batch_code, or None.
    """
    if not raw or raw in ("NA", "N/A") or not batch_code:
        return None
    if not isinstance(raw, str):
        return None
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        code, expiry = pair.split(":", 1)
        if code.strip() == batch_code:
            try:
                from frappe.utils import getdate
                return getdate(expiry.strip())
            except Exception:
                return None
    return None


def _ensure_batch(
    *,
    item_code: str,
    batch_code: str,
    expiry_date=None,
    ee_invoice_id: str | None = None,
) -> str:
    """Find-or-create an ERPNext Batch for (item_code, batch_code).

    Batch DocType names Batches by their batch_id field. We use the
    EE batch code directly (e.g. 'MD01', 'N72600117'). If the Batch
    already exists, we return its name. If not, we create a fresh one
    with the expiry_date. Idempotent under concurrent poll ticks — a
    unique-key collision on the (item, batch_id) tuple is caught and
    the existing Batch is returned.
    """
    existing = frappe.db.get_value(
        "Batch",
        {"item": item_code, "batch_id": batch_code},
        "name",
    )
    if existing:
        return existing

    try:
        batch_doc = frappe.get_doc({
            "doctype": "Batch",
            "batch_id": batch_code,
            "item": item_code,
            "expiry_date": expiry_date,
            "reference_doctype": "EasyEcom Sync Record" if ee_invoice_id else None,
        })
        batch_doc.flags.ignore_permissions = True
        batch_doc.flags.ignore_mandatory = True
        batch_doc.insert()
        return batch_doc.name
    except frappe.DuplicateEntryError:
        # Race — another poll created it in the meantime
        return frappe.db.get_value(
            "Batch",
            {"item": item_code, "batch_id": batch_code},
            "name",
        )


# ============================================================
# EE document attachment (invoice PDF + shipping label PDF)
# ============================================================


def _attach_ee_documents(si, order_row: dict) -> dict:
    """Fetch invoice + label PDFs from EE's presigned S3 URLs and
    attach them as File records on the SI. URLs live in the
    `documents` dict of the getAllOrders payload:

        documents.easyecom_invoice → invoice PDF (always for EE-billed)
        documents.label            → shipping label PDF
        documents.marketplaceinvoice / marketplace_tax_invoice /
        marketplace_b2c_invoice     → marketplace-billed invoices
                                      (Flipkart, Amazon, etc.)

    Returns a summary of what was attached / skipped / failed.
    """
    import requests

    docs = order_row.get("documents") or {}
    if not isinstance(docs, dict):
        return {"skipped": "no documents dict"}

    # In priority order: try marketplace-generated invoices first for
    # marketplace channels (Flipkart / Amazon issue their own PDFs),
    # fall back to easyecom_invoice for Own Storefront / Offline.
    attachments: list[tuple[str, str, str]] = []  # (label, url, filename_prefix)
    for key, prefix in (
        ("marketplace_tax_invoice", "invoice"),
        ("marketplaceinvoice", "invoice"),
        ("marketplace_b2c_invoice", "invoice"),
        ("easyecom_invoice", "invoice"),
    ):
        url = docs.get(key)
        if url:
            attachments.append((key, url, prefix))
            break  # take the first invoice we find

    label_url = docs.get("label") or docs.get("originalLabelUrl")
    if label_url:
        attachments.append(("label", label_url, "label"))

    out = {"attached": [], "failed": []}
    for source_key, url, prefix in attachments:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            out["failed"].append({"key": source_key, "reason": "not a URL"})
            continue
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200 or not r.content:
                out["failed"].append({
                    "key": source_key,
                    "reason": f"HTTP {r.status_code}, {len(r.content)} bytes",
                })
                continue
            file_doc = frappe.get_doc({
                "doctype": "File",
                "file_name": f"{prefix}-{si.name}.pdf",
                "attached_to_doctype": "Sales Invoice",
                "attached_to_name": si.name,
                "content": r.content,
                "is_private": 1,
            })
            file_doc.flags.ignore_permissions = True
            file_doc.insert()
            out["attached"].append({"key": source_key, "file": file_doc.name})
        except Exception as exc:
            out["failed"].append({
                "key": source_key,
                "reason": f"{type(exc).__name__}: {str(exc)[:150]}",
            })
    frappe.db.commit()
    return out


# ============================================================
# Warehouse resolution
# ============================================================


def _resolve_warehouse(order_row: dict, company: str) -> str | None:
    """Resolve EE warehouse_id → ERPNext Warehouse via §8a Source-of-Truth
    Map. Returns None if not resolved — SI uses Company default.

    Flow (verified 2026-06-29 Harmony smoke):
      1. order_row.warehouse_id (EE company_id integer like 99293)
      2. → EasyEcom Location with ee_company_id = <that integer>;
         read its `location_key` field (the bare key, not the docname)
      3. → Source-of-Truth Map where ee_location_key = <key> AND
         company = <Company>; return its `warehouse` field
    """
    ee_company_id = (
        order_row.get("warehouse_id")
        or order_row.get("assigned_warehouse_id")
        or order_row.get("warehouseId")
    )
    if not ee_company_id:
        return None

    # Source-of-Truth Map's `ee_location_key` is a Link → EasyEcom
    # Location, so it stores the docname (e.g. ECS-LOC-ee9859099849),
    # NOT the bare location_key field value. Live-verified
    # 2026-06-29 Harmony smoke — LinkValidationError when using bare
    # key.
    location_docname = frappe.db.get_value(
        "EasyEcom Location",
        {"ee_company_id": str(ee_company_id)},
        "name",
    )
    if not location_docname:
        return None

    return frappe.db.get_value(
        "Source-of-Truth Map",
        {
            "ee_location_key": location_docname,
            "company": company,
            "enabled": 1,
        },
        "warehouse",
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
    rows.

    ERPNext v16 Company doctype doesn't have `default_tax_account`
    (live-verified 2026-06-29 — column doesn't exist). Resolution
    walks the CoA for India Compliance's conventional output-tax
    account names: prefer IGST (the inter-state default; B2C
    marketplace orders are commonly inter-state), then SGST / CGST,
    then any 'Output Tax' or 'Sales Taxes' generic. Raises if none.

    For proper CGST+SGST split on intra-state SIs, the SI's
    tax_category (driven by the Customer's pool) makes ERPNext's
    Sales Taxes and Charges Template apply automatically. This
    function only resolves the account for the single 'Actual' EE-
    supplied tax row when no template applies.
    """
    # Try canonical India Compliance output-tax account prefixes,
    # in preference order. Most common live shapes: 'Output Tax IGST',
    # 'Output Tax SGST', 'Output Tax CGST'.
    candidates = (
        "Output Tax IGST - ",
        "Output Tax SGST - ",
        "Output Tax CGST - ",
        "Output Tax - ",
        "Sales Taxes - ",
    )
    for prefix in candidates:
        match = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "name": ["like", f"{prefix}%"],
                "is_group": 0,
            },
            "name",
        )
        if match:
            return match

    raise B2CBuilderError(
        f"Company {company!r} has no output-tax account in the CoA. "
        "Configure 'Output Tax IGST / SGST / CGST' accounts via India "
        "Compliance, or create a generic 'Output Tax' / 'Sales Taxes' "
        "Account."
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
