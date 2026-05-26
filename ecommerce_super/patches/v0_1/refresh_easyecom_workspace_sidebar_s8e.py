"""Refresh the EasyEcom Workspace Sidebar from JSON — §8e Stage 6
Customer Map additions.

Mirror of refresh_easyecom_workspace_sidebar.py — re-imports the
on-disk JSON so the new Customer Map sub-link (under Masters) and
the 3 Customers - * worklist sub-links (under FDE Worklists) land.

Frappe v16 ships the `Workspace Sidebar` DocType — distinct from
the standard `Workspace` DocType — and uses it as the actual source
of truth for the desk left rail. Stage 6's Workspace JSON update
didn't carry over because the sidebar reads from this other
DocType. This patch wires the Customer entries into the right
place.

Idempotent: import_file_by_path(force=True) is safe to re-run.
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
    print(
        "[ecommerce_super] refreshed EasyEcom Workspace Sidebar "
        "(§8e Stage 6: Customer Map + Customer worklist sub-links)"
    )
