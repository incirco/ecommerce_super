"""§11 Stage 2 — Gate 0 + precondition validation for B2B push.

Gate 0 — `is_section_11_gated(so)`:
  Returns True iff SO.set_warehouse maps to a Live + enabled EE
  Location. Same Gate-0 pattern as §9 and §10: when False, the
  integration is silently inert (pure ERPNext, integration not
  involved). Never throws.

Preconditions — `validate_preconditions(so, ee_account)`:
  Runs the nine refusals from §11 packet §11.2 in document order.
  Each throw uses the EXACT title + message from the packet so the
  FDE sees the same wording the spec promises. Throws block the SO
  save before any persisted state exists — same discipline as §10's
  validate_pre_submit.

These functions are called from push.py's validate_pre_push hook;
keeping them factored out lets trace_b2b_so reuse them for
read-only gate inspection.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_super.easyecom.helpers.master_resolution import (
    resolve_ee_customer_id,
    resolve_ee_sku,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_location_for_warehouse,
)


def is_section_11_gated(so: Any) -> bool:
    """Gate 0: returns True iff §11 should fire for this SO.

    Defensive — never throws. When set_warehouse is empty / non-EE-
    mapped, returns False (silent inert path)."""
    if not getattr(so, "set_warehouse", None):
        return False
    location = get_ee_location_for_warehouse(so.set_warehouse)
    return location is not None


def validate_preconditions(so: Any, ee_account: Any) -> None:
    """Run the nine §11.2 refusals. Throws with the exact packet text
    on the first failure (don't accumulate — the SO is unsavable
    until the first failure is fixed; subsequent failures surface
    on re-submit).
    """
    # 1. All line items' warehouse == set_warehouse
    for item in so.items or []:
        if item.warehouse != so.set_warehouse:
            frappe.throw(
                _(
                    "Mixed warehouses not supported for §11 push. All "
                    "line items must use set_warehouse. Item {0} (row "
                    "{1}) uses warehouse {2} but set_warehouse is {3}."
                ).format(
                    item.item_code,
                    item.idx,
                    item.warehouse,
                    so.set_warehouse,
                ),
                title=_("Mixed Warehouses Not Supported"),
            )

    # 2. EE Account has ecs_b2b_module configured
    module = getattr(ee_account, "ecs_b2b_module", None) or ""
    if not module:
        frappe.throw(
            _(
                "EasyEcom Account {0} is missing the B2B module "
                "configuration (Old B2B / New B2B). Configure "
                "ecs_b2b_module before pushing."
            ).format(ee_account.name),
            title=_("B2B Module Not Configured"),
        )

    customer = frappe.get_doc("Customer", so.customer)

    # 3. Customer synced (§8e Customer Map.ee_customer_id present, status=Mapped)
    if not resolve_ee_customer_id(so.customer):
        frappe.throw(
            _(
                "Customer {0} is not synced to EasyEcom for company "
                "{1}. Sync the customer before submitting."
            ).format(so.customer, so.company),
            title=_("Customer Not Synced"),
        )

    # 4. All items synced (§8d Item Map.ee_sku present, status=Mapped)
    for item in so.items or []:
        if not resolve_ee_sku(item.item_code):
            frappe.throw(
                _(
                    "Item {0} is not synced to EasyEcom. Sync the item "
                    "before submitting."
                ).format(item.item_code),
                title=_("Item Not Synced"),
            )

    # 5. GSTIN strict for Old B2B (URP fallback is New B2B only)
    if module == "Old B2B" and not (customer.tax_id or "").strip():
        frappe.throw(
            _(
                "Customer {0} has no GSTIN. Old B2B requires GSTIN; "
                "URP fallback is only available for New B2B."
            ).format(so.customer),
            title=_("Customer GSTIN Missing"),
        )

    # 6. HSN on every item
    for item in so.items or []:
        item_doc = frappe.get_cached_doc("Item", item.item_code)
        if not (item_doc.gst_hsn_code or "").strip():
            frappe.throw(
                _("Item {0} missing HSN code.").format(item.item_code),
                title=_("HSN Code Missing"),
            )

    # 7. Non-zero rate (no Free of Charge flag on SO Item in this
    #    codebase — per design-lead's pre-Stage-1 ruling, the check
    #    is simply rate > 0).
    for item in so.items or []:
        if float(item.rate or 0) <= 0:
            frappe.throw(
                _(
                    "Item {0} has rate 0; mark explicitly as Free of "
                    "Charge or set price."
                ).format(item.item_code),
                title=_("Zero Rate"),
            )

    # 8. Billing address (customer primary)
    if not (customer.customer_primary_address or "").strip():
        frappe.throw(
            _(
                "Customer {0} has no Billing Address. B2B GST invoice "
                "cannot be generated without billing address."
            ).format(so.customer),
            title=_("Billing Address Missing"),
        )

    # 9. Shipping address (SO-level OR customer-primary fallback)
    has_so_shipping = bool((so.shipping_address_name or "").strip())
    has_customer_primary = bool(
        (customer.customer_primary_address or "").strip()
    )
    if not has_so_shipping and not has_customer_primary:
        frappe.throw(
            _(
                "Sales Order {0} has no shipping address. Set shipping "
                "address on SO or configure on customer."
            ).format(so.name),
            title=_("Shipping Address Missing"),
        )

    # 10. Whole-number qty on every line (EE contract: fractional
    #     quantities are not supported on either B2B module). Silently
    #     truncating (2.5 → 2) under-ships the customer, so throw at
    #     submit time and let the FDE fix the SO before push.
    for item in so.items or []:
        qty = float(item.qty or 0)
        if qty != int(qty):
            frappe.throw(
                _(
                    "Item {0} (row {1}) has fractional quantity {2}. "
                    "EasyEcom does not support fractional B2B quantities. "
                    "Change the item's UOM to a whole-number one, or "
                    "split the line into whole-number qty rows before "
                    "submitting."
                ).format(item.item_code, item.idx, qty),
                title=_("Fractional Quantity Not Supported"),
            )
