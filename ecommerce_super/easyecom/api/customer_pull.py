"""FDE-facing whitelisted endpoint for §8e Stage 3 customer pull.

Mirrors `discover_locations` / `discover_products` shape — role-gated,
never raises through the whitelist, returns a dict the form button
can render inline.

Mode handling: Stage 3 doesn't branch on customer_master_mode (the
flip-aware behaviour ships in Stage 5). For now, both onboarding and
erpnext_mastered modes run the same accept-and-create logic; the
button copy on the form sets expectations.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.customer_pull import pull_customers


@frappe.whitelist()
def discover_customers() -> dict[str, Any]:
    """Pull wholesale customers from EE and upsert ERPNext Customer +
    Address + Customer Map per row.

    Permission: EasyEcom FDE / System Manager / EasyEcom System Manager.
    Operator is read-only and refused.

    Never raises through the whitelist boundary. On failure returns
    {"ok": False, "message": ...} so the JS handler renders a clean
    message rather than a stack trace.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Discover Customers requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )

    try:
        outcome = pull_customers()
    except Exception as exc:  # noqa: BLE001 — whitelist boundary
        frappe.log_error(
            title="EasyEcom Discover Customers failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "message": (
                f"Discover Customers failed: {type(exc).__name__}: {exc}. "
                "See Error Log for the full trace."
            ),
        }

    return {
        "ok": True,
        "total": outcome.total,
        "created": outcome.created,
        "skipped": outcome.skipped,
        "created_flagged": outcome.created_flagged,
        "flagged_not_created": outcome.flagged_not_created,
        "failed": outcome.failed,
        "failures_sample": outcome.failures[:10],
    }
