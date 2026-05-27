"""Refresh the EasyEcom workspace from its shipped JSON (§8f Stage 6
Supplier-Map worklist additions).

Same shape as refresh_easyecom_workspace_s8e_customer.py — adds:
  - Suppliers in Drift / Created-Flagged / Flagged-Not-Created
    Number Cards into the FDE Worklist row + the number_cards array.
  - Supplier Map sub-link under Masters Card Break.
  - Suppliers - Drift / Created-Flagged / Flagged-Not-Created link
    entries under FDE Worklists Card Break.
  - Supplier Map shortcut on the Masters status panel.

Frappe's migrate doesn't re-import an existing workspace's content
arrays (to preserve in-desk customisations), so this patch forces
a one-shot reload after the §8f Stage 6 JSON edits land.

Idempotent: import_file_by_path with force=True is safe to re-run.
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
        "(§8f Stage 6: Supplier Map worklist cards + links)"
    )
