"""Force-refresh the EasyEcom Top Strip Custom HTML Block from JSON.

Follow-up to gh#3. The original `install_easyecom_top_strip_block` patch
skipped the install when the block already existed. On the puresta
deployment (and likely sibling Frappe Cloud sites), the block existed
but had empty / null `html` / `script` fields — Frappe's workspace
renderer then output the literal string "undefined" between the
"Operational Status" header and the "Operational KPIs" section,
matching the screenshot in the issue. The first gh#3 fix
(test_connection.py persisting Connected) addressed a different layer
and didn't resolve this render bug.

This patch overwrites html / script / style on the existing row from
the on-disk JSON unconditionally. Runs once per site (patches.txt
records execution by patch name). The Custom HTML Block JSON is the
source of truth for the strip's render payload; the EasyEcom System
Manager has no legitimate reason to hand-edit it. Existing audit fields
(creation, owner) are preserved — only the render-payload fields are
rewritten.

SEE ALSO (gh#3 follow-up #2): same `if not json_path.exists(): return`
silent-no-op problem as install_easyecom_top_strip_block on
deployments where the JSON fixture didn't reach the bench. The
`insert_easyecom_top_strip_from_inline` patch carries the same
content as embedded strings and is reachable on those deployments.
Drift check: tests/integration/test_top_strip_inline_matches_json.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import frappe


CUSTOM_BLOCK_NAME = "EasyEcom Top Strip"
_REFRESH_FIELDS = ("html", "script", "style", "is_standard", "module", "private")


def execute() -> None:
    json_path = Path(
        frappe.get_app_path(
            "ecommerce_super",
            "easyecom", "custom_html_block",
            "easyecom_top_strip", "easyecom_top_strip.json",
        )
    )
    if not json_path.exists():
        # Defensive — shouldn't happen in a deployed app; degrade quietly.
        return
    data = json.loads(json_path.read_text())
    data.pop("__islocal", None)

    if frappe.db.exists("Custom HTML Block", CUSTOM_BLOCK_NAME):
        existing = frappe.get_doc("Custom HTML Block", CUSTOM_BLOCK_NAME)
        changed = False
        for field in _REFRESH_FIELDS:
            new_val = data.get(field)
            if new_val is None:
                continue
            if existing.get(field) != new_val:
                existing.set(field, new_val)
                changed = True
        if changed:
            existing.save(ignore_permissions=True)
            frappe.db.commit()
            print(
                f"[ecommerce_super] refreshed Custom HTML Block "
                f"{CUSTOM_BLOCK_NAME!r} (rewrote render payload from JSON)"
            )
        return

    # Block doesn't exist — install fresh.
    doc = frappe.get_doc(data)
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"[ecommerce_super] installed Custom HTML Block {CUSTOM_BLOCK_NAME!r}")
