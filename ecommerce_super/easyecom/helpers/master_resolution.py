"""ERPNext → EasyEcom master-data identifier resolvers.

The §8d/§8e build chose Map DocTypes over Custom Fields on Item /
Customer for EE-side identifier storage. These helpers honour that
choice so callers across §11 / §12 / future flows don't reinvent the
join. Returns None when the ERPNext entity isn't yet synced — caller
decides whether that's a refusal (preconditions) or a soft skip
(diagnostics).

The §11 packet (pre-build) referenced `item.ecs_easyecom_product_sku_code`
and `customer.ecs_easyecom_customer_id` directly, expecting Custom
Fields that never shipped. Spec-vs-code reconciliation lands at
Phase 1 closeout in SPEC_11_patch_notes.md; for now this module is
the single source of truth for EE identifier lookups.
"""

from __future__ import annotations

import frappe


def resolve_ee_sku(item_code: str) -> str | None:
    """EE-side SKU for an ERPNext Item via §8d Item Map.

    Returns the `ee_sku` of the Mapped row, or None when the Item has
    no Map yet (never pulled / pushed) or the Map is in a non-Mapped
    state (Pending / Flagged / Drift). Caller decides what to do with
    None — preconditions throw, diagnostics report 'not synced'.
    """
    if not item_code:
        return None
    return frappe.db.get_value(
        "EasyEcom Item Map",
        {
            "erpnext_doctype": "Item",
            "erpnext_name": item_code,
            "status": "Mapped",
        },
        "ee_sku",
    )


def resolve_ee_customer_id(customer: str) -> str | None:
    """EE-side customer ID for an ERPNext Customer via §8e Customer Map.

    Returns the `ee_customer_id` of the Mapped row, or None when the
    Customer has no Map yet or the Map is in a non-Mapped state.
    """
    if not customer:
        return None
    return frappe.db.get_value(
        "EasyEcom Customer Map",
        {
            "erpnext_doctype": "Customer",
            "erpnext_name": customer,
            "status": "Mapped",
        },
        "ee_customer_id",
    )
