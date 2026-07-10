"""gh#144 — one-shot backfill for Customer Map rows whose ee_c_id is
still a pre-fix "flagged-<docname>" placeholder even though push has
since succeeded and ee_customer_id carries the real EE c_id.

Pre-fix, `_upsert_map_row_after_create` (customer_push.py:524) only
wrote ee_customer_id on the existing-map path, so an initial
Flagged-Not-Created row that later got pushed successfully never had
its placeholder ee_c_id overwritten. The inbound resolver in
invoice_mirror._resolve_customer queries ee_c_id → misses → SI create
fails with "cannot resolve buyer" even for customers that ARE pushed
and ARE mapped.

The push code and resolver are both fixed forward-going. This patch
heals the historical rows on already-deployed sites.

Idempotent: only touches rows where ee_c_id starts with "flagged-"
AND ee_customer_id looks real (non-empty, non-placeholder).

Deliberately does NOT touch rows where ee_customer_id is empty —
those are genuinely-still-Flagged-Not-Created and should stay flagged
so the FDE worklist keeps surfacing them.
"""
from __future__ import annotations

import frappe


def execute() -> None:
    rows = frappe.db.sql(
        """
        SELECT name, ee_c_id, ee_customer_id, erpnext_name, status
          FROM `tabEasyEcom Customer Map`
         WHERE ee_c_id LIKE 'flagged-%'
           AND COALESCE(ee_customer_id, '') != ''
           AND ee_customer_id NOT LIKE 'flagged-%'
        """,
        as_dict=True,
    )
    if not rows:
        return

    healed = 0
    for r in rows:
        new_ee_c_id = str(r["ee_customer_id"])
        # Guard against unique-constraint collision: another map row
        # may already own this ee_c_id (a §8e pull created a duplicate
        # for the same customer). Skip and leave for gh#126 dedup.
        collision = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"ee_c_id": new_ee_c_id, "name": ["!=", r["name"]]},
            "name",
        )
        if collision:
            frappe.logger().warning(
                f"gh#144 backfill: skipping {r['name']} "
                f"(ee_c_id={new_ee_c_id} already owned by "
                f"{collision} — dedup via gh#126)"
            )
            continue
        # If status was Flagged-Not-Created but ee_customer_id is
        # real, the push DID succeed at some point — Mapped is the
        # honest state.
        new_status = (
            "Mapped" if r["status"] == "Flagged-Not-Created" else r["status"]
        )
        frappe.db.set_value(
            "EasyEcom Customer Map",
            r["name"],
            {
                "ee_c_id": new_ee_c_id,
                "status": new_status,
                "flag_reason": "",
            },
            update_modified=False,
        )
        healed += 1

    frappe.logger().info(
        f"gh#144 backfill: healed {healed} Customer Map rows "
        f"(of {len(rows)} candidates)"
    )
