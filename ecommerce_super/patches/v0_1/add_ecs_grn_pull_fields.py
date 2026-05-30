"""§9 Stage 3 — GRN pull back-ref custom fields.

Two surfaces:
  - Purchase Receipt: ecs_easyecom_grn_id (Data, indexed; idempotency
    hinge on re-pull) + ecs_supplier_invoice_date (Date; from EE's
    grn_invoice_date — separate from ERPNext's posting_date).
  - Purchase Receipt Item: ecs_easyecom_grn_detail_id (Data, indexed;
    line back-ref so a per-line discrepancy can name the EE-side line)
    + ecs_easyecom_po_detail_id (Data, indexed; PO line back-ref so
    cumulative receipt-vs-ordered tolerance is per-PO-line, not per-PR).

ecs_easyecom_grn_id is NOT marked unique at the column level — the §9
flow enforces idempotency by querying existing PRs with this back-ref
filled. Frappe's Custom Field doesn't support per-Company composite
unique constraints; a single-column unique here would break multi-
Company sites that legitimately share a GRN id across Companies (rare
but possible during transition).

Idempotent — create_custom_fields skips fields that already exist.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Purchase Receipt": [
                # ecs_easyecom_grn_id — EasyEcom-side `grn_id` (int as
                # string). Set by §9 Stage 3 pull; idempotency hinge so
                # a re-pull of the same GRN does NOT create a second PR.
                {
                    "fieldname": "ecs_easyecom_grn_id",
                    "label": "EasyEcom GRN ID",
                    "fieldtype": "Data",
                    "insert_after": "supplier_delivery_note",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                },
                # ecs_supplier_invoice_date — EasyEcom-side
                # `grn_invoice_date`. Distinct from ERPNext's posting_date
                # (which reflects when PR was created on ERPNext, typically
                # the GRN pull tick). Kept for invoice reconciliation.
                {
                    "fieldname": "ecs_supplier_invoice_date",
                    "label": "Supplier Invoice Date (from EE)",
                    "fieldtype": "Date",
                    "insert_after": "ecs_easyecom_grn_id",
                    "read_only": 1,
                },
            ],
            "Purchase Receipt Item": [
                # ecs_easyecom_grn_detail_id — EasyEcom-side
                # `grn_detail_id`. Per-line back-ref tying a PR line to a
                # specific EE GRN line. Used by Stage 3 line-level
                # discrepancy logging.
                {
                    "fieldname": "ecs_easyecom_grn_detail_id",
                    "label": "EasyEcom GRN Detail ID",
                    "fieldtype": "Data",
                    "insert_after": "purchase_order_item",
                    "read_only": 1,
                    "search_index": 1,
                },
                {
                    "fieldname": "ecs_easyecom_po_detail_id",
                    "label": "EasyEcom PO Detail ID",
                    "fieldtype": "Data",
                    "insert_after": "ecs_easyecom_grn_detail_id",
                    "read_only": 1,
                    "search_index": 1,
                    "description": (
                        "EasyEcom-side `purchase_order_detail_id` — "
                        "the PO line this PR line satisfies. Cumulative "
                        "received-vs-ordered tolerance computed per "
                        "PO-line via this back-ref."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
    print("[ecommerce_super] ensured §9 Stage 3 GRN pull back-ref fields exist")
