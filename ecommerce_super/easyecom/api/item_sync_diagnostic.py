"""Diagnostic endpoint for §8d Item sync (gh#37).

Reporter symptom: "Item updates are not synced or mapped across ERPNext
and EasyEcom" — covers BOTH directions:
  - ERPNext → EE: depends on `EasyEcom Account.auto_push_on_save=1`
    AND the `enqueue_on_item_change` doc_event hook firing. If the
    toggle is OFF (the safe default), the hook silently no-ops.
  - EE → ERPNext: depends on the FDE manually running "Discover
    Products" — there is NO scheduled poll. Re-run pulls existing
    Map rows through `_load_or_refresh_item_from_map`, which IS an
    additive refresh on the linked Item.

Both paths are wired correctly in our code; the user-facing observation
is that NOTHING happens because (a) the FDE never enabled the toggle,
(b) the FDE never re-ran Discover Products, or (c) edge cases (Stage 5
`item_master_mode=erpnext_mastered` drift detection, ping-pong guard,
has_variants gate).

This endpoint walks every gate + downstream artifact for one Item and
returns a structured trace the FDE can read on the form. Read-only —
inspects existing DB state, does NOT re-fire any push/pull.

Usage from desk console:
    frappe.call('ecommerce_super.easyecom.api.item_sync_diagnostic.trace_item',
                {item_code: 'WIDGET-001'})

Or via the FDE button on the Item form (wired in a separate JS patch).
"""

from __future__ import annotations

from typing import Any

import frappe


