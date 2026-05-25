"""Ensure the UNIQUE (tax_rule_name, company) index exists on
tabEasyEcom Tax Rule Map (§8.5.3 natural key).

install.py declares the index in its COMPOSITE_INDEXES list, but
after_install only fires once at first install. Existing sites
that already had `bench migrate` run before 8c add the DocType
without the index. This patch installs it idempotently.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    table = "tabEasyEcom Tax Rule Map"
    index_name = "uq_tax_rule_map_rule_company"
    columns = "tax_rule_name, company"

    if not frappe.db.sql(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (table,),
    ):
        return
    if frappe.db.sql(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s "
        "LIMIT 1",
        (table, index_name),
    ):
        return
    try:
        frappe.db.sql(
            f"CREATE UNIQUE INDEX `{index_name}` ON `{table}` ({columns})"
        )
        frappe.db.commit()
        print(
            f"[ecommerce_super] created UNIQUE index {index_name} on {table} "
            "(§8.5.3 — natural key (tax_rule_name, company))"
        )
    except Exception as e:
        frappe.log_error(
            title=f"add_tax_rule_map_unique_index: could not create {index_name}",
            message=f"{type(e).__name__}: {e}",
        )
