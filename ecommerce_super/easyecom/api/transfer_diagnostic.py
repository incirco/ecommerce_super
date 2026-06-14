"""Diagnostic endpoint for §10 outbound STN push (gh#26).

The reporter ran an end-to-end test: DN submitted in ERPNext, but no
corresponding entry appeared in Harmony's Bulk Orders module. The
`enqueue_on_dn_submit` hook has multiple silent Gate-0 early returns
(no Internal-Customer flag, no warehouse pair, neither warehouse
EE-mapped) AND `push_one_transfer` itself emits "skipped" outcomes for
those same gates without writing a Sync Record — so the FDE has no
trace of what the integration decided.

This endpoint takes a Delivery Note name and walks every gate +
side-effect the outbound push touches, returning a structured trace
the FDE can read on the form. It does NOT re-fire the push — it
inspects the existing state.

Usage from desk console:
    frappe.call('ecommerce_super.easyecom.api.transfer_diagnostic.trace_dn',
                {dn_name: 'MAT-DN-2026-00042'})

Or via the FDE button on the Delivery Note form (wired in a separate
JS patch).
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.transfer_push import (
    _is_ee_mapped_warehouse,
    _resolve_source_target_pair,
)


@frappe.whitelist()
def trace_dn(dn_name: str) -> dict[str, Any]:
    """Walk every gate the outbound push touches and report what's
    visible in the DB. Read-only — never mutates state."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._("Transfer trace requires EasyEcom FDE or System Manager."),
            frappe.PermissionError,
        )

    trace: dict[str, Any] = {
        "ok": True,
        "dn_name": dn_name,
        "gates": [],
        "downstream": {},
    }

    if not dn_name or not frappe.db.exists("Delivery Note", dn_name):
        trace["ok"] = False
        trace["gates"].append(
            {"gate": "dn_exists", "passed": False, "detail": f"DN {dn_name!r} not found"}
        )
        return trace

    dn = frappe.get_doc("Delivery Note", dn_name)

    # Gate 0a — DN must be submitted to have triggered the on_submit hook.
    trace["gates"].append(
        {
            "gate": "docstatus_submitted",
            "passed": int(dn.docstatus or 0) == 1,
            "detail": f"docstatus={dn.docstatus}",
        }
    )

    # Gate 0b — is_internal_customer must be 1 (the on_submit hook
    # short-circuits on the unflag).
    trace["gates"].append(
        {
            "gate": "is_internal_customer",
            "passed": bool(int(getattr(dn, "is_internal_customer", 0) or 0)),
            "detail": f"is_internal_customer={getattr(dn, 'is_internal_customer', None)}",
        }
    )

    # Gate 0c — single source/target warehouse pair (multi-pair refused
    # by validate_pre_submit, but legacy DNs may slip through).
    try:
        pair = _resolve_source_target_pair(dn)
    except Exception as exc:  # noqa: BLE001
        trace["gates"].append(
            {
                "gate": "source_target_pair",
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
        pair = None
    if pair is None:
        trace["gates"].append(
            {
                "gate": "source_target_pair",
                "passed": False,
                "detail": "no consistent (source_wh, target_wh) pair on the DN",
            }
        )
    else:
        source_wh, target_wh = pair
        trace["gates"].append(
            {
                "gate": "source_target_pair",
                "passed": True,
                "detail": f"source={source_wh}, target={target_wh}",
            }
        )

        # Gate 0d — at least one of source/target must be EE-mapped.
        src_mapped = _is_ee_mapped_warehouse(source_wh)
        tgt_mapped = _is_ee_mapped_warehouse(target_wh)
        trace["gates"].append(
            {
                "gate": "source_warehouse_ee_mapped",
                "passed": bool(src_mapped),
                "detail": f"warehouse={source_wh}",
            }
        )
        trace["gates"].append(
            {
                "gate": "target_warehouse_ee_mapped",
                "passed": bool(tgt_mapped),
                "detail": f"warehouse={target_wh}",
            }
        )
        trace["gates"].append(
            {
                "gate": "at_least_one_warehouse_ee_mapped",
                "passed": bool(src_mapped or tgt_mapped),
                "detail": "Gate-0: silently inert if neither warehouse is EE-mapped",
            }
        )

    # Downstream — what artifacts has the push (attempted or
    # successful) left in the DB?
    # gh#26 (mmpl16 retest): `branch` was speculatively included in
    # the field list when the diagnostic was first written, but it's
    # NOT a field on EasyEcom Transfer Map (branch resolution is
    # computed dynamically by predict_section10_branch from the
    # warehouse pair). The query was crashing with
    # `MySQLdb.OperationalError: Unknown column 'branch'` and aborting
    # the entire diagnostic before any artifact reached the FDE.
    # Fix: drop `branch` from the column list; FDE-visible branch
    # routing lives in the gates above (source/target warehouse
    # EE-mapped flags).
    map_row = frappe.db.get_value(
        "EasyEcom Transfer Map",
        {"delivery_note": dn_name},
        [
            "name",
            "status",
            "ee_order_id",
            "flag_reason",
            "sales_invoice",
        ],
        as_dict=True,
    )
    trace["downstream"]["transfer_map"] = dict(map_row) if map_row else None

    sync_records = frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"entity_doctype": "Delivery Note", "entity_name": dn_name},
        fields=["name", "status", "direction", "last_error", "modified"],
        order_by="modified DESC",
        limit_page_length=5,
    )
    trace["downstream"]["sync_records"] = sync_records

    queue_jobs = frappe.db.get_all(
        "EasyEcom Queue Job",
        filters={
            "target_doctype": "Delivery Note",
            "target_name": dn_name,
            "job_type": "Transfer Push",
        },
        fields=["name", "state", "attempts", "last_error", "modified"],
        order_by="modified DESC",
        limit_page_length=5,
    )
    trace["downstream"]["queue_jobs"] = queue_jobs

    api_calls = frappe.db.get_all(
        "EasyEcom API Call",
        filters={"parent_sync_record": ("in", [r.name for r in sync_records])},
        fields=["name", "endpoint", "status", "response_status_code", "modified"],
        order_by="modified DESC",
        limit_page_length=10,
    )
    trace["downstream"]["api_calls"] = api_calls

    # Verdict — the most actionable line for the FDE.
    failed_gates = [g for g in trace["gates"] if not g["passed"]]
    if failed_gates:
        trace["verdict"] = (
            "Push did NOT fire — the following gate(s) failed: "
            + ", ".join(g["gate"] for g in failed_gates)
        )
    elif map_row and map_row.get("ee_order_id"):
        trace["verdict"] = (
            f"Push reached EE — Transfer Map status={map_row['status']!r}, "
            f"ee_order_id={map_row['ee_order_id']!r}. If Harmony's Bulk "
            "Orders is not showing this, check the Harmony filter "
            "(date range / status / store) — the integration's side is "
            "complete. Cross-reference the API Call row to confirm the "
            "exact EE response."
        )
    elif map_row:
        trace["verdict"] = (
            f"Push was attempted — Transfer Map status={map_row['status']!r}, "
            f"flag_reason={map_row.get('flag_reason') or '—'}. EE did not "
            "return an ee_order_id; check the latest Sync Record's "
            "last_error and the linked API Call row for the EE response."
        )
    else:
        trace["verdict"] = (
            "All gates passed but no Transfer Map row exists — the "
            "on_submit hook did not run (was the DN created before the "
            "Internal-Customer flag was set?), or push_one_transfer "
            "raised before the upsert. Check Error Log for the time of "
            "DN submission."
        )

    return trace
