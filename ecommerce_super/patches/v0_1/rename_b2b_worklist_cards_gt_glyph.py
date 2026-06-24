"""gh#79 — rename §11 worklist Number Cards that shipped with `>`
in their names (`(>24h)` / `(>2h)`).

Symptom (mmpl16.frappe.cloud 2026-06-23): migrate from `de1b54e` to
`7f77b90` ran every patch successfully, then aborted during
`sync_dashboards()` with
    `frappe.exceptions.NameError: Name cannot contain special
     characters like '<', '>'`.

Root cause: two §11 Phase 1 Stage 3 worklist cards shipped with
`name = "B2B orders awaiting invoice (>24h)"` and
`name = "New B2B orders missing IDs (>2h)"`. Frappe's `validate_name`
(frappe/model/naming.py:510) rejects `<` and `>` in record names, so
the dashboard sync aborted before any §11 card landed.

The shipped JSON files now use ASCII-safe alternatives (`(24h+)`,
`(2h+)`). For any bench that already imported the original bad-named
rows in an older Frappe version (where `validate_name` was lenient),
or that has a stale row left over from a partial sync, this patch
renames the rows in-place. Idempotent — no-op on a clean bench.
"""
from __future__ import annotations

import frappe


# Old → New mapping, applied in BOTH `name` and `label` columns of
# `tabNumber Card`. Order matters only for readability; both pairs
# are independent.
_RENAMES = (
    ("B2B orders awaiting invoice (>24h)",
     "B2B orders awaiting invoice (24h+)"),
    ("New B2B orders missing IDs (>2h)",
     "New B2B orders missing IDs (2h+)"),
)


def execute() -> None:
    if not frappe.db.table_exists("Number Card"):
        return
    for old_name, new_name in _RENAMES:
        if not frappe.db.exists("Number Card", old_name):
            continue
        # Use Frappe's rename so any downstream references (workspace
        # links, dashboard back-refs) follow. `force=True` skips the
        # name-validation gate that would block re-entering the same
        # bad character — irrelevant here since the new name is
        # ASCII-safe, but harmless.
        try:
            frappe.rename_doc(
                "Number Card", old_name, new_name,
                force=True, merge=False,
            )
            # Also fix the label column on the renamed row in case it
            # was set separately (some old fixtures only matched on
            # name and missed label).
            frappe.db.set_value(
                "Number Card", new_name, "label", new_name,
                update_modified=False,
            )
        except Exception as exc:
            # Don't block migrate on a per-row rename failure — log
            # and continue so the broader migrate can finish.
            frappe.log_error(
                title=f"gh#79 rename {old_name!r} failed",
                message=f"{type(exc).__name__}: {exc}",
            )
    frappe.db.commit()
