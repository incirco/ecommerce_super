"""§12 deployment fix — convert heavy tables to ROW_FORMAT=DYNAMIC.

MariaDB / InnoDB COMPACT row format has a hard 65535-byte in-row size
limit. Heavy production benches with many installed apps hit this when
our patches try to add columns. The fix is to convert to DYNAMIC
(or COMPRESSED as fallback), which stores long varchar columns off-
page with only a 20-byte pointer in the row.

Per PR #114 — each SI-column-adding patch also calls
ensure_dynamic_row_format inline as a self-healing pre-flight, so this
standalone patch is no longer load-bearing for the per-table conversion.
Kept as a forward-defense pass for Sales Order + Customer (which
don't have dedicated SI-style patches yet).

Idempotent — re-runs on already-DYNAMIC tables are a no-op.
"""
from __future__ import annotations

from ecommerce_super.easyecom._schema_utils import (
    ensure_dynamic_row_format_for_heavy_tables,
)


def execute() -> None:
    results = ensure_dynamic_row_format_for_heavy_tables()
    import frappe
    for table, result in results.items():
        frappe.logger().info(f"[ecs row-format] {table}: {result}")
