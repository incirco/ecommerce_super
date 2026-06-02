"""Refresh the EasyEcom workspace to add the API Calls shortcut (gh#18).

The workspace JSON gains a new shortcut row (Blue / "API Calls" → list
of EasyEcom API Call) so the FDE can reach the API Call log directly
from the workspace's top-row shortcuts — previously it was nested
under the Runtime Logs Card Break and easy to miss.

Frappe's normal migrate doesn't re-import an existing workspace's
shortcuts array (preserves desk customisations). One-shot patch
forces a reload of the shipped JSON. Idempotent — re-running on a
site that already matches the JSON is a no-op.
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
    print("[ecommerce_super] gh#18: refreshed EasyEcom workspace (API Calls shortcut)")
