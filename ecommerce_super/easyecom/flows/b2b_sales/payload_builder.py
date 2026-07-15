"""§11 createOrder payload builders — Old B2B + New B2B.

EE has two B2B modules and the payload shapes differ slightly:

  Old B2B (sync):
    - Quantity is a STRING ("5")
    - paymentGateway present (empty for self-shipped Phase 1)
    - shipping block may carry latitude/longitude
    - orderType: "businessorder"
    - Returns OrderID + SuborderID + InvoiceID synchronously

  New B2B (async / queued):
    - Quantity is an INTEGER (5)
    - No paymentGateway, no lat/long
    - is_pricing_master: false (ERPNext owns pricing)
    - queue: 1 (always queued)
    - Returns "Successfully Queued" — IDs arrive later via polling
    - URP fallback for GSTIN-missing customers (Old B2B refuses)

Hardcoded invariants (locked by packet, no decisions to re-make):
  is_market_shipped = 0       — B2B is always self-shipped in client model
  is_pricing_master = False   — ERPNext owns pricing (New B2B only)

These builders are pure functions — no DB writes, no EE calls. They
read whatever they need from the SO + Customer + Item Map + Payment
Entries and return a dict. Push.py (Stage 2) calls them, posts the
result, and persists the response.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _

from ecommerce_super.easyecom.flows.b2b_sales.customer_block import (
    build_customer_block,
)
from ecommerce_super.easyecom.flows.b2b_sales.date_format import (
    format_ist_date,
    format_ist_datetime,
)
from ecommerce_super.easyecom.flows.b2b_sales.payment import (
    derive_payment_fields,
)
from ecommerce_super.easyecom.helpers.master_resolution import (
    resolve_ee_sku,
)


IS_MARKET_SHIPPED: int = 0
IS_PRICING_MASTER: bool = False


def resolve_ee_sku_or_throw(item_code: str) -> str:
    """Resolve the EE-side SKU for an ERPNext Item or throw with the
    exact §11 refusal text. Builders call this so a missing-Map
    condition surfaces at payload-build time with the same message
    the precondition gate would have produced."""
    sku = resolve_ee_sku(item_code)
    if not sku:
        frappe.throw(
            _(
                "Item {0} is not synced to EasyEcom. Sync the item "
                "before submitting."
            ).format(item_code),
            title=_("Item Not Synced"),
        )
    return sku


def get_shipping_charge(so: Any) -> float:
    """Sum of taxes/charges whose account_head substring-matches
    'shipping'. Substring match is fragile (a tax-template-driven
    approach would be more robust); flagged for Phase 1 closeout
    design-review per the design-lead's pre-Stage-1 ruling."""
    total = 0.0
    for tax in (so.taxes or []):
        head = (tax.account_head or "").lower()
        if "shipping" in head:
            total += float(tax.tax_amount or 0)
    return total


def _item_price_and_discount(so_item: Any, so: Any) -> tuple[float, float]:
    """gh#184 + gh#187 + gh#197: EE's `Price` is applied PER-UNIT
    (multiplied by Quantity) but `itemDiscount` is applied PER-LINE
    (subtracted once, not multiplied). Both are TAX-INCLUSIVE.

    Three corrections rolled into this helper:
      1. gh#184 — reconstruct list price from post-discount rate +
         discount (was: sending `rate` alone → double count).
      2. gh#187 — gross up by SO tax multiplier so EE's back-out
         yields the correct net.
      3. gh#197 — multiply `itemDiscount` by qty so a qty>1 discounted
         line is discounted correctly. EE math for a discounted line
         is `(Price * qty) - itemDiscount`; sending itemDiscount per-
         unit under-discounts by (qty-1)*discount. Live symptom on
         SO-2610401 line 2 (qty=5, 50% discount): EE returned
         taxable=2700 instead of 1500 → SI = ₹5,984 vs SO = ₹4,724.

    Formula:
        tax_multiplier = so.grand_total / so.net_total
        Price          = (rate + discount) * tax_multiplier   # per-unit
        itemDiscount   = discount * qty * tax_multiplier      # per-line

    For SO-2610401 line 2 (rate=300, discount=300, qty=5, tax=5%):
        Price        = (300 + 300) * 1.05 = 630
        itemDiscount = 300 * 5 * 1.05     = 1575
        EE gross     = (630 * 5) - 1575   = 1575
        Backs out to taxable=1500, tax=75. Matches SO's ₹1,575 line total.
    """
    rate = float(so_item.rate or 0)
    discount = float(so_item.discount_amount or 0)
    qty = float(so_item.qty or 0)

    net_total = float(getattr(so, "net_total", 0) or 0)
    grand_total = float(getattr(so, "grand_total", 0) or 0)
    tax_multiplier = (
        grand_total / net_total if net_total > 0 else 1.0
    )

    price = round((rate + discount) * tax_multiplier, 2)
    # gh#197: EE applies itemDiscount ONCE per line (not per unit),
    # so we pre-multiply by qty here to hand EE the correct line-total
    # discount. Zero-discount lines are unaffected (0 * anything = 0).
    discount_incl_tax = round(discount * qty * tax_multiplier, 2)
    return price, discount_incl_tax


