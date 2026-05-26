"""Refresh the EasyEcom Workspace Sidebar from JSON (§17 layer +
post-§8d structural updates).

Frappe v16 ships a `Workspace Sidebar` DocType — a STANDARD,
read-only-from-the-desk sidebar definition that overrides the
workspace's own links rendering. The old workspace_sidebar/easyecom.json
shipped pre-§8d / pre-§17 with stale labels ("Control Panel /
Configuration / Operations / Masters" with only a fraction of the
actual DocTypes). The JSON has been rewritten to match the
workspace's 5-Card-Break structure (Setup / Masters /
FDE Worklists / Operations / Runtime Logs) and to include the
six FDE worklist URLs.

Frappe's migrate does NOT auto-re-import an existing Workspace
Sidebar on JSON updates (same behaviour as workspace.json).
This patch forces the refresh. Idempotent —
import_file_by_path(force=True) is safe to re-run.
"""

from __future__ import annotations

from pathlib import Path

import frappe


def execute() -> None:
    from frappe.modules.import_file import import_file_by_path

    json_path = Path(
        frappe.get_app_path(
            "ecommerce_super",
            "workspace_sidebar", "easyecom.json",
        )
    )
    if not json_path.exists():
        return
    import_file_by_path(str(json_path), force=True, reset_permissions=False)
    frappe.db.commit()
    print("[ecommerce_super] refreshed EasyEcom Workspace Sidebar from JSON")
