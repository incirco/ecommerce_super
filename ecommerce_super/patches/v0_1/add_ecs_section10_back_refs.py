"""§10 Stage 1 — Transfer Map back-ref custom fields on related DocTypes.

Mirrors §9's add_ecs_grn_pull_fields.py pattern. The `ecs_*` fixture
filter in hooks.py auto-captures these into the export, so adding the
fields via patch is sufficient for both initial install and re-deploys.

Surfaces:
  - Delivery Note: the outbound anchor (always set when §10 is in play).
  - Sales Invoice: populated only for different-GSTIN transfers (auto-
    drafted in Stage 2).
  - Purchase Receipt: each IPR row (one per GRN against the transfer).
  - Purchase Invoice: covers both IPI and the Debit Note (ERPNext models
    Debit Note as a PI with is_return=1, so one field handles both).

Read-only + hidden by default — the flow code writes them; humans
don't. Idempotent via create_custom_fields.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Delivery Note": [
                {
                    "fieldname": "ecs_section10_transfer_map",
                    "label": "EasyEcom §10 Transfer Map",
                    "fieldtype": "Link",
                    "options": "EasyEcom Transfer Map",
                    "insert_after": "is_internal_customer",
                    "read_only": 1,
                    "hidden": 1,
                    "description": (
                        "§10 back-ref. Set by Stage 2's DN.on_submit "
                        "hook when the DN enters the §10 outbound flow."
                    ),
                },
            ],
            "Sales Invoice": [
                {
                    "fieldname": "ecs_section10_transfer_map",
                    "label": "EasyEcom §10 Transfer Map",
                    "fieldtype": "Link",
                    "options": "EasyEcom Transfer Map",
                    "insert_after": "is_internal_customer",
                    "read_only": 1,
                    "hidden": 1,
                    "description": (
                        "§10 back-ref. Set by Stage 2 when the "
                        "Internal SI is auto-drafted (different-GSTIN "
                        "transfers only)."
                    ),
                },
            ],
            "Purchase Receipt": [
                {
                    "fieldname": "ecs_section10_transfer_map",
                    "label": "EasyEcom §10 Transfer Map",
                    "fieldtype": "Link",
                    "options": "EasyEcom Transfer Map",
                    "insert_after": "is_internal_supplier",
                    "read_only": 1,
                    "hidden": 1,
                    "description": (
                        "§10 back-ref. Set by Stage 3's GRN-pull "
                        "branch when an IPR is auto-created against a "
                        "§10 transfer. Independent from §9's "
                        "ecs_easyecom_grn_id back-ref (§9 PRs are "
                        "buying-side; §10 PRs are inbound internal "
                        "transfer receipts)."
                    ),
                },
            ],
            "Purchase Invoice": [
                {
                    "fieldname": "ecs_section10_transfer_map",
                    "label": "EasyEcom §10 Transfer Map",
                    "fieldtype": "Link",
                    "options": "EasyEcom Transfer Map",
                    "insert_after": "is_internal_supplier",
                    "read_only": 1,
                    "hidden": 1,
                    "description": (
                        "§10 back-ref. Covers both IPI (auto-drafted "
                        "for full ITC claim, mirroring submitted SI) "
                        "and Debit Note (auto-drafted for the "
                        "dispatched−received gap; ERPNext models "
                        "Debit Note as a Purchase Invoice with "
                        "is_return=1). The Transfer Map's "
                        "internal_purchase_invoice and "
                        "draft_debit_note fields disambiguate which "
                        "PI is which from the §10 side."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
    print(
        "[ecommerce_super] ensured §10 Stage 1 Transfer Map back-ref "
        "fields exist on Delivery Note / Sales Invoice / Purchase "
        "Receipt / Purchase Invoice"
    )
