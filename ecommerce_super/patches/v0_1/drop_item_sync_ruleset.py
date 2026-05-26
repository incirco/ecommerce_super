"""Drop the stale EasyEcom-Item-Sync ruleset row.

§8d Stage 2 replaces the Bidirectional EasyEcom-Item-Sync fixture with
two new ones split by direction: EasyEcom-Item-Pull (Stage 2, shipped
now) and EasyEcom-Item-Push (Stage 3, future). The fixture loader can
add the new row but does NOT delete the old — orphan removal during
migrate only catches orphan child docs, not parent renames.

This patch is idempotent: if EasyEcom-Item-Sync doesn't exist (fresh
install or already-migrated site) it's a no-op.

The replacement EasyEcom-Item-Pull row is added by the fixture loader
in the same migrate cycle (`fixtures = [... "EasyEcom Field Mapping" ...]`
in hooks.py), so by the time the next pull runs the new ruleset is in
place. There's no operational window where neither ruleset exists in
the DB because the new fixture's `name` differs from the old (no
collision; both can briefly coexist if this patch runs after fixture
load — Frappe's patch order is post_model_sync → fixtures).

Why we don't keep the old name: callers (item_pull.py) name the
ruleset they want to use; renaming would require changing every
caller. The Pull-direction ruleset gets the Pull-named identifier;
the Push ruleset will get its own. The old name now means nothing.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    old_name = "EasyEcom-Item-Sync"
    if not frappe.db.exists("EasyEcom Field Mapping", old_name):
        return
    # Hard delete — the child Field Mapping Rule rows go with it via the
    # parent cascade. force=True bypasses the standard "linked record"
    # warning; nothing should reference this ruleset by name (no caller
    # ever shipped that depended on it).
    frappe.delete_doc(
        "EasyEcom Field Mapping", old_name, force=True, ignore_permissions=True
    )
    frappe.db.commit()
    print(
        f"[ecommerce_super] dropped stale ruleset {old_name!r} "
        "(replaced by EasyEcom-Item-Pull in §8d Stage 2)"
    )
