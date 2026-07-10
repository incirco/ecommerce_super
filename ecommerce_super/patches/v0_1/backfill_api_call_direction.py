"""gh#147 — set direction='Outbound' on every pre-existing EasyEcom API
Call row so the field's `reqd=1` constraint is satisfied on already-
deployed sites. All historic rows are outbound (only outbound-side
logging existed before this patch).

Idempotent: skips rows already carrying a direction value.
"""
from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.has_column("EasyEcom API Call", "direction"):
        # DocType hasn't migrated yet; bench migrate runs the JSON
        # schema sync BEFORE post_model_sync patches, so this should
        # never trigger — defensive only.
        return
    frappe.db.sql(
        """
        UPDATE `tabEasyEcom API Call`
           SET direction = 'Outbound'
         WHERE direction IS NULL OR direction = ''
        """
    )
    frappe.db.commit()
