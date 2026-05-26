"""Rename the drift+exclude child DocTypes to entity-agnostic names.

§8f Stage 1 work: the child DocTypes that started life as Item-Map
sub-tables are now used by THREE consumers (Item Map, Customer Map,
soon-to-land Supplier Map). The "Item Map" prefix was always cosmetic
(the schema is entity-agnostic — field + value pairs only). With three
consumers it's actively misleading; renaming once justified.

  EasyEcom Item Map Drift Field    →  EasyEcom Drift Field
  EasyEcom Item Map Exclude Field  →  EasyEcom Exclude Field

Frappe's `rename_doc("DocType", old, new)` does the table-rename
(`tabOLD` → `tabNEW`) AND updates DocField.options references where
they pointed at the old name. Existing child rows survive — only the
table identity changes.

The JSON files on disk have already been moved + renamed before this
patch fires; the model sync after this patch re-imports the new
DocType identities cleanly.

Idempotent: runs only when the OLD name still exists. After a
successful rename the old name is gone, so subsequent migrate ticks
are no-ops.

Pre-model-sync because the OLD DocType has to be renamed BEFORE
model sync re-imports the new JSON (otherwise sync would try to
create the new DocType while the old still exists, hitting the same
table name).
"""

from __future__ import annotations

import frappe


RENAMES = [
    ("EasyEcom Item Map Drift Field", "EasyEcom Drift Field"),
    ("EasyEcom Item Map Exclude Field", "EasyEcom Exclude Field"),
]


def execute() -> None:
    for old, new in RENAMES:
        if frappe.db.exists("DocType", new):
            # Already renamed — nothing to do.
            continue
        if not frappe.db.exists("DocType", old):
            # Fresh install / never had the old name. Model sync will
            # create the new DocType from the on-disk JSON. No rename
            # needed.
            continue
        # rename_doc handles: tabOLD → tabNEW, DocField.options
        # references, and the DocType row itself. force=True bypasses
        # safety prompts (we know what we're doing); merge=False because
        # we're NOT merging with an existing new-named row (we checked
        # exists above).
        frappe.rename_doc(
            "DocType", old, new, force=True, merge=False
        )
        print(f"[ecommerce_super] renamed DocType {old!r} → {new!r}")
    frappe.db.commit()
