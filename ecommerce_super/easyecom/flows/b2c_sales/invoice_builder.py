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
    backfill_mode: bool = False,
) -> dict:
    """Create SI + Sync Record audit trail from one EE order row.

    Args:
        backfill_mode: When True, treats the run as a historical
            re-materialisation (not a fresh live order). Concretely:
              - update_stock = 0 — the stock has already moved via EE
                weeks/months ago; a live insert now would double-count
                the depletion and drift the ERPNext ledger from reality.
              - _attach_ee_documents runs in reference-only mode — File
                records store the EE-hosted S3 URL instead of downloading
                the PDF locally, saving ~1-3s per SI × N orders. If EE
                later prunes the S3 objects, live re-download can be
                triggered by a follow-up sweep.
              - Variance checks (_check_variance, _check_total_variance)
                log warnings instead of raising Integration Discrepancies.
                Historical rows tend to trip these on stale HSN configs,
                but they aren't actionable during backfill; the recon
                engine catches real variance from settlement lines.

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

    # PR D — multi-currency handling for foreign-currency B2C exports.
    # EE getAllOrders returns `total_amount` + per-line `selling_price`
    # in the invoice's NATIVE currency (SGD / CAD / NPR / etc.), NOT
    # in INR. Discovered via Aug 2026 Puresta reconciliation: 4 export
    # invoices reconciled to zero delta once native → INR conversion
    # was applied. See drafts/spec_sections/spec_12_amendment_restore_
    # mp_order_map.draft.md for the analysis.
    invoice_currency = (
        (order_row.get("invoice_currency_code") or "").strip().upper()
        or "INR"
    )
    conversion_rate = _resolve_currency_conversion_rate(
        from_currency=invoice_currency,
        to_currency=frappe.get_cached_value(
            "Company", marketplace_account.company, "default_currency"
        ) or "INR",
        transaction_date=posting_date,
    )

    si_dict: dict[str, Any] = {
        "doctype": "Sales Invoice",
        "customer": pool_choice["customer"],
        "company": marketplace_account.company,
        "posting_date": posting_date,
        # PR D — native invoice currency (usually INR; SGD/CAD/NPR for
        # export orders). Ecommerce_super lets ERPNext do the INR
        # conversion natively via SI.currency + SI.conversion_rate —
        # matches Frappe-primitives-first rule (CLAUDE.md). Item rates
        # in items[] stay in native currency; ERPNext computes
        # base_grand_total = grand_total × conversion_rate.
        "currency": invoice_currency,
        "conversion_rate": conversion_rate,
        # backfill_mode: skip Stock Ledger movement. The stock has
        # already moved via EE in real time; a historical SI insert
        # with update_stock=1 would double-deplete the ERPNext ledger.
        "update_stock": 0 if backfill_mode else 1,
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
                # PR G' — per-line Item Tax Template drives IC's IGST/
                # CGST/SGST split at set_missing_values() time. Only
                # attached when a matching template exists (see
                # _resolve_item_tax_template — returns None for non-
                # standard rates so IC falls back to Item defaults).
                **(
                    {
                        "item_tax_template": _resolve_item_tax_template(
                            company=marketplace_account.company,
                            tax_rate=li.get("tax_rate", 0),
                        )
                    }
                    if _resolve_item_tax_template(
                        company=marketplace_account.company,
                        tax_rate=li.get("tax_rate", 0),
                    )
                    else {}
                ),
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

    # ---- 5b. Buyer address, Puresta-app fields, Channel dimension ----
    # These are structured EE-supplied fields the client ops team needs
    # for reporting + printing. Some (contact_mobile, contact_email,
    # market_place_customer_name, market_place_customer_billing_address)
    # are usually NULL from EE for B2C because Amazon/Flipkart/Shopify
    # suppress buyer PII at the /orders/V2/getAllOrders layer — those
    # only populate for tenants whose EE token has PII access enabled.
    # We still write what EE DOES return (city / state / pin / country)
    # so the SI address block, list-view filters, and channel-wise GL
    # reports work end-to-end.
    billing_display = "\n".join(
        x for x in [
            order_row.get("billing_name") or "",
            order_row.get("billing_address_1") or "",
            order_row.get("billing_address_2") or "",
            order_row.get("billing_city") or "",
            " - ".join(x for x in [
                order_row.get("billing_state") or "",
                order_row.get("billing_pin_code") or "",
            ] if x),
            order_row.get("billing_country") or "",
        ] if x
    )
    shipping_display = "\n".join(
        x for x in [
            order_row.get("address_line_1") or "",
            order_row.get("address_line_2") or "",
            order_row.get("city") or "",
            " - ".join(x for x in [
                order_row.get("state") or "",
                order_row.get("pin_code") or "",
            ] if x),
            order_row.get("country") or "",
        ] if x
    )
    ee_invoice_date_raw = order_row.get("invoice_date") or ""
    canonical_billing_state = _canonical_state_name(
        order_row.get("billing_state") or ""
    )
    buyer_gst_raw = (order_row.get("buyer_gst") or "").strip()
    gst_category = _derive_gst_category(buyer_gst_raw)
    place_of_supply = _derive_place_of_supply(order_row)
    si_dict.update({
        # Accounting Dimension: Channel = Marketplace. Required for
        # channel-wise GL / P&L reports (Puresta has this dimension
        # defined; SIs without it don't show up in the channel slice).
        "channel": marketplace_account.marketplace,

        # India Compliance — Place of Supply + GST category.
        # gst_category defaults to "Unregistered" (URP) when EE returns
        # blank / NA / URP for buyer_gst — that's the correct GST
        # treatment for sales to non-GSTIN buyers (still reported to
        # GSTN under the B2C schedule, not blocked). Real 15-char
        # GSTINs land as "Registered Regular" and populate
        # billing_address_gstin so IC can render the buyer's GSTIN
        # on the invoice PDF.
        "gst_category": gst_category,
        "billing_address_gstin": (
            buyer_gst_raw if gst_category == "Registered Regular" else ""
        ),
        # `place_of_supply` value is "NN-CanonicalStateName" — passes
        # IC's Autocomplete validation and drives the CGST+SGST vs IGST
        # split. None when EE omits billing_state; IC then derives
        # from the customer's default address (pool customer has one).
        "place_of_supply": place_of_supply,

        # Puresta custom app — "Market Place Customer Details" section
        # (empty on SI form before this fix because we never wrote to it).
        "market_place_customer_name": order_row.get("billing_name") or "",
        "market_place_customer_billing_address_display": billing_display,
        "market_place_customer_shipping_address_display": shipping_display,

        # Puresta custom app — "EasyEcom Details" section.
        # Both custom_ee_state and ee_state get the canonical name so
        # list-view filters group correctly (variant spellings would
        # split "Gujarat" and "gujarat" into two rows).
        "custom_ee_state": canonical_billing_state,
        "custom_ee_selling_price": (str(ee_grand_total) if ee_grand_total else ""),
        "ee_state": canonical_billing_state,
        "ee_invoice_date": (ee_invoice_date_raw[:19] if ee_invoice_date_raw else None),
        "ee_is_return": 1 if (order_row.get("order_status") == "Returned") else 0,
        "ee_collectable_amount": float(
            order_row.get("collectable_amount") or ee_grand_total or 0
        ),

        # Standard SI: contact + Customer PO reference. `po_no` /
        # `po_date` mirror the marketplace order id + date so ops can
        # search "customer PO = SQ-440390821" and find the invoice.
        # `po_date` deliberately uses order_date (not posting_date) —
        # posting_date now tracks EE invoice_date for GST timing, but a
        # customer's PO reference date is when they placed the order.
        "contact_mobile": (
            order_row.get("billing_mobile") or order_row.get("contact_num") or ""
        ),
        "contact_email": order_row.get("email") or "",
        "po_no": marketplace_order_id,
        "po_date": _resolve_po_date(order_row),

        # EE-hosted invoice PDF URL. Query string
        # `?request-content-type=application/force-download` stripped so
        # the file opens inline in Chrome instead of being downloaded
        # (the same S3 object is public without the query param).
        "ecs_easyecom_invoice_pdf_url": (
            ((order_row.get("documents") or {}).get("easyecom_invoice") or "")
            .split("?")[0]
        ),
    })

    # Structured billing + shipping address as a Table child row on
    # EasyEcom Address (istable=1). Lives off the SI row so its
    # columns don't inflate tabSales Invoice past MariaDB's 65535-byte
    # in-row limit (see patch
    # move_ecs_ee_address_fields_to_child_table for context). Only
    # add the child row when we actually have at least one non-empty
    # value from EE — creating an empty child on every SI just adds
    # noise to tabEasyEcom Address.
    address_child = {
        "billing_name":       order_row.get("billing_name") or "",
        "billing_address_1":  order_row.get("billing_address_1") or "",
        "billing_address_2":  order_row.get("billing_address_2") or "",
        "billing_city":       order_row.get("billing_city") or "",
        "billing_state":      canonical_billing_state,
        "billing_pincode":    order_row.get("billing_pin_code") or "",
        "billing_country":    order_row.get("billing_country") or "India",
        "shipping_address_1": order_row.get("address_line_1") or "",
        "shipping_address_2": order_row.get("address_line_2") or "",
        "shipping_city":      order_row.get("city") or "",
        "shipping_state":     _canonical_state_name(order_row.get("state") or ""),
        "shipping_pincode":   order_row.get("pin_code") or "",
        "shipping_country":   order_row.get("country") or "India",
    }
    # `India` defaults are always present — filter them out when
    # deciding "any real content" so a country-only row doesn't get
    # created.
    non_default_values = {
        k: v for k, v in address_child.items()
        if v and not (k.endswith("_country") and v == "India")
    }
    if non_default_values:
        si_dict["ecs_ee_address"] = [address_child]

    # PR G' — IC-native tax injection (replaces the legacy flat
    # charge_type="Actual" pattern that violated CLAUDE.md #206).
    #
    # We attach `taxes_and_charges` (Sales Taxes and Charges Template)
    # and per-item `item_tax_template` (see items[] block above). India
    # Compliance's set_missing_values() at insert time then generates
    # the correct IGST (inter-state) or CGST+SGST (intra-state) rows
    # using the SI's `place_of_supply` (set by _derive_place_of_supply,
    # PR #263) vs the seller's `pickup_state`.
    #
    # The variance check (§8 below) still runs post-insert to catch
    # drift between EE's authoritative `total_tax` and IC's computed
    # total — Path 2's safety net is preserved.
    #
    # When either template lookup returns None (template missing on
    # the site), the SI still inserts — with no tax rows — and the
    # variance check raises a Discrepancy so ops can add the missing
    # template. Better than blocking the SI creation entirely.
    sales_tax_template = _resolve_gst_sales_taxes_template(
        company=marketplace_account.company,
        place_of_supply=place_of_supply,
        seller_state=order_row.get("pickup_state"),
    )
    if sales_tax_template:
        si_dict["taxes_and_charges"] = sales_tax_template

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
    #
    # backfill_mode: reference-only. Store S3 URL on the File record
    # instead of downloading PDF content. Saves ~1-3s per SI × N.
    _attach_ee_documents(si, order_row, download_pdf=not backfill_mode)

    # ---- 6c. Credit Note for Returned / Cancelled orders ----
    # When the order is already in a reversed state at time of insert
    # (typical for backfill of historical orders that have completed
    # their full lifecycle), create a paired Credit Note (Sales Invoice
    # with is_return=1) referencing this original SI.
    #
    # GST parity: GSTR-1 must show both the original sale AND the
    # credit note reversing it. Net effect is zero but the audit trail
    # is complete. Without this, backfilled Returned/Cancelled orders
    # would show up as active sales in GSTR-1 without a matching
    # reversal, over-reporting sales to GSTN.
    #
    # Only in backfill_mode. In live flow, the polling loop that
    # observes an order_status transition to Returned/Cancelled owns
    # the Credit Note creation via a separate step (§13 Returns
    # Inwards) and this branch stays off.
    credit_note_name = None
    if (
        backfill_mode
        and order_row.get("order_status") in ("Returned", "Cancelled")
        and ee_grand_total
    ):
        try:
            credit_note = _build_credit_note_for_reversed_order(
                original_si=si,
                order_row=order_row,
            )
            credit_note_name = credit_note.name
        except Exception as exc:
            # Never let a CN failure fail the original SI backfill —
            # a follow-up sweep can retry the CN.
            frappe.logger().warning(
                f"§12 backfill: could not create Credit Note for "
                f"{order_row.get('order_status')} SI {si.name}: "
                f"{type(exc).__name__}: {exc}"
            )

    # ---- 6b. Marketplace Order Map — order-grain recon bridge ----
    # RESTORED post-PR #107 per Aug 2026 methodology decision (see
    # drafts/spec_sections/spec_12_amendment_restore_mp_order_map.draft.md
    # and PR #272). The Map + Shipment child give the recon engine the
    # order-grain join it needs (RECON_SPEC §6.2) — critical for split
    # shipments where one marketplace_order_id produces multiple SIs.
    #
    # This block is a strict superset of what Sync Record captures:
    # Sync Record is entity-centric (one per SI-per-direction), Map is
    # order-centric (one per marketplace_order_id, N shipments each).
    # Both coexist.
    try:
        mp_order_map_name = _upsert_marketplace_order_map(
            si=si,
            marketplace_account=marketplace_account,
            order_row=order_row,
            ee_order_id=ee_order_id,
            ee_invoice_id=ee_invoice_id,
            event_type=_derive_invoice_status_event_type(order_row),
            original_sales_invoice=None,
        )
        if credit_note_name:
            # Paired Credit Note also gets its own shipment row on the
            # same Map, pointing back to the original SI. Recon nets
            # Forward (SI) against Reverse (CN) at order grain per
            # RECON_SPEC §6.4.
            _upsert_marketplace_order_map(
                si=frappe.get_doc("Sales Invoice", credit_note_name),
                marketplace_account=marketplace_account,
                order_row=order_row,
                ee_order_id=ee_order_id,
                ee_invoice_id=f"CN-{ee_invoice_id}",
                event_type=(
                    "Returned" if order_row.get("order_status") == "Returned"
                    else "Sold(cancelled)"
                ),
                original_sales_invoice=si.name,
            )
    except Exception as exc:
        # Never let a Map upsert failure fail the SI insert — recon can
        # still function on Sync Record data alone until the sweeper
        # (PR F) backfills the Map row. Log for later manual repair.
        mp_order_map_name = None
        frappe.logger().warning(
            f"§12: could not upsert Marketplace Order Map for SI {si.name}: "
            f"{type(exc).__name__}: {exc}"
        )

    # ---- 7. Sync Record — per-sync audit trail ----
    # Sync Record captures the raw poll payload + hash + correlation id
    # for every SI created / touched. Retained alongside Marketplace
    # Order Map — different grain, different purpose (see 6b above).
    sync_record_name = _write_sync_record(
        si=si,
        marketplace_account=marketplace_account,
        order_row=order_row,
        ee_invoice_id=ee_invoice_id,
        correlation_id=correlation_id,
    )

    # ---- 8. Variance checks (Path 2 alert mechanism) ----
    # backfill_mode: log-only. Historical SIs frequently trip these
    # on stale HSN configs / catalog drift; a Discrepancy per row
    # floods the FDE inbox and isn't actionable during backfill.
    # The recon engine still catches real variance later.
    tax_variance = _check_variance(
        si=si,
        marketplace_account=marketplace_account,
        ee_invoice_id=ee_invoice_id,
        ee_tax_total=ee_tax_total,
        erpnext_tax_check=erpnext_tax_check,
        correlation_id=correlation_id,
        raise_discrepancy=not backfill_mode,
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
        raise_discrepancy=not backfill_mode,
    )

    return {
        "sales_invoice": si.name,
        "sync_record": sync_record_name,
        "customer_pool_used": pool_choice["kind"],
        "tax_variance_pct": tax_variance["tax_variance_pct"],
        "tax_discrepancy_raised": tax_variance["discrepancy_raised"],
        "total_variance_paise": total_variance["total_variance_paise"],
        "total_discrepancy_raised": total_variance["discrepancy_raised"],
        # Paired Credit Note (backfill_mode + Returned/Cancelled only)
        "credit_note": credit_note_name,
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


# EE-supplied state-name variants → ERPNext / India Compliance canonical
# names. IC's Place-of-Supply autocomplete rejects the variant on the
# LHS ("01-Jammu & Kashmir" — verified failure on backfill batch 2).
# When a variant lands, we substitute the canonical spelling before
# constructing `place_of_supply` and before writing `ee_state`.
STATE_NAME_ALIASES = {
    "jammu & kashmir": "Jammu and Kashmir",
    "j&k": "Jammu and Kashmir",
    # Post-2020 merger — DNH + DD are one UT (state code 26)
    "dadra & nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman & diu": "Dadra and Nagar Haveli and Daman and Diu",
    "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "dadra & nagar haveli and daman & diu": "Dadra and Nagar Haveli and Daman and Diu",
    "andaman & nicobar islands": "Andaman and Nicobar Islands",
    "andaman & nicobar": "Andaman and Nicobar Islands",
}


def _canonical_state_name(state: str) -> str:
    """Return the ERPNext / IC canonical Place-of-Supply state name
    for an EE-supplied state string. Passes untouched if already
    canonical."""
    s = (state or "").strip()
    if not s:
        return ""
    return STATE_NAME_ALIASES.get(s.lower(), s)


def _derive_gst_category(buyer_gst: str) -> str:
    """Map EE `buyer_gst` → India Compliance `gst_category`.

    URP (Unregistered Person) is the default when there's no valid
    GSTIN on the buyer — EE sometimes sends "NA" / "URP" / blank for
    B2C and for B2B customers who haven't shared their GSTIN. Per
    Indian GST law, sales to URPs are still valid — they're just
    reported under the B2C schedule instead of B2B.

    Returns "Registered Regular" only when the string looks like a
    real 15-char GSTIN (state-code + PAN-shape). Everything else →
    "Unregistered" (which IC accepts as the URP catch-all)."""
    g = (buyer_gst or "").strip().upper()
    if not g or g in ("NA", "URP", "N/A", "NONE", "-"):
        return "Unregistered"
    # GSTIN structure: 2-digit state code + 10-char PAN + 3-char suffix
    if len(g) == 15 and g[:2].isdigit() and g[2:7].isalpha():
        return "Registered Regular"
    return "Unregistered"


def _derive_place_of_supply(order_row: dict) -> str | None:
    """Compose `NN-StateName` for IC's Place-of-Supply field, using
    the canonical ERPNext state name (not EE's variant spelling).
    Returns None if state code or name is missing — IC then derives
    from the customer's default address."""
    code = (order_row.get("billing_state_code") or "").strip()
    state = _canonical_state_name(order_row.get("billing_state") or "")
    if not code or not state:
        return None
    # Pad single-digit state codes to two chars (EE sometimes sends "6"
    # instead of "06" for Haryana).
    if len(code) == 1:
        code = "0" + code
    return f"{code}-{state}"


def _gstin_state_code_to_name(code: str) -> str | None:
    """Minimal GSTIN state-code → state-name lookup. Covers the
    Indian-state GSTIN prefixes; returns None for unknown codes.

    Returns ERPNext canonical Place-of-Supply names — safe to feed
    directly into `place_of_supply` construction."""
    # GSTIN state codes per Income Tax / GST Council assignment.
    # 26 is the post-2020 DNH+DD combined UT (25 is obsolete).
    mapping = {
        "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
        "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
        "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
        "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
        "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
        "16": "Tripura", "17": "Meghalaya", "18": "Assam",
        "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
        "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
        "26": "Dadra and Nagar Haveli and Daman and Diu",
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
                # PR G' — carry EE's line-level tax_rate through so the
                # SI builder can resolve the right Item Tax Template.
                # Enables India Compliance to compute IGST/CGST/SGST
                # split natively at set_missing_values() time, replacing
                # the flat charge_type="Actual" pattern (CLAUDE.md #206).
                "tax_rate": float(line.get("tax_rate") or 0),
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
# Credit Note for Returned / Cancelled orders (backfill_mode)
# ============================================================


def _build_credit_note_for_reversed_order(
    *,
    original_si: Any,
    order_row: dict,
) -> Any:
    """Create a paired Credit Note (Sales Invoice with is_return=1)
    referencing an original SI, with negative qty per line.

    Called from build_si_from_ee_order when backfill_mode is True
    AND the EE order_status is "Returned" or "Cancelled". The
    Credit Note is inserted as Draft (docstatus=0) so the user can
    review and submit both together.

    IMPORTANT — return_against + Draft semantics:
        ERPNext validates that `return_against` points to a submitted
        SI at CREDIT NOTE SUBMIT time, not at insert. Insert as Draft
        succeeds even when the original is also Draft. Users need to
        submit the original SI first, then submit the CN.

    Posting date derivation:
        - EE `shipping_last_update_date` (when EE last updated
          shipping status — best proxy for when the return happened)
        - EE `manifest_date` (when the courier picked up — earlier
          fallback)
        - original_si.posting_date + 1 day (last-resort)
    """
    # Return posting date — best guess from EE's timestamps
    return_dt_raw = (
        order_row.get("shipping_last_update_date")
        or order_row.get("manifest_date")
        or ""
    )
    if return_dt_raw and len(return_dt_raw) >= 10:
        return_date = return_dt_raw[:10]
    else:
        return_date = frappe.utils.add_days(original_si.posting_date, 1)

    reason = order_row.get("order_status") or "Reversed"

    cn_dict: dict[str, Any] = {
        "doctype": "Sales Invoice",
        "customer": original_si.customer,
        "company": original_si.company,
        "posting_date": return_date,
        "set_posting_time": 1,
        # Backfill: no stock movement (stock was already reversed on EE
        # side; ledger stays clean).
        "update_stock": 0,
        # Credit-note contract
        "is_return": 1,
        "return_against": original_si.name,
        # PR D — inherit currency + conversion_rate from the original
        # SI. Credit Note reverses the invoice in the SAME currency +
        # AT THE SAME rate so base amounts net to exactly zero on the
        # INR ledger. Using a fresh conversion_rate here would leave a
        # residual INR gain/loss from FX drift between the two dates.
        "currency": original_si.currency or "INR",
        "conversion_rate": (
            float(original_si.conversion_rate or 1)
            if (original_si.currency or "INR") != "INR"
            else 1
        ),
        "ignore_pricing_rule": 1,
        # Mirror the header dimensions from the original — recon /
        # channel-wise reports treat the reversal on the same axes.
        "channel": original_si.get("channel"),
        "gst_category": original_si.get("gst_category"),
        "billing_address_gstin": original_si.get("billing_address_gstin"),
        "place_of_supply": original_si.get("place_of_supply"),
        "taxes_and_charges": original_si.get("taxes_and_charges"),
        "items": [
            {
                "item_code": it.item_code,
                # ERPNext Credit Note convention: negative qty. Same
                # rate — ERPNext computes amount = qty × rate as
                # negative automatically.
                "qty": -abs(float(it.qty)),
                "rate": float(it.rate),
                "price_list_rate": float(it.rate),
                "discount_amount": 0,
                "discount_percentage": 0,
                "warehouse": it.warehouse,
                "gst_hsn_code": it.get("gst_hsn_code"),
                "sales_invoice_item": it.name,  # link back to original line
            }
            for it in original_si.items
        ],
        # Traceability back-refs. Prefix invoice_id with "CN-" so
        # backfill_mode's dupe-check keys don't collide with the
        # original SI's ecs_easyecom_invoice_id.
        "ecs_easyecom_invoice_id": (
            f"CN-{original_si.get('ecs_easyecom_invoice_id')}"
        ),
        "ecs_easyecom_order_id": original_si.get("ecs_easyecom_order_id"),
        "ecs_marketplace": original_si.get("ecs_marketplace"),
        "ecs_marketplace_order_id": original_si.get("ecs_marketplace_order_id"),
        "ecs_easyecom_dispatch_status": reason,
        "remarks": (
            f"Credit Note for EE {reason} order — original SI "
            f"{original_si.name} (ee_invoice_id "
            f"{original_si.get('ecs_easyecom_invoice_id')})"
        ),
    }

    # Post-audit LOW-1 fix 2026-08-17: previously mirrored original_si.
    # taxes verbatim with negated tax_amount. That worked when originals
    # used the flat charge_type="Actual" pattern, but PR G' switched
    # originals to `taxes_and_charges` template + per-item item_tax_
    # template. IC-generated tax rows have `charge_type="On Net Total"`
    # (or similar) with `rate` populated by IC — copying those with
    # only `tax_amount` set and `rate` missing produces zero-tax rows
    # on the CN (ERPNext re-derives from rate).
    #
    # Fix: rely on cn_dict["taxes_and_charges"] already set above to
    # the original SI's template. ERPNext's set_missing_values() at
    # CN insert time regenerates the tax rows fresh, using the
    # per-item item_tax_template mirrored via `sales_invoice_item`
    # back-refs in the items[] list. Negative qty → negative
    # taxable amount → negative tax amount naturally.
    #
    # For legacy CNs against SIs still carrying the old flat-Actual
    # tax pattern (submitted pre-PR-#275), the same template on the
    # CN will produce IC-style rows that may over/under-count
    # slightly vs the original's Actual row. Accept the small delta
    # — those SIs will be re-issued via the backfill flow if needed.

    cn = frappe.get_doc(cn_dict)
    cn.flags.ignore_permissions = True
    cn.insert()
    frappe.db.commit()
    return cn


# ============================================================
# EE document attachment (invoice PDF + shipping label PDF)
# ============================================================


def _attach_ee_documents(
    si, order_row: dict, *, download_pdf: bool = True
) -> dict:
    """Fetch invoice + label PDFs from EE's presigned S3 URLs and
    attach them as File records on the SI. URLs live in the
    `documents` dict of the getAllOrders payload:

        documents.easyecom_invoice → invoice PDF (always for EE-billed)
        documents.label            → shipping label PDF
        documents.marketplaceinvoice / marketplace_tax_invoice /
        marketplace_b2c_invoice     → marketplace-billed invoices
                                      (Flipkart, Amazon, etc.)

    Args:
        download_pdf: When True (default, live flow), downloads the
            PDF content from S3 and stores it in Frappe's file storage
            (is_private=1). Self-contained but ~1-3s per SI. When
            False (backfill), stores the S3 URL as a reference on the
            File record (is_private=0, file_url=<url>). File.file_url
            has `?request-content-type=application/force-download`
            stripped so Chrome renders the PDF inline instead of
            triggering a download.

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
            if download_pdf:
                # Live flow — download + store locally
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
            else:
                # Backfill — reference S3 URL directly, no download.
                # Strip the force-download query so Chrome renders inline.
                file_doc = frappe.get_doc({
                    "doctype": "File",
                    "file_name": f"{prefix}-{si.name}.pdf",
                    "attached_to_doctype": "Sales Invoice",
                    "attached_to_name": si.name,
                    "file_url": url.split("?")[0],
                    "is_private": 0,
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
# PR G' — IC-native tax template resolvers
# ============================================================
#
# Replaces the legacy charge_type="Actual" flat-row pattern (which
# bypassed India Compliance's line-level IGST/CGST/SGST split and
# violated CLAUDE.md #206). The new pattern:
#
#   SI.taxes_and_charges = "<Sales Taxes and Charges Template name>"
#   SI.items[].item_tax_template = "<Item Tax Template name>"
#
# India Compliance's set_missing_values() then generates the correct
# tax rows (IGST for inter-state, CGST+SGST for intra-state) using
# the SI's place_of_supply (already set by _derive_place_of_supply).
#
# The existing _check_variance still runs post-insert to catch drift
# between EE's authoritative total_tax and IC's computed total — that
# safety net is preserved.


def _resolve_gst_sales_taxes_template(
    *, company: str, place_of_supply: str | None, seller_state: str | None
) -> str | None:
    """Return the Sales Taxes and Charges Template name matching this
    company + direction (intra vs inter state). Naming convention on
    Puresta + Health Q sites:

        Output GST In-state - {abbr}      (intra-state = CGST + SGST)
        Output GST Out-state - {abbr}     (inter-state = IGST)

    Direction derived from place_of_supply vs the seller's state.
    Returns None (caller falls back to no template + variance-check-only)
    when the template doesn't exist — safer than raising, which would
    block SI creation for a config gap.
    """
    abbr = frappe.get_cached_value("Company", company, "abbr")
    if not abbr:
        return None

    # Place of supply format is "NN-CanonicalStateName" (per PR #263).
    # Strip the numeric prefix to compare state names.
    pos_state = ""
    if place_of_supply and "-" in place_of_supply:
        pos_state = place_of_supply.split("-", 1)[1].strip().lower()

    is_intra = bool(
        seller_state and pos_state
        and _canonical_state_name(seller_state).lower() == pos_state
    )
    direction = "In-state" if is_intra else "Out-state"
    template_name = f"Output GST {direction} - {abbr}"

    if frappe.db.exists("Sales Taxes and Charges Template", template_name):
        return template_name

    frappe.logger().warning(
        f"§12 PR G': Sales Taxes and Charges Template {template_name!r} "
        f"not found for company={company!r}. SI will insert without a "
        f"template — variance check will still run against EE's total_tax."
    )
    return None


def _resolve_item_tax_template(*, company: str, tax_rate: float) -> str | None:
    """Return the Item Tax Template name matching this company + rate.
    Naming convention:

        GST 5% - {abbr}, GST 12% - {abbr}, GST 18% - {abbr}, GST 28% - {abbr}
        Nil-Rated - {abbr}          (for rate=0)

    Returns None for non-standard rates (IC then falls back to the
    Item Master's default tax template).
    """
    abbr = frappe.get_cached_value("Company", company, "abbr")
    if not abbr:
        return None

    rate_int = int(round(tax_rate))
    if rate_int in (5, 12, 18, 28):
        template_name = f"GST {rate_int}% - {abbr}"
    elif rate_int == 0:
        template_name = f"Nil-Rated - {abbr}"
    else:
        # Non-standard rate — IC will use the Item Master's default
        frappe.logger().warning(
            f"§12 PR G': non-standard tax_rate {tax_rate!r} — no "
            f"matching Item Tax Template. Falling back to Item's default."
        )
        return None

    if frappe.db.exists("Item Tax Template", template_name):
        return template_name
    frappe.logger().warning(
        f"§12 PR G': Item Tax Template {template_name!r} not found — "
        f"falling back to Item's default tax template."
    )
    return None


# ============================================================
# PR D — Multi-currency conversion resolver
# ============================================================
#
# Foreign-currency B2C exports (SGD / CAD / NPR / USD etc.) come back
# from getAllOrders with total_amount + line selling_prices in the
# NATIVE currency, NOT INR. Puresta Aug 2026 reconciliation:
# 4 export invoices had native-CAD/SGD/NPR values that our puller
# was treating as INR, producing wildly wrong SI totals.
#
# The fix (Frappe-primitives-first): set SI.currency to the native
# code + SI.conversion_rate to the FX rate. ERPNext then natively
# computes base_grand_total = grand_total × conversion_rate for the
# INR ledger. Everything downstream (GSTR-1 base amounts, GL entries,
# variance checks) works correctly without custom conversion logic
# in ecommerce_super.


def _resolve_currency_conversion_rate(
    *,
    from_currency: str,
    to_currency: str,
    transaction_date: Any,
) -> float:
    """Look up the FX rate for from_currency → to_currency on the
    transaction date, using ERPNext's native Currency Exchange lookup.

    Same-currency short-circuit (INR → INR) returns 1.0 without a
    DB call — the common case for domestic invoices.

    For cross-currency lookups, defers to ERPNext's
    `erpnext.setup.utils.get_exchange_rate`, which:
      1. Checks the Currency Exchange doctype for a manually-entered
         rate keyed on (date, from, to) — preferred source
      2. Falls back to the currency_exchange_rate service configured
         in System Settings (frappe.exchange APIs)
      3. Returns 0 if nothing resolves

    Raises B2CBuilderError when rate resolves to 0 for non-same-
    currency case — a real gap that would silently produce wrong INR
    base amounts. Ops adds a Currency Exchange row and re-polls.
    """
    if from_currency == to_currency:
        return 1.0

    try:
        from erpnext.setup.utils import get_exchange_rate
    except ImportError as exc:
        raise B2CBuilderError(
            f"§12 PR D: erpnext.setup.utils.get_exchange_rate not "
            f"importable ({exc!s}). Cannot resolve {from_currency} → "
            f"{to_currency} conversion — refusing to insert SI with "
            f"a wrong INR base."
        ) from exc

    rate = get_exchange_rate(
        from_currency=from_currency,
        to_currency=to_currency,
        transaction_date=transaction_date,
    ) or 0

    if not rate or rate <= 0:
        raise B2CBuilderError(
            f"§12 PR D: no Currency Exchange rate for {from_currency} → "
            f"{to_currency} on {transaction_date}. Refusing to insert "
            f"SI — a rate of 0 or 1 would land wildly-wrong INR base "
            f"amounts on the ledger. Fix: add a Currency Exchange row "
            f"for (date={transaction_date}, from={from_currency}, "
            f"to={to_currency}, exchange_rate=<rate>) via Setup → "
            f"Currency Exchange, then re-poll this order."
        )

    return float(rate)


# ============================================================
# Marketplace Order Map upsert (restored per PR #272 amendment)
# ============================================================


def _derive_invoice_status_event_type(order_row: dict) -> str:
    """Synthesise the EasyEcom Order Map Shipment.ecs_easyecom_event_type
    from EE's order_status + invoice_number presence, matching the
    EE tax export Invoice Status column.

    Mapping (verified against EE CSV cross-tab, Aug 2026 Puresta data):
      Sold             = { Shipped, Returned, Confirmed, Manifest Scanned }
                         AND invoice_number populated
      Sold(cancelled)  = { Cancelled } AND invoice_number populated
      (empty)          = invoice_number blank OR unrecognised status

    Returned is a distinct value here even though EE lumps it into
    Sold — the recon engine needs to distinguish Forward (Shipped)
    from Reverse (Returned) legs, so we surface Returned separately.
    """
    inv_num = (order_row.get("invoice_number") or "").strip()
    if not inv_num:
        return ""
    st = order_row.get("order_status") or ""
    if st == "Returned":
        return "Returned"
    if st in {"Shipped", "Confirmed", "Manifest Scanned"}:
        return "Sold"
    if st == "Cancelled":
        return "Sold(cancelled)"
    return ""


def _upsert_marketplace_order_map(
    *,
    si: Any,
    marketplace_account: Any,
    order_row: dict,
    ee_order_id: str | None,
    ee_invoice_id: str,
    event_type: str,
    original_sales_invoice: str | None,
) -> str | None:
    """Ensure a Marketplace Order Map exists for this SI's marketplace
    order, and append a Shipment child row for this SI.

    Idempotency: keyed on (marketplace, marketplace_order_id, company)
    at parent level and on `sales_invoice` at child level. Safe to
    call multiple times for the same SI — the child upsert is a no-op
    when a row for this SI already exists.

    Split-shipment support: when a Map already exists (another SI for
    the same marketplace_order_id was inserted earlier), this appends
    a new Shipment row rather than raising a duplicate error.

    Returns the Map name, or None if the marketplace_order_id was
    missing (defensive — legitimate B2C orders always have one).
    """
    marketplace_order_id = (
        order_row.get("reference_code")
        or order_row.get("referenceCode")
        or ""
    ).strip()
    if not marketplace_order_id:
        frappe.logger().warning(
            f"§12: SI {si.name} has no reference_code — skipping "
            f"Marketplace Order Map upsert. This should never happen "
            f"for a legitimate marketplace order."
        )
        return None

    marketplace = marketplace_account.marketplace
    company = marketplace_account.company

    # Look up existing Map on the composite key
    existing_name = frappe.db.get_value(
        "EasyEcom Marketplace Order Map",
        {
            "marketplace": marketplace,
            "marketplace_order_id": marketplace_order_id,
            "company": company,
        },
        "name",
    )

    if existing_name:
        mp_map = frappe.get_doc("EasyEcom Marketplace Order Map", existing_name)
    else:
        mp_map = frappe.get_doc({
            "doctype": "EasyEcom Marketplace Order Map",
            "marketplace_order_id": marketplace_order_id,
            "marketplace": marketplace,
            "marketplace_account": marketplace_account.name,
            "company": company,
            "easyecom_order_id": str(ee_order_id) if ee_order_id else None,
            # settlement_status defaults to "Forecast" per JSON default;
            # recon writes any transition. We never write to it here
            # beyond the default, honoring the interface contract in
            # RECON_SPEC §2.
        })

    # Idempotency at child level: skip if a Shipment row for this SI
    # already exists. Handles re-runs without duplicating.
    already_has_shipment = any(
        (row.sales_invoice or "") == si.name
        for row in (mp_map.shipments or [])
    )
    if not already_has_shipment:
        mp_map.append("shipments", _build_shipment_row(
            si=si,
            order_row=order_row,
            ee_invoice_id=ee_invoice_id,
            event_type=event_type,
            original_sales_invoice=original_sales_invoice,
        ))

    if existing_name:
        mp_map.save(ignore_permissions=True)
    else:
        mp_map.insert(ignore_permissions=True)

    return mp_map.name


def _build_shipment_row(
    *,
    si: Any,
    order_row: dict,
    ee_invoice_id: str,
    event_type: str,
    original_sales_invoice: str | None,
) -> dict:
    """Assemble one EasyEcom Order Map Shipment child dict from an EE
    order payload + the SI just created for it.

    All fields default to None/empty when EE omits them — the child
    doesn't require anything beyond `sales_invoice`.
    """
    documents = order_row.get("documents") or {}
    if not isinstance(documents, dict):
        documents = {}

    return {
        "doctype": "EasyEcom Order Map Shipment",
        "sales_invoice": si.name,
        "ecs_easyecom_event_type": event_type,
        "original_sales_invoice": original_sales_invoice,
        # EE identifiers
        "invoice_id": str(ee_invoice_id) if ee_invoice_id else None,
        "invoice_number": (
            order_row.get("invoice_number") or order_row.get("invoiceNumber") or ""
        ),
        "easyecom_invoice_pdf_url": (
            (documents.get("easyecom_invoice") or "").split("?")[0] or None
        ),
        # Manifest / dispatch
        "manifest_date": (order_row.get("manifest_date") or "")[:19] or None,
        "manifest_no": order_row.get("manifest_no") or None,
        "batch_id": order_row.get("batch_id") or None,
        "batch_created_at": (order_row.get("batch_created_at") or "")[:19] or None,
        "sales_channel": order_row.get("sales_channel") or None,
        "awb_number": order_row.get("awb_number") or None,
        "courier": order_row.get("courier") or None,
        # Currency — native from EE + actual conversion rate the SI
        # was created with. Native amounts preserved on the child for
        # audit. Reading SI.currency / SI.conversion_rate keeps this
        # in sync with what ERPNext will use for base_grand_total.
        "invoice_currency_code": (
            (order_row.get("invoice_currency_code") or "").strip().upper()
            or "INR"
        ),
        "conversion_rate": float(
            getattr(si, "conversion_rate", None) or 1
        ),
        # PR D — Auto-confirm rate for:
        #   - INR invoices (no FX drift possible), OR
        #   - Credit Notes (inherit rate from an already-confirmed
        #     original SI — no separate ops verification possible or
        #     needed. Post-audit HIGH-2 fix 2026-08-17.)
        #
        # Non-INR original SIs land with confirmed=0 → the pending-
        # manifest sweeper (PR #274) will NOT submit them until ops
        # verifies the rate against EE's tax export/portal and ticks
        # the box. Guards against FX drift between our Currency
        # Exchange DocType and EE's locked-in rate.
        "conversion_rate_confirmed": (
            1 if (
                # INR = no FX to confirm
                (
                    (order_row.get("invoice_currency_code") or "").strip().upper()
                    or "INR"
                ) == "INR"
                # OR this is a Credit Note (rate inherited from source)
                or bool(original_sales_invoice)
            ) else 0
        ),
        "conversion_rate_source": "ERPNext Currency Exchange",
        "total_amount_native": float(
            order_row.get("total_amount")
            or order_row.get("invoice_amount")
            or 0
        ),
        "total_tax_native": float(order_row.get("total_tax") or 0),
        # Payment (populated on prepaid orders — usually null on COD / B2B)
        "payment_gateway_name": order_row.get("payment_gateway_name") or None,
        "payment_gateway_transaction_number": (
            order_row.get("payment_gateway_transaction_number") or None
        ),
        # GST compliance cross-references (from EE side; may be blank
        # when the order isn't e-invoice-eligible or e-way-bill-eligible)
        "irn": order_row.get("irn") or None,
        "ack_no": order_row.get("ack_no") or None,
        "ack_date": (order_row.get("ack_dt") or order_row.get("ack_date") or "")[:19] or None,
        "eway_bill_number": order_row.get("eway_bill_number") or None,
        "eway_bill_date": (order_row.get("eway_bill_date") or "")[:19] or None,
    }


# ============================================================
# Sync Record (per-sync audit trail — retained alongside MP Order Map)
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
    raise_discrepancy: bool = True,
) -> dict:
    """Path 2 variance check: compare EE-supplied tax against the
    ERPNext-computed cross-check. >1% delta raises an Integration
    Discrepancy as an upstream-issue alert.

    If erpnext_tax_check is 0 (we couldn't compute — HSN missing,
    GST HSN Code rows incomplete), we skip the alert — better than
    a false-positive flood on fresh installs.

    Args:
        raise_discrepancy: When False (backfill), the >1% variance
            is logged via frappe.logger() instead of raising an
            Integration Discrepancy. Prevents Discrepancy flood on
            historical rows that trip on stale HSN configs.

    Returns:
        {"tax_variance_pct": <float>, "discrepancy_raised": <bool>}
    """
    if not erpnext_tax_check or not ee_tax_total:
        return {"tax_variance_pct": 0.0, "discrepancy_raised": False}

    variance_pct = abs(ee_tax_total - erpnext_tax_check) / ee_tax_total * 100
    if variance_pct <= 1.0:
        return {"tax_variance_pct": round(variance_pct, 2), "discrepancy_raised": False}

    if not raise_discrepancy:
        frappe.logger().warning(
            f"§12 backfill tax variance (log-only): {si.name} "
            f"EE={ee_tax_total:.2f} ERPNext={erpnext_tax_check:.2f} "
            f"delta={variance_pct:.2f}% ee_invoice_id={ee_invoice_id} "
            f"correlation_id={correlation_id}"
        )
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
    raise_discrepancy: bool = True,
) -> dict:
    """§12.9 line 2821 — EE order total vs ERPNext SI.grand_total must
    match within 1 paisa (₹0.01). Mismatch raises an Integration
    Discrepancy as an upstream alert.

    Independent of the tax variance check (Path 2): catches discount
    mishandling, missing line items, rounding bugs that the tax
    check would miss because EE-supplied tax is applied directly.

    Skipped when ee_grand_total is 0 (zero-amount orders are edge
    cases — refunds-only, promotional, etc.; no alert).

    Args:
        raise_discrepancy: When False (backfill), the > 1 paisa
            variance is logged via frappe.logger() instead of
            raising an Integration Discrepancy.

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

    if not raise_discrepancy:
        frappe.logger().warning(
            f"§12 backfill total variance (log-only): {si.name} "
            f"EE=₹{ee_grand_total:.2f} SI=₹{si_grand_total:.2f} "
            f"delta={variance_paise} paise ee_invoice_id={ee_invoice_id} "
            f"correlation_id={correlation_id}"
        )
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
    """Posting date for the SI — EE `invoice_date` drives GST-legal timing.

    Why invoice_date, not order_date: `posting_date` on the SI is the
    date GSTR-1 files under, the date GL entries hit, and the date all
    invoice-date-based accounting views (including EE's own tax export
    at `Sale (Month) Easycom.xlsx`) group by. GST §31 requires the tax
    invoice date to be the date the invoice was issued, not the date
    the customer placed the order. Using order_date creates silent
    drift versus EE's tax export and versus GSTR-1 for any order that
    straddles a month boundary between placement and invoicing.

    The B2B mirror (b2b_sales/invoice_mirror.py:_parse_posting_date)
    already uses invoice_date — this brings B2C in line.

    Fallback chain: invoice_date → order_date → today. order_date
    fallback covers cancelled-before-invoice cases where EE never
    minted an invoice_date.
    """
    raw = (
        order_row.get("invoice_date")
        or order_row.get("invoiceDate")
        or order_row.get("order_date")
        or order_row.get("orderDate")
    )
    if raw:
        try:
            return getdate(raw)
        except Exception:
            pass
    return getdate(now_datetime())


def _resolve_po_date(order_row: dict):
    """PO date on the SI — the customer's marketplace order date.

    `po_no` / `po_date` mirror the marketplace order (SQ-440390821 +
    the moment the customer clicked buy). Kept on `order_date` even
    though `posting_date` is now `invoice_date` — a customer's PO
    reference date is when they placed the order, not when EE later
    minted the invoice. Falls back to invoice_date when order_date
    is missing (extremely rare)."""
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
