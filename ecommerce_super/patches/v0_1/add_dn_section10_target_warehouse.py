"""Add `ecs_section10_target_warehouse` custom field on Delivery Note.

GROUNDING CORRECTION (live Harmony smoke 2026-05-29): §10's substrate
needs to route stock through the source Company's Goods-In-Transit
warehouse on DN submit, not deposit it directly at the final
destination. ERPNext's internal-customer DN convention uses
`items[].target_warehouse` as the in-transit holding bay; the §10
substrate was reading it as the FINAL destination, which left the
GIT bin negative and the final warehouse double-credited.

The fix: validate_pre_submit captures the FDE's intended final target
on `ecs_section10_target_warehouse` (DN doc-level field), then
overrides each line's `target_warehouse` to the source Company's GIT.
Substrate-side EE Location lookups read the captured intended target
from this field instead of from the now-coerced item line.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Delivery Note": [
                {
                    "fieldname": "ecs_section10_target_warehouse",
                    "label": "Section 10 Final Target Warehouse",
                    "fieldtype": "Link",
                    "options": "Warehouse",
                    "insert_after": "set_target_warehouse",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 0,
                    "hidden": 0,
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
