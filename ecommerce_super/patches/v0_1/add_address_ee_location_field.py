"""Add `ecs_ee_location` custom field on Address.

Existence of a non-empty `ecs_ee_location` on an Address signals
"this Address is EE-managed — mirrored from the linked EasyEcom
Location. Edit on the Location, not here."

The Address JS uses this field to lock the address fields and render
the "edit on EE Location" banner. The Location → Warehouse address
sync writes this back-pointer when upserting the Address.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import (
    create_custom_fields,
)


def execute() -> None:
    create_custom_fields(
        {
            "Address": [
                {
                    "fieldname": "ecs_ee_location",
                    "label": "EasyEcom Location (managed)",
                    "fieldtype": "Link",
                    "options": "EasyEcom Location",
                    "insert_after": "address_title",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "description": (
                        "Set when this Address is mirrored from an "
                        "EasyEcom Location. Address fields lock on "
                        "the form — edit the Location, then re-save "
                        "to push changes back."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
