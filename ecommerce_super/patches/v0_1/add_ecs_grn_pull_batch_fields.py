"""§9 Stage 3 — EE-supplied batch fields on Purchase Receipt Item.

Live finding 2026-05-28 on Harmony GRN 2115504: EE supplied
`batch_code: "b23121"` + `expire_date: "2026-07-31"` on a GRN line
for an Item where ERPNext's `has_batch_no=0`. Our flow's batch
handler gates on `Item.has_batch_no` (correctly — ERPNext rejects
`batch_no` on a non-batch Item), so the EE batch data was being
silently dropped.

Two custom fields on Purchase Receipt Item:
  - ecs_ee_batch_code  (Data, indexed) — always captured from EE
                       regardless of Item.has_batch_no
  - ecs_ee_expire_date (Date) — paired with ecs_ee_batch_code

When `Item.has_batch_no=1` the flow ALSO creates the native Batch +
sets `PR Item.batch_no` (existing behavior unchanged). The custom
fields are populated in addition, providing per-line back-ref
visibility into what EE sent. When the Item is NOT batch-managed,
only the custom fields are populated and a Discrepancy is raised
(`kind="batch_code on non-batch item"`) so the FDE sees the data
mismatch and can either enable batch tracking retroactively or
accept the loss for this PR.

Idempotent — create_custom_fields skips fields that already exist.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Purchase Receipt Item": [
                {
                    "fieldname": "ecs_ee_batch_code",
                    "label": "EE Batch Code",
                    "fieldtype": "Data",
                    "insert_after": "ecs_easyecom_po_detail_id",
                    "read_only": 1,
                    "search_index": 1,
                    "description": (
                        "EasyEcom-side `batch_code` from the GRN line. "
                        "Always captured (even when Item.has_batch_no=0). "
                        "When the Item IS batch-managed, the native "
                        "batch_no field is ALSO populated and pointed at "
                        "the auto-created Batch doc."
                    ),
                },
                {
                    "fieldname": "ecs_ee_expire_date",
                    "label": "EE Expire Date",
                    "fieldtype": "Date",
                    "insert_after": "ecs_ee_batch_code",
                    "read_only": 1,
                    "description": (
                        "EasyEcom-side `expire_date` from the GRN line. "
                        "Paired with ecs_ee_batch_code."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
    print("[ecommerce_super] ensured §9 Stage 3 batch back-ref fields exist")
