"""Schema-level utilities for Custom Field patches.

The MariaDB InnoDB row-size limit (65535 bytes for COMPACT format)
hits production benches with many installed apps when our patches
try to add columns to already-fat tables like Sales Invoice. The
fix is to convert to ROW_FORMAT=DYNAMIC (or COMPRESSED as fallback),
which stores long varchar columns off-page with only a 20-byte
pointer in the row.

This module exposes `ensure_dynamic_row_format(table)` — call it at
the top of any patch that adds columns to a heavy table. Idempotent,
self-healing, and survives any patches.txt ordering issue. Each
SI-column-adding patch is now responsible for its own pre-flight
instead of depending on a separate patch running first.

Surfaced 2026-06-29 on:
- puresta-uat.m.frappe.cloud (failed at add_b2c_sales_invoice_fields)
- mmpl16.frappe.cloud (failed at add_b2b_mode2_sales_invoice_fields,
  even after PR #112 / #113 reordered patches.txt — heavier bench,
  fails earlier in the chain)
"""
from __future__ import annotations

import frappe


def ensure_dynamic_row_format(table: str) -> dict:
    """Make sure `table` is on ROW_FORMAT=DYNAMIC. If DYNAMIC isn't
    enough for the impending ALTER, escalate to COMPRESSED.

    Returns a small dict describing what was done — caller can log.

    Idempotent: re-running on an already-DYNAMIC table is a no-op.
    Per-table failures are logged + the function returns; never raises
    (we don't want pre-flight to be MORE fragile than the original).
    """
    out: dict = {"table": table, "starting_format": None, "final_format": None}

    try:
        status = frappe.db.sql(
            f"SHOW TABLE STATUS WHERE Name='{table}'", as_dict=True
        )
        if not status:
            out["error"] = f"Table {table!r} not found"
            return out
        out["starting_format"] = status[0].get("Row_format")
    except Exception as e:
        out["error"] = f"status query failed: {type(e).__name__}: {e}"
        return out

    if (out["starting_format"] or "").lower() in ("dynamic", "compressed"):
        out["action"] = "already_dynamic_or_compressed"
        out["final_format"] = out["starting_format"]
        return out

    # Try DYNAMIC first
    try:
        frappe.db.sql_ddl(f"ALTER TABLE `{table}` ROW_FORMAT=DYNAMIC")
        frappe.logger().info(
            f"[ecs schema_utils] Converted {table} to ROW_FORMAT=DYNAMIC."
        )
        out["action"] = "converted_to_dynamic"
    except Exception as e:
        # DYNAMIC failed (rare; usually permissions / config) — try COMPRESSED
        out["dynamic_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            frappe.db.sql_ddl(f"ALTER TABLE `{table}` ROW_FORMAT=COMPRESSED")
            frappe.logger().info(
                f"[ecs schema_utils] Fallback: converted {table} to "
                f"ROW_FORMAT=COMPRESSED (DYNAMIC was rejected)."
            )
            out["action"] = "converted_to_compressed_fallback"
        except Exception as e2:
            out["compressed_error"] = f"{type(e2).__name__}: {str(e2)[:200]}"
            return out

    # Verify
    try:
        status = frappe.db.sql(
            f"SHOW TABLE STATUS WHERE Name='{table}'", as_dict=True
        )
        out["final_format"] = status[0].get("Row_format") if status else "(unknown)"
    except Exception:
        pass

    return out


def ensure_dynamic_row_format_for_heavy_tables() -> dict:
    """Pre-flight all SI / SO / Customer tables to DYNAMIC. Called
    by the convert_sales_invoice_to_dynamic_row_format patch and
    safe to call from any SI-column-adding patch as an upfront guard.
    """
    results = {}
    for t in ("tabSales Invoice", "tabSales Order", "tabCustomer"):
        results[t] = ensure_dynamic_row_format(t)
    return results
