"""FDE-facing whitelisted endpoints for §8f Stage 4 supplier push.

Mirrors §8e Stage 4 customer_push API surface — individual push
(called from a "Push to EasyEcom" button on the Supplier form) + batch
sweep (called from the EasyEcom Account form). Never raises through
the whitelist boundary.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.supplier_push import (
    enqueue_push_all_pending,
    push_one_supplier,
)


@frappe.whitelist()
def push_one_supplier_now(supplier_docname: str) -> dict[str, Any]:
    """FDE-facing individual push. Inline (not queued) so the FDE sees
    the result immediately; for batch use push_all_pending_suppliers."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._("Push Supplier requires EasyEcom FDE or System Manager."),
            frappe.PermissionError,
        )
    if not supplier_docname:
        return {"ok": False, "message": "supplier_docname required"}
    if not frappe.db.exists("Supplier", supplier_docname):
        return {
            "ok": False,
            "message": frappe._("Supplier {0} not found.").format(supplier_docname),
        }

    try:
        outcome = push_one_supplier(supplier_docname)
    except Exception as exc:
        frappe.log_error(
            title=f"push_one_supplier failed: {supplier_docname}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "supplier_docname": outcome.supplier_docname,
        "operation": outcome.operation,
        "pushed": outcome.pushed,
        "ee_vendor_c_id": outcome.ee_vendor_c_id,
        "ee_vendor_id": outcome.ee_vendor_id,
        "flag_reasons": outcome.flag_reasons,
    }


@frappe.whitelist()
def push_all_pending_suppliers(account: str) -> dict[str, Any]:
    """FDE-facing batch sweep — enqueues one Queue Job per candidate
    Supplier. Returns immediately with the count enqueued; per-Supplier
    progress lands in Sync Records / Queue Job rows.

    Mirrors §8e push_all_pending_customers."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Push All Pending Suppliers requires EasyEcom FDE or "
                "System Manager."
            ),
            frappe.PermissionError,
        )
    if not account:
        return {"ok": False, "message": "account required"}

    try:
        result = enqueue_push_all_pending(account_name=account)
    except Exception as exc:
        frappe.log_error(
            title="push_all_pending_suppliers failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "total_considered": result["total_considered"],
        "enqueued_count": result["enqueued_count"],
        "queue_job_names_sample": result["queue_job_names_sample"],
    }
