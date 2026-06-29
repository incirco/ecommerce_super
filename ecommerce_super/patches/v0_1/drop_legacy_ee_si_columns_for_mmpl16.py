"""TEMPORARY mmpl16 cleanup — drop 11 legacy ee_* Custom Fields on
tabSales Invoice. They date to a previous ad-hoc EasyEcom integration
attempt on this site (last write 2026-05-26) and are now both
(a) duplicated by our ecs_easyecom_* model and (b) eating row-size
budget that blocks the §12 b2c patch ALTER.

**Intended to be reverted from main after the one-shot mmpl16 deploy.**
The patch file + patches.txt entry should be removed via `git revert`
so this never runs on other client benches.

Per the user's 2026-06-29 decision: the 713 rows of historical EE-
integration data carried by ee_invoice_id / ee_order_id / etc. are
not needed. Dropping all 11 ee_* frees ~6.1 KB of in-row headroom —
comfortably unblocks the b2c patch with room to spare for future SI
columns. source_customer / target_customer are intentionally NOT
included per the same user instruction (preserved for now).

Fields dropped (column drops batched in one ALTER for efficiency):

  ee_invoice_id        713 rows  | DATA LOSS — duplicates ecs_easyecom_invoice_id
  ee_invoice_number    507 rows  | DATA LOSS — duplicates ecs_easyecom_invoice_number
  ee_order_id          713 rows  | DATA LOSS — duplicates ecs_easyecom_order_id
  ee_order_status      713 rows  | DATA LOSS — different enum from ecs_easyecom_dispatch_status
  ee_order_date        713 rows  | DATA LOSS — close to si.posting_date
  ee_payment_mode      713 rows  | DATA LOSS — duplicates ecs_payment_mode
  ee_awb_number        457 rows  | DATA LOSS — duplicates ecs_awb_number
  ee_courier           713 rows  | DATA LOSS — duplicates ecs_courier
  ee_carrier_id        713 rows  | DATA LOSS — no equivalent
  ee_reference_code    713 rows  | DATA LOSS — duplicates ecs_marketplace_order_id
  ee_po_id               0 rows  | empty — likely a 3rd-party app remnant

Defensive:
- Conditional on column existence (safe no-op on sites that don't have
  these columns — puresta-uat, puresta.local, Harmony, any client bench)
- Per-field tolerant (continues even if some columns are missing)
- Idempotent re-runs are clean no-ops
- Cleans Property Setter rows referencing each field (orphans otherwise)
- Cleans Custom Field rows where they exist (ee_po_id has no CF row —
  attributed to 'core' in the audit; DELETE is a harmless no-op for it)

Same temporary-revert-after-deploy workflow as PR #117.
"""
from __future__ import annotations

import frappe


_DOCTYPE = "Sales Invoice"
_TABLE = "tabSales Invoice"
_TO_DROP = (
    # Legacy ad-hoc EasyEcom integration (user-confirmed data loss OK)
    "ee_invoice_id",
    "ee_invoice_number",
    "ee_order_id",
    "ee_order_status",
    "ee_order_date",
    "ee_payment_mode",
    "ee_awb_number",
    "ee_courier",
    "ee_carrier_id",
    "ee_reference_code",
    "ee_po_id",
)


def execute() -> None:
    rows = frappe.db.sql(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
        "AND COLUMN_NAME IN %s",
        (_TABLE, _TO_DROP),
        as_dict=True,
    )
    existing = {r.COLUMN_NAME for r in rows}
    if not existing:
        frappe.logger().info(
            f"[ecs mmpl cleanup] No legacy columns found on {_TABLE} "
            "— skipping (this site is clean)."
        )
        return

    # 1. Clean Property Setter rows referencing each field
    for f in existing:
        frappe.db.sql(
            "DELETE FROM `tabProperty Setter` "
            "WHERE doc_type=%s AND field_name=%s",
            (_DOCTYPE, f),
        )

    # 2. Clean Custom Field rows (no-op for columns lacking a CF row,
    #    e.g. ee_po_id which was attributed to 'core' in the audit)
    for f in existing:
        frappe.db.sql(
            "DELETE FROM `tabCustom Field` "
            "WHERE dt=%s AND fieldname=%s",
            (_DOCTYPE, f),
        )

    # 3. Single ALTER to drop all extant columns (deterministic order
    #    for predictable logs)
    sorted_existing = sorted(existing)
    drop_clauses = ", ".join(f"DROP COLUMN `{f}`" for f in sorted_existing)
    frappe.db.sql_ddl(f"ALTER TABLE `{_TABLE}` {drop_clauses}")

    frappe.db.commit()
    frappe.logger().info(
        f"[ecs mmpl cleanup] Dropped {len(sorted_existing)} legacy "
        f"columns from {_TABLE}: {sorted_existing}"
    )