def build_old_b2b_item(so: Any, so_item: Any) -> dict:
    """Old B2B line item — Quantity as STRING."""
    price, discount = _item_price_and_discount(so_item, so)
    return {
        "OrderItemId": f"{so.name}-line-{so_item.idx}",
        "Sku": resolve_ee_sku_or_throw(so_item.item_code),
        "productName": so_item.item_name,
        "Quantity": str(so_item.qty),
        "Price": price,
        "itemDiscount": discount,
    }


def build_new_b2b_item(so: Any, so_item: Any) -> dict:
    """New B2B line item — Quantity as INTEGER, no productName."""
    price, discount = _item_price_and_discount(so_item, so)
    return {
        "OrderItemId": f"{so.name}-line-{so_item.idx}",
        "Sku": resolve_ee_sku_or_throw(so_item.item_code),
        "Quantity": int(so_item.qty),
        "Price": price,
        "itemDiscount": discount,
    }


def build_old_b2b_payload(so: Any, ee_account: Any) -> dict:
    """Old B2B createOrder payload (synchronous response).

    Old B2B refuses URP customers — GSTIN is strict. The fallback
    to URP lives only on the New B2B path. Preconditions in Stage 2
    catch this earlier with a refusal-specific message; the throw
    here is defensive (covers direct payload-builder calls bypassing
    the gate).
    """
    customer = frappe.get_doc("Customer", so.customer)
    if not customer.tax_id:
        frappe.throw(
            _(
                "Customer {0} has no GSTIN. Old B2B requires GSTIN; "
                "URP fallback is only available for New B2B."
            ).format(so.customer),
            title=_("Customer GSTIN Missing"),
        )

    payment = derive_payment_fields(so)

    return {
        "orderType": "businessorder",
        "orderNumber": so.name,
        "orderDate": format_ist_datetime(so.transaction_date),
        "expDeliveryDate": format_ist_date(so.delivery_date),
        "is_market_shipped": IS_MARKET_SHIPPED,
        "remarks1": so.terms or "",
        "remarks2": "",
        "shippingCost": get_shipping_charge(so),
        "discount": so.discount_amount or 0,
        "walletDiscount": 0,
        "promoCodeDiscount": 0,
        "prepaidDiscount": 0,
        "paymentMode": payment["paymentMode"],
        "paymentGateway": "",
        "shippingMethod": payment["shippingMethod"],
        "paymentTransactionNumber": payment["paymentTransactionNumber"],
        "collectableAmount": payment["collectableAmount"],
        "packageWeight": 0,
        "packageHeight": 0,
        "packageWidth": 0,
        "packageLength": 0,
        "taxIdentificationNumber": customer.tax_id,
        "items": [build_old_b2b_item(so, it) for it in so.items],
        "customer": [build_customer_block(so, include_lat_long=True)],
    }


def build_new_b2b_payload(so: Any, ee_account: Any) -> dict:
    """New B2B createOrder payload (async / queued response).

    GSTIN-missing customers fall back to URP — New B2B accepts
    unregistered B2B sales explicitly.
    """
    customer = frappe.get_doc("Customer", so.customer)
    gstin = customer.tax_id or "URP"

    payment = derive_payment_fields(so)

    return {
        "orderType": "businessorder",
        "orderNumber": so.name,
        "orderDate": format_ist_datetime(so.transaction_date),
        "is_market_shipped": IS_MARKET_SHIPPED,
        "remarks1": so.terms or "",
        "is_pricing_master": IS_PRICING_MASTER,
        "items": [build_new_b2b_item(so, it) for it in so.items],
        "paymentMode": payment["paymentMode"],
        "paymentTransactionNumber": payment["paymentTransactionNumber"],
        "collectableAmount": payment["collectableAmount"],
        "shippingMethod": payment["shippingMethod"],
        "shippingCost": get_shipping_charge(so),
        "discount": so.discount_amount or 0,
        "taxIdentificationNumber": gstin,
        "customer": [build_customer_block(so, include_lat_long=False)],
        "queue": 1,
    }


def compute_payload_hash(payload: dict) -> str:
    """SHA-256 hex of canonical JSON for audit.

    Canonicalisation: sorted keys, no whitespace separators. Same
    payload always produces the same hash — used for idempotency
    detection (re-push of identical payload skips the EE call) and
    audit reproducibility on the Map row.
    """
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
