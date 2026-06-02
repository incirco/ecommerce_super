"""Migrate Sync Record status=Discrepancy → Failed (gh#16, SPEC §7.3 M1).

The Sync Record status enum is now binary (Pending / Running / Success /
Failed / Cancelled / AlreadySynced). The legacy "Discrepancy" value is
removed from the DocType options. Existing rows carrying status =
"Discrepancy" would fail validation on next save — back-fill them to
"Failed" and annotate last_error with the migration note so the FDE can
trace the original semantic.

Idempotent: rows already at "Failed" are untouched; the WHERE clause
selects only the legacy value. Safe to re-run.
"""

from __future__ import annotations

import frappe


_LEGACY_STATUS = "Discrepancy"
_NEW_STATUS = "Failed"
_ANNOTATION_PREFIX = "[gh#16 migrated from Discrepancy] "


def execute() -> None:
    rows = frappe.db.sql(
        """
        SELECT name, COALESCE(last_error, '') AS last_error
        FROM `tabEasyEcom Sync Record`
        WHERE status = %s
        """,
        (_LEGACY_STATUS,),
        as_dict=True,
    )
    if not rows:
        return

    for row in rows:
        prior_err = row["last_error"] or ""
        annotated = (
            _ANNOTATION_PREFIX + (prior_err if prior_err else "(no prior last_error)")
        )
        frappe.db.set_value(
            "EasyEcom Sync Record",
            row["name"],
            {
                "status": _NEW_STATUS,
                "last_error": annotated,
            },
            update_modified=False,
        )

    frappe.db.commit()
    print(
        f"[ecommerce_super] gh#16: migrated {len(rows)} Sync Record row(s) "
        f"from status={_LEGACY_STATUS!r} → {_NEW_STATUS!r}"
    )
