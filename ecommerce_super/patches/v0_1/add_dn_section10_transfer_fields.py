"""Add DN-level §10 transfer fields — Customer-anchored routing.

GROUNDING CORRECTION (live Harmony smoke 2026-05-30):
  - The FDE explicitly ticks "Is Internal Transfer (§10)" on the DN
    to opt into §10 routing. The checkbox auto-fetches from
    `customer.is_internal_customer` so it pre-ticks for internal
    customers, but the FDE can override.
  - When ticked, the two warehouse fields (Transfer From, Transfer To)
    become visible + mandatory. before_validate derives item
    warehouses, GIT routing, and customer addresses from these.

The earlier-added `ecs_section10_target_warehouse` field is superseded
by `ecs_section10_transfer_to_warehouse` and is hidden — its value is
mirrored from transfer_to during validate so legacy lookups still
resolve.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Delivery Note": [
                {
                    "fieldname": "ecs_is_section10_transfer",
                    "label": "Is Internal Transfer",
                    "fieldtype": "Check",
                    "default": 0,
                    "insert_after": "customer_name",
                    "fetch_from": "customer.is_internal_customer",
                },
                {
                    "fieldname": "ecs_section10_transfer_from_warehouse",
                    "label": "Transfer From Warehouse",
                    "fieldtype": "Link",
                    "options": "Warehouse",
                    "insert_after": "ecs_is_section10_transfer",
                    "depends_on": "eval:doc.ecs_is_section10_transfer",
                    "mandatory_depends_on": "eval:doc.ecs_is_section10_transfer",
                },
                {
                    "fieldname": "ecs_section10_transfer_to_warehouse",
                    "label": "Transfer To Warehouse",
                    "fieldtype": "Link",
                    "options": "Warehouse",
                    "insert_after": "ecs_section10_transfer_from_warehouse",
                    "depends_on": "eval:doc.ecs_is_section10_transfer",
                    "mandatory_depends_on": "eval:doc.ecs_is_section10_transfer",
                },
            ],
        },
        ignore_validate=True,
    )

    # Hide the legacy single-target field
    legacy = frappe.db.get_value(
        "Custom Field",
        {"fieldname": "ecs_section10_target_warehouse", "dt": "Delivery Note"},
        "name",
    )
    if legacy:
        frappe.db.set_value("Custom Field", legacy, "hidden", 1)

    # Hide the standard Title field on the §10 transactional doctypes
    # — it shows blank on new docs and clutters the Details tab.
    for dt in (
        "Delivery Note",
        "Purchase Receipt",
        "Sales Invoice",
        "Purchase Invoice",
    ):
        ps_name = f"{dt}-title-hidden"
        if not frappe.db.exists("Property Setter", ps_name):
            ps = frappe.new_doc("Property Setter")
            ps.doctype_or_field = "DocField"
            ps.doc_type = dt
            ps.field_name = "title"
            ps.property = "hidden"
            ps.value = "1"
            ps.property_type = "Check"
            ps.flags.ignore_permissions = True
            ps.insert()
        frappe.clear_cache(doctype=dt)
    frappe.db.commit()
