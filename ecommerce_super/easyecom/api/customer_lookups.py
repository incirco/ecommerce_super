"""FDE-facing whitelisted endpoint for the §8e Stage 2 refresh.

Wraps the discover flow in the standard "never raise through whitelist"
+ role-gate contract used by discover_locations / discover_channels.
Returns a dict the form-button JS can render inline (counts + failed
summary).

Mode-irrelevant: the lookup tables are needed in both onboarding and
erpnext_mastered modes, so the button doesn't check master_mode.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.customer_lookups import (
    pull_countries_and_states,
)


@frappe.whitelist()
def refresh_countries_and_states() -> dict[str, Any]:
    """Refresh the EasyEcom Country / EasyEcom State cache by pulling
    /getCountries and per-country /getStates.

    Permission: EasyEcom FDE / System Manager / EasyEcom System Manager.
    Operator is read-only and refused.

    Never raises through the whitelist boundary. On failure returns
    {"ok": False, "message": ...} so the JS handler can render a clean
    message rather than a stack trace.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Refresh States/Countries requires EasyEcom FDE or "
                "System Manager privilege."
            ),
            frappe.PermissionError,
        )

    try:
        outcome = pull_countries_and_states()
    except Exception as exc:  # noqa: BLE001 — whitelist boundary
        frappe.log_error(
            title="EasyEcom Refresh States/Countries failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "message": (
                f"Refresh failed: {type(exc).__name__}: {exc}. "
                "See Error Log for the full trace."
            ),
        }

    return {
        "ok": True,
        "countries_total": outcome.countries_total,
        "countries_new": outcome.countries_new,
        "countries_updated": outcome.countries_updated,
        "countries_skipped": outcome.countries_skipped,
        "countries_failed_count": len(outcome.countries_failed),
        "countries_failed_sample": outcome.countries_failed[:5],
        "states_total": outcome.states_total,
        "states_new": outcome.states_new,
        "states_updated": outcome.states_updated,
        "states_skipped": outcome.states_skipped,
        "states_failed_count": len(outcome.states_failed),
        "states_failed_sample": outcome.states_failed[:5],
    }
