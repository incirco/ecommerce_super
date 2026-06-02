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
    *,
    start_fresh: bool | str = True,
    account: str | None = None,
    inline: int | bool = False,
) -> dict[str, Any]:
    """Run the §8f pull (mode-aware: onboarding → accept-and-create;
    erpnext_mastered → drift-detection-only).

    DEFAULT: async — enqueues into the `long` queue and returns
    immediately. The 120s desk whitelist budget is fine for the Harmony
    sandbox (~41 vendors) but a >2000-vendor cursor walk + IC
    validation per row blows past it and the JS surfaces "(network or
    permission)" — misleading, since the pull is authorised, just slow.

    `inline=True` opt-in for tests + small catalogues (existing tests
    rely on synchronous outcome shape).

    Permission: EasyEcom FDE / System Manager / EasyEcom System Manager.
    Operator is read-only and refused. Never raises through the
    whitelist.
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

    if not bool(int(inline or 0)):
        import time as _time
        from ecommerce_super.easyecom.utils.discover_notify import safe_caller
        triggered_by = safe_caller()
        job = frappe.enqueue(
            "ecommerce_super.easyecom.api.supplier_pull._discover_suppliers_worker",
            queue="long",
            timeout=3600,
            job_id=f"discover_suppliers_{account or 'default'}_{int(_time.time())}",
            account=account,
            start_fresh=start_fresh_bool,
            triggered_by=triggered_by,
        )
        return {
            "ok": True,
            "enqueued": True,
            "job_id": getattr(job, "id", None) or getattr(job, "name", None),
            "queue": "long",
            "message": (
                "Supplier discovery enqueued in the long queue. The "
                "cursor advances page-by-page on the EasyEcom Account; "
                "refresh this form to see `supplier_pull_cursor_at` "
                "update. Created Suppliers + Map rows appear in the "
                "Supplier Map list as the worker pulls them."
            ),
        }

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
        "enqueued": False,
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


def _discover_suppliers_worker(
    *, account: str | None, start_fresh: bool, triggered_by: str | None = None
) -> None:
    """Background worker entry-point for async supplier discovery.

    Notifies the user who enqueued the job on completion (gh#11) —
    triggered_by is captured at the enqueue site because RQ workers
    don't share `frappe.session.user` with the originating HTTP request.
    """
    from ecommerce_super.easyecom.utils.discover_notify import (
        notify_discover_complete,
    )

    try:
        outcome = pull_suppliers(start_fresh=start_fresh, account=account)
    except Exception as exc:
        frappe.log_error(
            title=f"EasyEcom Discover Suppliers (async) failed for {account or '(default)'}",
            message=f"{type(exc).__name__}: {exc}",
        )
        notify_discover_complete(
            triggered_by=triggered_by,
            kind="Suppliers",
            ok=False,
            summary=f"Discover Suppliers failed: {type(exc).__name__}: {exc}",
            list_route="/app/error-log",
        )
        raise

    summary = (
        f"Pages: {outcome.pages_walked} | Total: {outcome.total} | "
        f"Created: {outcome.created} | Skipped: {outcome.skipped} | "
        f"Disabled: {outcome.disabled} | Created-Flagged: {outcome.created_flagged} | "
        f"FNC: {outcome.flagged_not_created} | Drift: {outcome.drift_count} | "
        f"Failed: {outcome.failed}"
    )
    if outcome.final_cursor:
        summary += " | Partial walk — cursor preserved; re-run to resume."
    notify_discover_complete(
        triggered_by=triggered_by,
        kind="Suppliers",
        ok=True,
        summary=summary,
        list_route="/app/easyecom-supplier-map",
    )
