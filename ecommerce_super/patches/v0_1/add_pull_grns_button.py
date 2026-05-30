"""Add 'Pull GRNs Now' button on the EasyEcom Account form.

GROUNDING CORRECTION (live Harmony smoke 2026-05-30): until now the
GRN pull only ran via the daily scheduler. FDEs need to be able to
trigger it manually after they process a GRN on the EE UI to see the
inbound chain on ERPNext side. Mirrors §9's push_all_pending_pos_action
button.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "EasyEcom Account": [
                {
                    "fieldname": "pull_all_pending_grns_action",
                    "label": "Pull GRNs Now",
                    "fieldtype": "Button",
                    "insert_after": "grn_pull_total_seen",
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
