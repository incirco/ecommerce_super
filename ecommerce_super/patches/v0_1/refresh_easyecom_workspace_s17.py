"""Refresh the EasyEcom workspace from its shipped JSON (§17 layer).

Why a separate patch (not inlined into the workspace JSON's normal
load path): Frappe's migrate does NOT re-import an existing workspace's
content / number_cards / charts arrays — that's deliberate, to
preserve in-desk customisations the FDE may have made.

This packet (§17 operational layer) adds NEW content blocks, number
cards, charts, and a custom HTML block to an already-existing
workspace. On a fresh install the JSON loads natively; on an
existing install the JSON is ignored. This patch forces a one-shot
reload to pick up the §17 additions.

Idempotent: import_file_by_path with force=True is safe to re-run
(it overwrites with the JSON's content; if the JSON hasn't changed,
the row ends up identical).
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
    print("[ecommerce_super] refreshed EasyEcom workspace from JSON (§17 layer)")
