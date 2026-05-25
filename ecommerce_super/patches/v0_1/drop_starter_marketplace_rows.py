"""Drop the §5-era 'starter-*' Marketplace rows.

The 8b refactor promoted Marketplace.marketplace_id from Data to Int
(per §31.2.18). The starter seed previously shipped string keys like
'starter-amazon-in' that won't reconcile against the real numeric
ids from EE's /current-channel-status (Flipkart=2, Amazon.in=8, etc.).
The seed fixture now ships those same well-known channels with their
real numeric ids (2, 8, 60, 100, 122). This patch removes the
orphaned string-keyed rows on sites that received the old fixture.

Idempotent: skips silently if the rows are already gone.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.table_exists("Marketplace"):
        return
    # Drop any Marketplace row whose docname starts with the starter-*
    # prefix. We avoid filtering on marketplace_id directly because the
    # column type has changed (Data → Int) and the old string values
    # would no longer be addressable via a `("like", ...)` filter.
    rows = frappe.db.sql(
        """SELECT name FROM `tabMarketplace` WHERE name LIKE 'starter-%%'""",
        as_dict=True,
    )
    if not rows:
        return
    for row in rows:
        try:
            frappe.delete_doc(
                "Marketplace", row.name, force=True, ignore_permissions=True
            )
        except Exception as exc:  # noqa: BLE001 — log + continue
            frappe.log_error(
                title=f"drop_starter_marketplace_rows: could not delete {row.name}",
                message=f"{type(exc).__name__}: {exc}",
            )
    frappe.db.commit()
    print(
        f"[ecommerce_super] dropped {len(rows)} orphaned starter-* "
        "Marketplace row(s) (§8b refactor: marketplace_id is now Int; "
        "the seed ships real EE ids — Flipkart=2, Amazon.in=8, "
        "TaTa Cliq=60, Customer Cash Sales=100, meesho=122)"
    )
