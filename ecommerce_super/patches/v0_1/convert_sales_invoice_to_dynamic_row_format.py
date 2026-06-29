"""§12 deployment fix — convert `tabSales Invoice` to ROW_FORMAT=DYNAMIC.

MariaDB / InnoDB COMPACT row format has a hard 65535-byte in-row size
limit. ERPNext + India Compliance + client custom-field apps each
add columns to Sales Invoice; a production-shaped bench often sits
within a few hundred bytes of the limit. Adding the 12 §12 B2C
Custom Fields in one go (7 varchar(140) + 3 decimal + 1 date + 1
datetime) pushes past it and the migrate fails:

  MySQLdb.OperationalError: (1118, 'Row size too large. The maximum
  row size for the used table type, not counting BLOBs, is 65535.')

This patch runs BEFORE add_b2c_sales_invoice_fields and converts the
table to ROW_FORMAT=DYNAMIC. DYNAMIC stores varchar columns off-page
when they overflow, bypassing the 65535 in-page limit. It's the
default for new InnoDB tables in MariaDB 10.2+ / MySQL 5.7+ —
legacy tables created earlier may still be on COMPACT.

Surfaced 2026-06-29 when migrating §12 to puresta-uat.m.frappe.cloud.

Idempotent — if the table is already DYNAMIC, the ALTER is a no-op
(MariaDB returns 0 rows changed). Safe to re-run.

Also applies the same conversion to a few other heavy Frappe tables
that future §12+ fields may hit (Sales Order, Customer) — cheap
forward defense, no behaviour change.
"""
from __future__ import annotations

import frappe


# Tables to convert. Sales Invoice is the immediate cause; the others
# are forward defense for tables that commonly get custom-field-heavy
# in client deployments.
TABLES = (
    "tabSales Invoice",
    "tabSales Order",
    "tabCustomer",
)


def execute() -> None:
    for table in TABLES:
        try:
            frappe.db.sql_ddl(
                f"ALTER TABLE `{table}` ROW_FORMAT=DYNAMIC"
            )
            frappe.logger().info(
                f"[ecs §12 row-format] Converted {table} to DYNAMIC."
            )
        except Exception as exc:
            # Log + continue — table might not exist on a fresh bench
            # (e.g. erpnext not yet installed), OR conversion may fail
            # on rare configurations. Never block migrate on this.
            frappe.logger().warning(
                f"[ecs §12 row-format] Could not convert {table}: "
                f"{type(exc).__name__}: {exc}"
            )
