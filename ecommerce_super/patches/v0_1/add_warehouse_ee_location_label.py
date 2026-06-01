"""Add `ecs_ee_location_label` custom field on Warehouse.

UX layer for §10 (and §9) outbound flows — surface EE-mapping status
in every Warehouse Link autocomplete (DN, PO, SI, etc.) so users see
"EE: <location_name> (#<location_key>)" right next to the warehouse
name, and pick non-EE warehouses with eyes open. Field is read-only,
populated by the EasyEcom Location's on_update / on_trash hook.

Empty string means "not EE-mapped" (or mapped to a non-Live location).
Treat absence-of-label as a meaningful UX signal — don't show a blank
column header.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import (
    create_custom_fields,
)


def execute() -> None:
    create_custom_fields(
        {
            "Warehouse": [
                {
                    "fieldname": "ecs_ee_location_label",
                    "label": "EE Location",
                    "fieldtype": "Data",
                    "insert_after": "warehouse_name",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "translatable": 0,
                    "length": 140,
                    "description": (
                        "Auto-computed from EasyEcom Location's "
                        "mapped_warehouse. Empty when not EE-mapped "
                        "(or mapped only to a non-Live location)."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
