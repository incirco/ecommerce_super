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
    EasyEcomError,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_location_for_warehouse,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id


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
    ) if is_shipped_refusal else "B2B cancellation refused by EE"

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
