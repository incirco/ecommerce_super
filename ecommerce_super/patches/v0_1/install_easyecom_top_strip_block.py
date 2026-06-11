"""Install the EasyEcom Top Strip Custom HTML Block + refresh the
EasyEcom workspace from JSON (§17.2 informational layer).

Two things in one patch — both idempotent:

1. Insert the EasyEcom Top Strip Custom HTML Block from its
   canonical JSON. Frappe's migrate auto-loader doesn't know about
   Custom HTML Block (unlike Number Card / Dashboard Chart /
   Workspace, which it does auto-import from
   `<app>/<module>/<doctype>/<name>/<name>.json`).

2. Force-reload the EasyEcom workspace JSON. Frappe's normal migrate
   does NOT re-import an existing workspace's content / number_cards /
   charts (so user edits in the desk aren't clobbered). But this
   packet ADDS new content / cards / charts; an in-place site that
   already had the b3dc218 workspace needs a one-shot refresh.
   import_file_by_path(force=True) is the standard mechanism Frappe
   uses for app-shipped workspace updates.

SEE ALSO (gh#3 follow-up #2): on deployments where the JSON fixture
didn't reach the bench (Frappe Cloud surface where the file system
isn't introspectable from inside the patch), the `if not
json_path.exists(): return` defensive clause below turns into a
silent no-op that still logs the patch as executed.
`insert_easyecom_top_strip_from_inline` is the rescue path — it
carries the same html/script/style as embedded Python strings.
Both patches must produce the same final block content; the drift
check lives in tests/integration/test_top_strip_inline_matches_json.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import frappe


CUSTOM_BLOCK_NAME = "EasyEcom Top Strip"


def execute() -> None:
    # Workspace refresh moved to a separate patch
    # (refresh_easyecom_workspace_s17) so an idempotent re-run lands
    # on sites that already executed THIS patch in its prior shape.
    # Frappe records patch-execution by patches.txt entry; once a
    # patch line has run, it won't re-run. Adding a new patch line
    # below is how we ship the workspace refresh without touching
    # this already-applied patch's content.
    _install_custom_html_block()


def _install_custom_html_block() -> None:
    if frappe.db.exists("Custom HTML Block", CUSTOM_BLOCK_NAME):
        return
    json_path = Path(
        frappe.get_app_path(
            "ecommerce_super",
            "easyecom", "custom_html_block",
            "easyecom_top_strip", "easyecom_top_strip.json",
        )
    )
    if not json_path.exists():
        # Defensive — shouldn't happen, but a missing fixture file is
        # not a migrate-blocker; the workspace will degrade gracefully
        # (the custom_block content reference renders an empty slot).
        return
    data = json.loads(json_path.read_text())
    data.pop("__islocal", None)
    doc = frappe.get_doc(data)
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"[ecommerce_super] installed Custom HTML Block {CUSTOM_BLOCK_NAME!r}")


# refresh_workspace_from_json lives in refresh_easyecom_workspace_s17.py
# (separate patch line so it runs idempotently on already-migrated sites).
