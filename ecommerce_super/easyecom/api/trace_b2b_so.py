"""Diagnostic endpoint for §11 B2B sales push (skeleton — Stage 1).

Mirror of §10's transfer_diagnostic.trace_dn: takes an SO name and
walks every gate the §11 push touches, returning a structured trace
the FDE can render on the form. Read-only — never mutates state.

Stage 1 ships only the skeleton (permission gate + SO-exists check +
placeholder gate list). Stage 2 fills in the real gate walks once
gating.py and push.py exist.

Usage from desk console:
    frappe.call(
      'ecommerce_super.easyecom.api.trace_b2b_so.trace_so',
      {so_name: 'SAL-ORD-2026-00042'},
    )
"""

from __future__ import annotations

from typing import Any

import frappe


@frappe.whitelist()
def trace_so(so_name: str) -> dict[str, Any]:
    """Return a structured trace of the §11 push state for an SO.

    Stage 1 contract:
      - Permission gate (FDE / System Manager / EE System Manager).
      - SO existence check.
      - Placeholder gates list with status='stage2_pending'.

    Stage 2 adds the real Gate 0 + precondition walks + push attempt
    inspection. Stage 3 adds last_polled_at + polling reconciliation
    detail.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._("B2B trace requires EasyEcom FDE or System Manager."),
            frappe.PermissionError,
        )

    trace: dict[str, Any] = {
        "ok": True,
        "so_name": so_name,
        "stage": "stage_1_skeleton",
        "gates": [],
        "downstream": {},
    }

    if not so_name or not frappe.db.exists("Sales Order", so_name):
        trace["ok"] = False
        trace["gates"].append(
            {
                "gate": "so_exists",
                "passed": False,
                "detail": f"Sales Order {so_name!r} not found",
            }
        )
        return trace

    trace["gates"].append(
        {
            "gate": "so_exists",
            "passed": True,
            "detail": f"Sales Order {so_name} exists",
        }
    )

    # Placeholder — Stage 2 wires gate_0, preconditions_*, push_attempt,
    # map_row_state. Stage 3 wires polling_last_at + polling_diagnosis.
    trace["gates"].append(
        {
            "gate": "stage_2_pending",
            "passed": None,
            "detail": (
                "Gate 0, precondition walks, push-attempt inspection, "
                "and Map-row state population land in Stage 2."
            ),
        }
    )

    # Downstream artifact discovery — Stage 2 wires this. Existing
    # B2B Order Map row + recent Integration Discrepancies + last
    # API Call rows would all live here.
    trace["downstream"]["b2b_order_map"] = None
    trace["downstream"]["discrepancies"] = []
    trace["downstream"]["api_calls"] = []

    return trace