@frappe.whitelist()
def trace_item(item_code: str) -> dict[str, Any]:
    """Walk every gate the Item sync flow touches (push hook + pull
    refresh) and report what's visible in the DB. Read-only — never
    mutates state.

    Gate coverage note: the `enqueue_on_item_change` hook also gates on
    `frappe.flags.in_easyecom_pull` to prevent pull→push ping-pong.
    That flag is request-scoped and only ever set transiently during an
    item-pull cycle — at FDE diagnostic call time it's always False, so
    surfacing it here would always read "OK" and be misleading. We
    intentionally omit it.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._("Item sync trace requires EasyEcom FDE or System Manager."),
            frappe.PermissionError,
        )

    trace: dict[str, Any] = {
        "ok": True,
        "item_code": item_code,
        "push_gates": [],
        "pull_state": {},
        "downstream": {},
    }

    if not item_code or not frappe.db.exists("Item", item_code):
        trace["ok"] = False
        trace["push_gates"].append(
            {
                "gate": "item_exists",
                "passed": False,
                "detail": f"Item {item_code!r} not found",
            }
        )
        return trace

    item = frappe.get_doc("Item", item_code)

    # === Push (ERPNext → EE) gate walk ===
    # We query without the auto_push_on_save filter (unlike
    # item_push._account_with_auto_push_enabled which combines both)
    # because the diagnostic needs to surface the Account state
    # regardless of the toggle, so the auto_push_on_save gate below
    # can say "OFF" with the right context. Multi-account ambiguity
    # also worth surfacing — push silently picks first-by-name and
    # logs an Error Log row; the FDE has no reason to discover that
    # without this trace pointing at it.
    account_rows = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1},
        fields=["name", "auto_push_on_save", "item_master_mode"],
        order_by="name asc",
        limit_page_length=2,
    )
    account_row = account_rows[0] if account_rows else None
    multi_account = len(account_rows) > 1

    trace["push_gates"].append(
        {
            "gate": "easyecom_account_enabled",
            "passed": account_row is not None,
            "detail": (
                (
                    f"account={account_row.name}"
                    + (
                        " — WARNING: multiple Accounts have enabled=1; "
                        "push picks first-by-name and logs an Error Log row. "
                        "Disable the others to remove ambiguity."
                        if multi_account
                        else ""
                    )
                )
                if account_row
                else "no enabled Account"
            ),
        }
    )

    if account_row:
        trace["push_gates"].append(
            {
                "gate": "auto_push_on_save",
                "passed": bool(int(account_row.auto_push_on_save or 0)),
                "detail": (
                    "ON — every Item save enqueues an EE push"
                    if int(account_row.auto_push_on_save or 0)
                    else (
                        "OFF — the on_update hook silently returns. Click "
                        "'Go Live → Enable Items auto-push' on the EasyEcom "
                        "Account form to enable. Manual pushes via the "
                        "'Push to EasyEcom' button on this form still work."
                    )
                ),
            }
        )
        trace["push_gates"].append(
            {
                "gate": "item_master_mode",
                "passed": True,  # informational — not a gate per se
                "detail": (
                    f"mode={account_row.item_master_mode or 'onboarding'!r} "
                    + (
                        "(steady state — push is the authoritative direction)"
                        if account_row.item_master_mode == "erpnext_mastered"
                        else "(onboarding — pull is authoritative, push races EE; "
                        "flip to erpnext_mastered before relying on push for updates)"
                    )
                ),
            }
        )

    trace["push_gates"].append(
        {
            "gate": "not_has_variants",
            "passed": not bool(int(getattr(item, "has_variants", 0) or 0)),
            "detail": (
                "variant-template Items are skipped — only sellable variants "
                "or non-variant Items push"
                if int(getattr(item, "has_variants", 0) or 0)
                else "OK"
            ),
        }
    )

    trace["push_gates"].append(
        {
            "gate": "not_disabled",
            "passed": not bool(int(item.disabled or 0)),
            "detail": (
                "Item is disabled — push_lifecycle deactivate fires instead "
                "of Create/Update"
                if int(item.disabled or 0)
                else "OK"
            ),
        }
    )

    # === Pull (EE → ERPNext) state — informational, not a gate walk ===
    trace["pull_state"] = {
        "manual_only": True,
        "detail": (
            "EE → ERPNext sync is NOT scheduled. Run 'Discover Products' on "
            "the EasyEcom Account to refresh this Item from EE. Existing "
            "Map rows go through _load_or_refresh_item_from_map, which "
            "additively refreshes non-None fields onto the linked Item."
        ),
        "last_pull_at": frappe.db.get_value(
            "EasyEcom Account",
            {"enabled": 1},
            "item_pull_cursor_at",
        ),
    }

    # === Downstream artifacts ===
    map_row = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Item", "erpnext_name": item_code},
        [
            "name",
            "status",
            "ee_product_id",
            "ee_cp_id",
            "ee_sku",
            "flag_reason",
        ],
        as_dict=True,
    )
    trace["downstream"]["item_map"] = dict(map_row) if map_row else None

    sync_records = frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"entity_doctype": "Item", "entity_name": item_code},
        fields=["name", "status", "direction", "last_error", "modified"],
        order_by="modified DESC",
        limit_page_length=5,
    )
    trace["downstream"]["sync_records"] = sync_records

    queue_jobs = frappe.db.get_all(
        "EasyEcom Queue Job",
        filters={
            "target_doctype": "Item",
            "target_name": item_code,
            "job_type": "Item Push",
        },
        fields=["name", "state", "attempts", "last_error", "modified"],
        order_by="modified DESC",
        limit_page_length=5,
    )
    trace["downstream"]["queue_jobs"] = queue_jobs

    sr_names = [r.name for r in sync_records]
    api_calls = (
        frappe.db.get_all(
            "EasyEcom API Call",
            filters={"parent_sync_record": ("in", sr_names)},
            fields=[
                "name",
                "endpoint",
                "status",
                "response_status_code",
                "modified",
            ],
            order_by="modified DESC",
            limit_page_length=10,
        )
        if sr_names
        else []
    )
    trace["downstream"]["api_calls"] = api_calls

    # === Verdict ===
    failed_gates = [g for g in trace["push_gates"] if not g["passed"]]
    if failed_gates:
        # Most actionable: name the first failing gate explicitly.
        first_fail = failed_gates[0]
        if first_fail["gate"] == "auto_push_on_save":
            trace["verdict"] = (
                "Auto-push on save is OFF. ERPNext → EE syncs only when the "
                "FDE clicks 'Push to EasyEcom' on this form OR runs the "
                "batch sweep. Updates to this Item save successfully in "
                "ERPNext but do NOT propagate to EE until auto-push is "
                "enabled (Go Live → Enable Items auto-push on the EasyEcom "
                "Account)."
            )
        else:
            trace["verdict"] = (
                f"Push did NOT fire — gate {first_fail['gate']!r} failed: "
                + first_fail["detail"]
            )
    elif map_row and map_row.get("ee_product_id"):
        trace["verdict"] = (
            f"Item is mapped to EE (ee_product_id={map_row['ee_product_id']!r}). "
            f"Map status={map_row['status']!r}. Push hook is wired and "
            "auto-push is ON; future saves will enqueue updates. To pull "
            "the latest EE state into ERPNext, run 'Discover Products' on "
            "the EasyEcom Account."
        )
    elif map_row:
        trace["verdict"] = (
            f"Map row exists (status={map_row['status']!r}) but ee_product_id "
            f"is blank — never successfully created on EE. flag_reason="
            f"{map_row.get('flag_reason') or '—'!r}. Resolve the flagged "
            "reason, then re-push via the form button."
        )
    else:
        trace["verdict"] = (
            "No Item Map row exists. The Item has never been pushed to EE "
            "and is not in scope of any previous Discover Products pull. "
            "Click 'Push to EasyEcom' on this form to create it on EE."
        )

    return trace
