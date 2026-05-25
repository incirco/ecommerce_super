"""Drop the orphaned EasyEcom-Warehouse-Pull Field Mapping ruleset.

The §8a refactor renamed the location-discovery ruleset from
EasyEcom-Warehouse-Pull → EasyEcom-Location-Pull. Frappe's fixtures
sync inserts/updates by name; it does NOT delete renamed entries — so
without this patch, sites that received the old fixture would carry
both rulesets after migrate, the orphaned one referencing stale paths
(location_id, name, pincode, is_active) that don't exist in the real
EasyEcom payload.

Idempotent: skips silently if the row is already gone (fresh sites
that never received the §5.11 fixture, or sites where this patch has
already run).
"""

from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.table_exists("EasyEcom Field Mapping"):
        return
    if not frappe.db.exists("EasyEcom Field Mapping", "EasyEcom-Warehouse-Pull"):
        return
    frappe.delete_doc(
        "EasyEcom Field Mapping",
        "EasyEcom-Warehouse-Pull",
        force=True,
        ignore_permissions=True,
    )
    frappe.db.commit()
    print(
        "[ecommerce_super] dropped orphaned EasyEcom-Warehouse-Pull ruleset "
        "(renamed to EasyEcom-Location-Pull in the §8a refactor)"
    )
