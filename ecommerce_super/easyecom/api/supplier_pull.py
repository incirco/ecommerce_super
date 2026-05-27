"""FDE-facing whitelisted endpoint for §8f Stage 3+5 — Discover Suppliers.

Mirrors api.customer_pull.discover_customers exactly: role-gated,
never raises through the whitelist, returns a dict the form-button JS
can render as a clean summary.

Onboarding mode (Stage 3): pulls /wms/V2/getVendors (cursor-walked),
creates Suppliers + Addresses + Map rows, propagates lifecycle.

ERPNext-mastered mode (Stage 5): same pull, but
process_one_supplier's phase gate routes each row through the drift
detector instead of accept-and-create. Drift Map rows are written;
ERPNext is NEVER mutated; Sync Records land as Discrepancy.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.supplier_pull import pull_suppliers


@frappe.whitelist()
def discover_suppliers(
    *, start_fresh: bool | str = True, account: str | None = None
) -> dict[str, Any]:
    """Run the §8f pull (mode-aware: onboarding → accept-and-create;
    erpnext_mastered → drift-detection-only). Returns a JSON-friendly
    summary.

    Args:
        start_fresh: True (default) clears the cursor and pulls from
            the top; False resumes from the persisted cursor. JS
            passes a string; we coerce.
        account: optional EasyEcom Account docname. Defaults to the
            single enabled account.

    Permission: EasyEcom FDE / System Manager / EasyEcom System Manager.
    Operator is read-only and refused.

    Never raises through the whitelist boundary. On infrastructure
    failure returns {"ok": False, "message": ...}; per-record
    failures are aggregated into outcome.failures and returned as
    part of the success summary.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Discover Suppliers requires EasyEcom FDE or System Manager privilege."
            ),
            frappe.PermissionError,
        )

    # Coerce string truthy from JS.
    if isinstance(start_fresh, str):
        start_fresh_bool = start_fresh.strip().lower() not in (
            "false", "0", "no", "",
        )
    else:
        start_fresh_bool = bool(start_fresh)

    try:
        outcome = pull_suppliers(start_fresh=start_fresh_bool, account=account)
    except Exception as exc:
        frappe.log_error(
            title="EasyEcom Discover Suppliers failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "message": (
                f"Pull failed: {type(exc).__name__}: {exc}. See Error Log."
            ),
        }

    return {
        "ok": True,
        "account": account,
        "pages_walked": outcome.pages_walked,
        "final_cursor_present": bool(outcome.final_cursor),
        "total": outcome.total,
        "created": outcome.created,
        "skipped": outcome.skipped,
        "disabled": outcome.disabled,
        "created_flagged": outcome.created_flagged,
        "flagged_not_created": outcome.flagged_not_created,
        "drift_count": outcome.drift_count,
        "failed": outcome.failed,
        "failures_sample": outcome.failures[:5],
    }
