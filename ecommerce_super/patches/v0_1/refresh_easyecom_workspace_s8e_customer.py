"""Refresh the EasyEcom workspace from its shipped JSON (§8e Stage 6
Customer-Map worklist additions).

Why a separate patch: Frappe's migrate does NOT re-import an existing
workspace's content / number_cards / links / charts arrays — to
preserve in-desk customisations. §8e Stage 6 adds 3 Customer Map
worklist Number Cards + Masters/Worklist link entries + 3 content
blocks; on a fresh install the JSON loads natively; on an existing
install (one that came through prior §17 / Stage 6 work) the JSON
is ignored. This patch forces a one-shot reload.

Idempotent: import_file_by_path with force=True is safe to re-run.
Sibling of refresh_easyecom_workspace_s17 / _sidebar.
"""

from __future__ import annotations

from pathlib import Path

import frappe


def execute() -> None:
    from frappe.modules.import_file import import_file_by_path

    json_path = Path(
        frappe.get_app_path(
            "ecommerce_super",
            "easyecom", "workspace", "easyecom", "easyecom.json",
        )
    )
    if not json_path.exists():
        return
    import_file_by_path(str(json_path), force=True, reset_permissions=False)
    frappe.db.commit()
    print(
        "[ecommerce_super] refreshed EasyEcom workspace from JSON "
        "(§8e Stage 6: Customer Map worklist cards + links)"
    )
