"""Rename the three worklist Number Cards from em-dash to hyphen.

User-facing labels previously used " — " (em-dash) as the separator;
the visual is awkward in narrow UIs and inconsistent with the rest
of the app. Renamed throughout to " - " (hyphen). This patch handles
the in-place rename for sites that already have the em-dash rows
(fresh installs pick up the new JSON-defined hyphen names directly).

Idempotent: skips when the new name already exists or the old name
doesn't.
"""

from __future__ import annotations

import frappe


RENAMES = {
    "Locations — To Map": "Locations - To Map",
    "Channels — Unclassified": "Channels - Unclassified",
    "Tax Rules — To Configure": "Tax Rules - To Configure",
}


def execute() -> None:
    for old, new in RENAMES.items():
        if frappe.db.exists("Number Card", new):
            # New name already in place — old should have been renamed
            # to it (or the row is fresh). If both exist, that's a
            # cleanup issue beyond this patch's scope.
            continue
        if not frappe.db.exists("Number Card", old):
            continue
        frappe.rename_doc("Number Card", old, new, force=True, merge=False)
        # Update the matching `label` field too (rename_doc only
        # changes the primary key).
        frappe.db.set_value(
            "Number Card", new, "label", new, update_modified=False
        )
        print(f"[ecommerce_super] renamed Number Card {old!r} → {new!r}")
    frappe.db.commit()
