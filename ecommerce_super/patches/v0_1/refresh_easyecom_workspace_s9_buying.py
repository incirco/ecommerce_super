"""Refresh the EasyEcom workspace + sidebar (§9 Stage 4 Buying row).

Mirrors refresh_easyecom_workspace_s8f_supplier.py. Adds:
  - Buying sub-header + 6 number cards (POs FNC/Drift, GRNs
    Failed/Discrepancy/Held-Pre-QC/STN-Routed-pending) to the
    workspace content + number_cards array.
  - PO Map + GRN Map sub-links under Masters Card Break in the
    sidebar.
  - PO + GRN worklist link entries under FDE Worklists Card Break.

Frappe's migrate doesn't re-import an existing workspace's content
arrays (to preserve in-desk customisations), so this patch forces
a one-shot reload after the §9 Stage 4 JSON edits land.

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
        "from JSON (§9 Stage 4: Buying row — PO/GRN cards + worklist links)"
    )
