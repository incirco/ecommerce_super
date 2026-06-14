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
    try:
        response = client.post(
            CANCEL_ORDER,
            payload={"reference_code": so.name},
            correlation_id=correlation_id,
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        frappe.throw(
            _(
                "EasyEcom cancellation request failed: {0}. "
                "Correlation ID: {1}. The B2B order on EE side has NOT "
                "been cancelled — the local Map row remains in {2}."
            ).format(
                f"{type(exc).__name__}: {exc}",
                correlation_id,
                map_doc.status,
            ),
            title=_("EE Cancellation Failed"),
        )

    if int(response.get("code") or 0) != 200:
        ee_msg = response.get("message") or "Unknown EE error"
        frappe.throw(
            _(
                "EasyEcom returned a non-200 response on cancellation: "
                "{0}. Correlation ID: {1}. Local Map row remains in {2}."
            ).format(ee_msg, correlation_id, map_doc.status),
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
