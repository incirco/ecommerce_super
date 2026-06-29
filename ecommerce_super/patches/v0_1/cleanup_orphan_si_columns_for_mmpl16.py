"""TEMPORARY mmpl16 cleanup — drop 2 orphan Custom Fields on tabSales
Invoice that are eating row-size budget and blocking the §12 b2c patch.

**Intended to be reverted from main after the one-shot mmpl16 deploy.**
The patch file + patches.txt entry should be removed via `git revert`
so this never runs on other client benches.

Defensive: the patch is CONDITIONAL — only acts when each candidate
column (a) exists in the DB AND (b) has POPULATED=0. Safe no-op on
puresta-uat / puresta.local / Harmony / any other site that doesn't
carry these orphans, OR on mmpl16 if the columns somehow gained data
between verify and execute.

Context — audit run on mmpl16 2026-06-29 (after PRs #115/#116 deployed):
- tabSales Invoice on DYNAMIC row format, 232 columns, 64021/65535
  bytes used → only 1514 bytes headroom
- add_b2c_sales_invoice_fields needs ~2358 bytes (4 varchar(140) Link/Data
  + 3 decimal + 1 date + 1 datetime + 3 Long Text pointers) — deficit ~844 bytes
- The 41 'custom:?' fields on this site eat 15.5 KB, most from a previous
  ad-hoc EE integration (ee_*) and an older marketplace customer model
  (market_place_customer*). Only `source_customer` and `target_customer`
  are POPULATED=0 — the rest carry real legacy data and need backfill
  before they can be dropped (separate cleanup project).

Frees 2 × varchar(140) ≈ 1124 in-row bytes — enough to unblock b2c
(1124 > 844). Other ee_* / market_place_customer* cleanup is deferred.
"""
from __future__ import annotations

import frappe


_DOCTYPE = "Sales Invoice"
_TABLE = "tabSales Invoice"
_CANDIDATES = ("source_customer", "target_customer")


def execute() -> None:
    # 1. Filter to columns that actually exist on this site
    rows = frappe.db.sql(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
        "AND COLUMN_NAME IN %s",
        (_TABLE, _CANDIDATES),
        as_dict=True,
    )
    existing = {r.COLUMN_NAME for r in rows}
    if not existing:
        frappe.logger().info(
            "[ecs mmpl cleanup] No orphan columns found on "
            f"{_TABLE} — skipping (this site is clean)."
        )
        return

    # 2. Safety re-check per field — skip any with unexpected data
    to_drop: list[str] = []
    for f in _CANDIDATES:
        if f not in existing:
            continue
        cnt = frappe.db.sql(
            f"SELECT COUNT(*) FROM `{_TABLE}` "
            f"WHERE `{f}` IS NOT NULL AND `{f}` != ''"
        )[0][0]
        if cnt > 0:
            frappe.logger().warning(
                f"[ecs mmpl cleanup] SKIPPING {f}: populated={cnt} "
                "— field gained unexpected data, not dropping."
            )
            continue
        to_drop.append(f)

    if not to_drop:
        return

    # 3. Clean Property Setters referencing these fields
    for f in to_drop:
        frappe.db.sql(
            "DELETE FROM `tabProperty Setter` "
            "WHERE doc_type=%s AND field_name=%s",
            (_DOCTYPE, f),
        )

    # 4. Clean Custom Field rows
    for f in to_drop:
        frappe.db.sql(
            "DELETE FROM `tabCustom Field` "
            "WHERE dt=%s AND fieldname=%s",
            (_DOCTYPE, f),
        )

    # 5. Drop the columns (single ALTER for efficiency)
    drop_clauses = ", ".join(f"DROP COLUMN `{f}`" for f in to_drop)
    frappe.db.sql_ddl(f"ALTER TABLE `{_TABLE}` {drop_clauses}")

    frappe.db.commit()
    frappe.logger().info(
        f"[ecs mmpl cleanup] Dropped orphan columns from {_TABLE}: {to_drop}"
    )
