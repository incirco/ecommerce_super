"""EasyEcom Exclude Field — child table of EasyEcom Item Map.

Lists ERPNext field names the drift detector should IGNORE when
comparing this specific Item against an incoming EE payload. Use case:
the FDE renames item_name in ERPNext intentionally (e.g. cleaner
internal name vs the EE marketplace-facing one). Without this list,
every nightly drift pull would re-flag the same field forever.

Stored per-Item-Map-row (not globally) — different items may have
different intentional divergences. Empty list (the default) means
"compare every field" — preserves Stage-5 behaviour for any item that
hasn't been touched.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomExcludeField(Document):
    pass
