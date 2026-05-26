"""FDE-facing whitelisted endpoints for §8e Stage 4 customer push.

Mirrors §8d Item push API surface — individual push (called from a
"Push to EasyEcom" button on Customer form) + batch sweep (called from
the EasyEcom Account form). Never raises through the whitelist.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.customer_push import (
    enqueue_push_all_pending,
    push_one_customer,
)


@frappe.whitelist()
def push_one_customer_now(customer_docname: str) -> dict[str, Any]:
    """FDE-facing individual push. Used by the "Push to EasyEcom" button
    on a Customer form. Inline (not queued) so the FDE sees the result
    immediately; for batch use push_all_pending_customers."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._("Push Customer requires EasyEcom FDE or System Manager."),
            frappe.PermissionError,
        )
    if not customer_docname:
        return {"ok": False, "message": "customer_docname required"}
    if not frappe.db.exists("Customer", customer_docname):
        return {
            "ok": False,
            "message": frappe._("Customer {0} not found.").format(customer_docname),
        }

    try:
        outcome = push_one_customer(customer_docname)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=f"push_one_customer failed: {customer_docname}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "customer_docname": outcome.customer_docname,
        "operation": outcome.operation,
        "pushed": outcome.pushed,
        "ee_customer_id": outcome.ee_customer_id,
        "flag_reasons": outcome.flag_reasons,
    }


@frappe.whitelist()
def push_all_pending_customers(account: str) -> dict[str, Any]:
    """FDE-facing batch sweep — enqueues one Queue Job per candidate
    Customer. Returns immediately with the count enqueued; per-Customer
    progress lands in Sync Records / Queue Job rows.

    Mirrors §8d push_all_pending_products.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Push All Pending Customers requires EasyEcom FDE or "
                "System Manager."
            ),
            frappe.PermissionError,
        )
    if not account:
        return {"ok": False, "message": "account required"}

    try:
        result = enqueue_push_all_pending(account_name=account)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title="push_all_pending_customers failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "total_considered": result["total_considered"],
        "enqueued_count": result["enqueued_count"],
        "queue_job_names_sample": result["queue_job_names_sample"],
    }
