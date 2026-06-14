"""Customer + address block builder for the §11 createOrder payload.

EE expects a single-element `customer` array carrying a customerId
and a {billing, shipping} pair. Both addresses are required for the
GST invoice EE generates on Old B2B (and for the address validation
the New B2B queue runs before promoting the order).

Sources:
  - customerId        ← §8e Customer Map (resolved via helpers.master_resolution)
  - billing address   ← Customer.customer_primary_address (must be set; refuses otherwise)
  - shipping address  ← SO.shipping_address_name if set, else customer_primary_address
  - lat/long          ← Address.ecs_latitude / .ecs_longitude (Custom Fields; absent
                        on this codebase — getattr safely no-ops). Only emitted when
                        include_lat_long=True (Old B2B) AND both values are present.

Refusal discipline: missing billing address throws with the exact
title + message from §11 packet's refusal table (§11.2). Missing
shipping address falls back to billing — that's the documented
behaviour and matches §10's pattern.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_super.easyecom.helpers.master_resolution import (
    resolve_ee_customer_id,
)


def build_customer_block(so: Any, *, include_lat_long: bool = False) -> dict:
    """Build EE's `customer` array element for a Sales Order.

    Args:
        so: Sales Order document (or any object exposing customer,
            shipping_address_name).
        include_lat_long: Old B2B sample includes latitude/longitude
            in the shipping block; New B2B doesn't. Only populated
            when the shipping Address has the geo Custom Fields
            (currently absent — call site safely no-ops).
    """
    customer = frappe.get_doc("Customer", so.customer)

    billing_addr_name = customer.customer_primary_address
    if not billing_addr_name:
        frappe.throw(
            _(
                "Customer {0} has no primary billing address. B2B GST "
                "invoice cannot be generated without billing address."
            ).format(so.customer),
            title=_("Billing Address Missing"),
        )
    billing_addr = frappe.get_doc("Address", billing_addr_name)

    shipping_addr_name = so.shipping_address_name or billing_addr_name
    shipping_addr = frappe.get_doc("Address", shipping_addr_name)

    customer_id = resolve_ee_customer_id(so.customer)

    block: dict[str, Any] = {
        "customerId": customer_id,
        "billing": _flatten_address(billing_addr, customer),
        "shipping": _flatten_address(shipping_addr, customer),
    }

    if include_lat_long:
        lat = getattr(shipping_addr, "ecs_latitude", None)
        lng = getattr(shipping_addr, "ecs_longitude", None)
        if lat and lng:
            block["shipping"]["latitude"] = str(lat)
            block["shipping"]["longitude"] = str(lng)

    return block


def _flatten_address(addr: Any, customer: Any) -> dict:
    """Render a Frappe Address as EE's flat address shape.

    Falls through customer-level contact when the Address-level
    contact / email isn't set; matches the real-world pattern where
    Address is a physical location and the Customer carries the
    relationship-level contact.
    """
    return {
        "name": customer.customer_name,
        "addressLine1": addr.address_line1 or "",
        "addressLine2": addr.address_line2 or "",
        "postalCode": addr.pincode or "",
        "city": addr.city or "",
        "state": addr.state or "",
        "country": addr.country or "India",
        "contact": customer.mobile_no or addr.phone or "",
        "email": customer.email_id or addr.email_id or "",
    }
