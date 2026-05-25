"""Create the ecs_cess Custom Field on Item (§8.5.4 step 5 — cess
pass-through from the EE product payload to the Item).

8c's resolver writes product.cess to item.ecs_cess after stamping the
Item Tax Templates from the EasyEcom Tax Rule Map. cess is per-product
on EE's side (not part of the rule map), so it rides separately. 8d
Item sync will read this field at invoice/order time.

Idempotent — `create_custom_fields` skips fields that already exist.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Item": [
                {
                    "fieldname": "ecs_cess",
                    "label": "EasyEcom Cess",
                    "fieldtype": "Currency",
                    "insert_after": "taxes",
                    "default": "0",
                    "read_only": 1,
                    "no_copy": 1,
                    "description": (
                        "EasyEcom per-product cess (from the product payload's "
                        "`cess` field, written by the §8c resolver). Separate "
                        "from the Item Tax Templates (Item.taxes), which are "
                        "GST tax rates. cess is per-product on EE's side, not "
                        "part of the EasyEcom Tax Rule Map. §8.5.4 step 5."
                    ),
                }
            ]
        },
        ignore_validate=True,
    )
    frappe.db.commit()
    print("[ecommerce_super] ensured Custom Field Item.ecs_cess exists (§8c)")
