"""Refresh the EasyEcom workspace + sidebar (§10 Stage 4 worklist).

Mirrors refresh_easyecom_workspace_s9_buying.py. Adds:
  - Stock Transfers sub-header + 3 number cards (Drift / EE-originated /
    Late GRN after submitted DN) to the workspace content + number_cards
    array.
  - Transfer Map link under Masters Card Break in the sidebar.
  - 3 worklist link entries (Drift / EE-originated / Late GRN) under
    FDE Worklists Card Break.

Frappe's migrate doesn't re-import an existing workspace's content
arrays (to preserve in-desk customisations), so this patch forces
a one-shot reload after the §10 Stage 4 JSON edits land.

Idempotent: import_file_by_path with force=True is safe to re-run.
"""

from __future__ import annotations

from pathlib import Path

import frappe


def execute() -> None:
    from frappe.modules.import_file import import_file_by_path

    workspace_path = Path(
        frappe.get_app_path(
            "ecommerce_super",
            "easyecom", "workspace", "easyecom", "easyecom.json",
        )
    )
    sidebar_path = Path(
        frappe.get_app_path(
            "ecommerce_super",
            "workspace_sidebar", "easyecom.json",
        )
    )
    for path in (workspace_path, sidebar_path):
        if path.exists():
            import_file_by_path(
                str(path), force=True, reset_permissions=False
            )
    frappe.db.commit()
    print(
        "[ecommerce_super] refreshed EasyEcom workspace + sidebar "
        "from JSON (§10 Stage 4: Stock Transfer row — Drift / "
        "EE-originated / Late-GRN cards + worklist links)"
    )
