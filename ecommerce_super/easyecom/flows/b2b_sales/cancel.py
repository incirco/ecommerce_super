"""§11 Stage 2 — ERPNext-initiated B2B cancellation.

Whitelisted endpoint called from the SO form's "Cancel on EasyEcom"
button. Cancellation is allowed only while the Map row is in a pre-
invoice-generation state — once EE has generated an invoice, EE's
own cancellation flow applies (Phase 2 work).

EE endpoint (grounded per design-lead's EE-doc reference):
  POST {{BaseURL}}/orders/cancelOrder
  Headers: x-api-key (mandatory) + Authorization: Bearer <Jwt>
  Payload: {"reference_code": "<SO name>"}
  Response shape on success:
    {"code": 200, "message": "Successfully Cancelled the Order with
     reference_code <SO name>", "data": []}

Identifier choice: `reference_code = SO.name = orderNumber sent at
createOrder`. Works uniformly for Old B2B and New B2B — no module
dispatch needed.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import CANCEL_ORDER
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomAuthError,
    EasyEcomError,
    EasyEcomRateLimitError,
    EasyEcomServerError,
    EasyEcomTimeoutError,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_location_for_warehouse,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id


# Infrastructure-failure exception types (per packet's
# accept/business-refuse/infra-fail trichotomy). These are network /
# auth / server-side conditions — NOT a business cancellation refusal.
# They must produce a Failed Sync Record (not a Discrepancy) and a
# distinct "EasyEcom unreachable" throw so the FDE knows to retry
# rather than escalate to §13.3 RTO flow.
_INFRA_FAILURE_TYPES: tuple[type[Exception], ...] = (
    EasyEcomTimeoutError,
    EasyEcomServerError,
    EasyEcomAuthError,
    EasyEcomRateLimitError,
)


CANCELLABLE_STATUSES: frozenset[str] = frozenset({"Pushed", "Queued"})

# Substrings EE uses when refusing a cancel because the order is
# already shipped or past the cancel window (FAQ #32 / #41). Defensive
# substring match — EE's exact phrasing varies by message variant.
_SHIPPED_REFUSAL_HINTS: tuple[str, ...] = (
    "already shipped",
    "shipped",
    "cannot be cancelled",
    "cancel window",
    "rto",
    "in transit",
)


def _looks_like_shipped_state_refusal(message: str | None) -> bool:
    if not message:
        return False
    lower = str(message).lower()
    return any(hint in lower for hint in _SHIPPED_REFUSAL_HINTS)


def _raise_b2b_cancel_refusal_discrepancy(
    *,
    map_doc: Any,
    ee_account_name: str,
    correlation_id: str,
    ee_message: str,
    ee_response_body: dict,
    is_shipped_refusal: bool,
) -> None:
    """Raise the §11 cancel-refusal Discrepancy. Uses the §9 pattern
    (_raise_discrepancy with all context inlined into the reason
    string). The kind string is filtered on by the FDE Worklist's
    'B2B cancellation refused' card — keep verbatim."""
    from ecommerce_super.easyecom.flows.grn_pull import _raise_discrepancy

    company = frappe.db.get_value(
        "Sales Order", map_doc.sales_order, "company"
    )
    try:
        body_excerpt = frappe.as_json(ee_response_body)[:2000]
    except Exception:
        body_excerpt = str(ee_response_body)[:2000]

    kind = (
        "B2B cancellation refused by EE — order already shipped "
        "or past cancel window"
    ) if is_shipped_refusal else (
        "B2B cancellation refused by EE — unexpected error"
    )

    _raise_discrepancy(
        kind=kind,
        reference_doctype="EasyEcom B2B Order Map",
        reference_name=map_doc.name,
        company=company or "",
        reason=(
            f"§11 ERPNext-initiated cancellation refused. EE Account "
            f"{ee_account_name}, correlation_id={correlation_id}, "
            f"sales_order={map_doc.sales_order}, map_status="
            f"{map_doc.status}. EE message: {ee_message[:500]}. "
            f"EE response body excerpt: {body_excerpt}"
        ),
    )


@frappe.whitelist()
def cancel_b2b_order_from_erpnext(sales_order: str) -> dict:
    """Cancel an §11-pushed B2B order from the ERPNext side.

    Refuses if the SO has no Map row or the Map is past the
    pre-invoice-generation states. On success: transitions Map to
    Cancelled, stamps cancelled_at, persists EE response in the
    audit fields. SO itself is NOT cancelled — that's a separate
    decision the FDE makes via ERPNext's standard cancel flow.

    Returns: {"ok": bool, "map_name": str, "ee_message": str}
    """
    so = frappe.get_doc("Sales Order", sales_order)

    map_name = so.get("ecs_b2b_order_map")
    if not map_name:
        frappe.throw(
            _(
                "Sales Order {0} has no §11 push to cancel."
            ).format(sales_order),
            title=_("No B2B Push to Cancel"),
        )

    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)

    if map_doc.status not in CANCELLABLE_STATUSES:
        frappe.throw(
            _(
                "Cannot cancel B2B order in status {0!r}. Cancellation "
                "is allowed only before invoice generation. Use EE's "
                "cancellation flow if needed."
            ).format(map_doc.status),
            title=_("Cannot Cancel"),
        )

    ee_account = frappe.get_doc(
        "EasyEcom Account", map_doc.easyecom_account
    )
    ee_location = get_ee_location_for_warehouse(so.set_warehouse)
    location_key = (
        str(getattr(ee_location, "location_key", "")) if ee_location else ""
    )
    correlation_id = new_correlation_id()

    client = EasyEcomClient(location_key=location_key)
    response: dict = {}
    exc_info: str | None = None
    try:
        response = client.post(
            CANCEL_ORDER,
            payload={"reference_code": so.name},
            correlation_id=correlation_id,
        )
    except _INFRA_FAILURE_TYPES as exc:
        # Infrastructure failure (timeout / 5xx / auth-expired /
        # rate-limit). Per §7.3 + packet: infra failure → Failed Sync
        # Record (NOT a Discrepancy) + distinct "unreachable" throw.
        # The local Map row is not transitioned; the SO must NOT be
        # allowed to cancel locally (no divergence window).
        _write_cancel_sync_record(
            so=so, ee_account=ee_account,
            location_key=location_key,
            correlation_id=correlation_id,
            status="Failed",
            last_error=f"{type(exc).__name__}: {str(exc)[:500]}",
        )
        frappe.throw(
            _(
                "EasyEcom unreachable — cancellation not propagated; "
                "retry. {0}. Correlation ID: {1}. Local Map row "
                "remains in {2}."
            ).format(
                f"{type(exc).__name__}: {str(exc)[:200]}",
                correlation_id,
                map_doc.status,
            ),
            title=_("EasyEcom Unreachable"),
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        # Capture the EE error body if present — the client wraps
        # HTTP-200-with-body-code>=400 responses as ValidationError
        # with response_body attached.
        body = getattr(exc, "response_body", None)
        exc_info = f"{type(exc).__name__}: {exc}"
        if isinstance(body, dict):
            response = body
    if not response and not exc_info:
        exc_info = "no response and no exception (client returned empty)"

    code = int(response.get("code") or 0) if isinstance(response, dict) else 0
    if code != 200:
        ee_msg = (
            response.get("message")
            if isinstance(response, dict)
            else None
        ) or exc_info or "Unknown EE error"
        is_shipped_refusal = _looks_like_shipped_state_refusal(ee_msg)
        # Raise an Integration Discrepancy so the FDE worklist surfaces
        # the failure (shipped-state refusal especially is operationally
        # important — the FDE needs to switch to the RTO flow).
        _raise_b2b_cancel_refusal_discrepancy(
            map_doc=map_doc,
            ee_account_name=ee_account.name,
            correlation_id=correlation_id,
            ee_message=str(ee_msg),
            ee_response_body=response,
            is_shipped_refusal=is_shipped_refusal,
        )
        if is_shipped_refusal:
            frappe.throw(
                _(
                    "EasyEcom refused the cancellation — the order is "
                    "already shipped or past the cancel window. EE: "
                    "{0}. The local Map row remains in {1}. A "
                    "Discrepancy has been raised for FDE follow-up "
                    "(RTO flow, not Phase 1 territory)."
                ).format(str(ee_msg)[:200], map_doc.status),
                title=_("EE Cancellation Refused — Already Shipped"),
            )
        frappe.throw(
            _(
                "EasyEcom returned a non-200 response on cancellation: "
                "{0}. Correlation ID: {1}. Local Map row remains in "
                "{2}. A Discrepancy has been raised for FDE follow-up."
            ).format(str(ee_msg)[:200], correlation_id, map_doc.status),
            title=_("EE Cancellation Failed"),
        )

    # Transition Map → Cancelled. Preserves ee_order_id / ee_suborder_id
    # / ee_invoice_id for audit (cancellation does NOT erase the EE
    # identifiers that were captured at push time).
    frappe.db.set_value(
        "EasyEcom B2B Order Map",
        map_doc.name,
        {
            "status": "Cancelled",
            "cancelled_at": frappe.utils.now(),
            "response_payload": frappe.as_json(response),
            "last_error": None,
        },
        update_modified=True,
    )
    frappe.db.commit()

    return {
        "ok": True,
        "map_name": map_doc.name,
        "ee_message": response.get("message", ""),
    }


def _write_cancel_sync_record(
    *,
    so: Any,
    ee_account: Any,
    location_key: str,
    correlation_id: str,
    status: str,
    last_error: str | None = None,
) -> str | None:
    """§7 Sync Record for the cancel attempt. Mirrors push.py's
    `_write_sync_record` shape — direction is "Cancel", status is one
    of {"Pending", "Success", "Failed", "Discrepancy"}. Used by the
    infra-failure path so the FDE Worklist surfaces unreachable
    EasyEcom errors as Failed (not Discrepancy, which would route to
    the §13.3 RTO flow incorrectly)."""
    try:
        from ecommerce_super.easyecom.doctype.easyecom_sync_record import (
            easyecom_sync_record as sync_record_mod,
        )
        from ecommerce_super.easyecom.utils.idempotency import (
            sha256_idempotency,
        )
        idem = sha256_idempotency(
            "so-cancel", so.company, so.name, location_key or "",
        )
        sr = sync_record_mod.upsert(
            company=so.company,
            entity_doctype="Sales Order",
            entity_name=so.name,
            entity_type="Sales Order",
            direction="Cancel",
            correlation_id=correlation_id,
            idempotency_key=idem,
            ee_location_key=location_key or None,
            status=status,
        )
        if status != "Pending":
            sr_name = sr.name if hasattr(sr, "name") else sr
            updates: dict = {"status": status}
            if last_error is not None:
                updates["last_error"] = last_error[:5000]
            frappe.db.set_value(
                "EasyEcom Sync Record", sr_name, updates,
                update_modified=False,
            )
        return sr.name if hasattr(sr, "name") else sr
    except Exception as exc:
        frappe.log_error(
            title=f"§11 cancel failed to write Sync Record for {so.name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return None


# ============================================================
# Document cancel hook (Sales Order before_cancel)
# ============================================================
#
# Wired in hooks.py doc_events["Sales Order"]["before_cancel"]. The
# hook runs BEFORE Frappe transitions docstatus to 2, so a throw here
# leaves the SO untouched (docstatus=1) — which is exactly what we
# want on a business-refusal or infra-failure: no divergence between
# ERPNext and EasyEcom.
#
# Scope guard: any SO that is not §11-pushed (no Map row, or no
# `ecs_b2b_order_map` back-ref) is untouched — vanilla ERPNext
# cancellation proceeds with zero EE traffic and zero Sync Records.
# Required by the §11 Phase 1 live-smoke packet HARD RULE 5.
#
# Sync semantics (synchronous block-on-refusal):
#   - EE accepts  → cancel_b2b_order_from_erpnext returns clean →
#                   hook returns → Frappe proceeds to docstatus 2.
#   - EE refuses  → cancel_b2b_order_from_erpnext throws (with the
#                   existing Discrepancy already raised) → hook
#                   re-raises → docstatus stays 1.
#   - Infra fail  → cancel_b2b_order_from_erpnext throws with the
#                   distinct "unreachable" message (Failed Sync
#                   Record already raised) → hook re-raises →
#                   docstatus stays 1.
#
# Known edge (per packet — note, do NOT solve in Phase 1): on the
# accept path the EE side commits before the local transaction. If a
# later step in the cancel chain rolls back, EE is cancelled but the
# SO stays submitted. The §5 polling tick's orphan / unexpected-
# state detection is the safety net for this.


def on_before_cancel_dispatch(doc: Any, method: str | None = None) -> None:
    """Sales Order.before_cancel hook entry. Thin wrapper:
      1. Scope-guard: bail out if this SO is not §11-pushed.
      2. Else: call cancel_b2b_order_from_erpnext synchronously.
         Its existing throws on refusal / infra-failure propagate
         through this hook and block the local cancel.
    Does NOT swallow exceptions — the throw is the load-bearing
    refusal mechanism.
    """
    if doc.doctype != "Sales Order":
        return
    # Scope guard: only B2B-pushed SOs touch EE.
    map_name = doc.get("ecs_b2b_order_map")
    if not map_name:
        return
    # Defensive: confirm Map row exists (a stale back-ref shouldn't
    # block cancel entirely).
    if not frappe.db.exists("EasyEcom B2B Order Map", map_name):
        return
    map_status = frappe.db.get_value(
        "EasyEcom B2B Order Map", map_name, "status"
    )
    if map_status not in CANCELLABLE_STATUSES:
        # Already Cancelled / Invoice Pending / etc — let the local
        # cancel proceed without an EE call. Cancellation on
        # already-cancelled is benign; on Invoice Pending the §13.3
        # post-dispatch flow is the right path (FDE will use that
        # surface, not the local cancel button).
        return
    # Hand off — the existing function carries all the EE-side logic
    # and the right exception semantics.
    cancel_b2b_order_from_erpnext(sales_order=doc.name)
