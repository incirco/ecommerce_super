"""§11 Phase 1 — Customer block builder tests.

Sources:
  customerId        ← §8e Customer Map (mock the resolver)
  billing address   ← Customer.customer_primary_address (refuse if missing)
  shipping address  ← SO.shipping_address_name OR customer's primary
  lat/long          ← shipping Address.ecs_latitude/_longitude (Custom
                      Fields; absent on this codebase, safe getattr no-op)
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.customer_block import (
    build_customer_block,
)


def _make_customer(
    name: str = "ACME",
    customer_name: str = "ACME Industries",
    primary_address: str | None = "ACME-Billing-Address",
    mobile_no: str | None = "9000000000",
    email_id: str | None = "ops@acme.example",
) -> MagicMock:
    c = MagicMock()
    c.name = name
    c.customer_name = customer_name
    c.customer_primary_address = primary_address
    c.mobile_no = mobile_no
    c.email_id = email_id
    return c


def _make_address(
    address_line1: str = "Plot 42, Industrial Estate",
    address_line2: str = "Sector 7",
    pincode: str = "560001",
    city: str = "Bengaluru",
    state: str = "Karnataka",
    country: str = "India",
    phone: str | None = None,
    email_id: str | None = None,
    ecs_latitude: str | None = None,
    ecs_longitude: str | None = None,
) -> MagicMock:
    a = MagicMock()
    a.address_line1 = address_line1
    a.address_line2 = address_line2
    a.pincode = pincode
    a.city = city
    a.state = state
    a.country = country
    a.phone = phone
    a.email_id = email_id
    # Default to NOT having the geo fields (matches real codebase).
    if ecs_latitude is None:
        # getattr(addr, "ecs_latitude", None) must return None.
        # MagicMock auto-creates attributes, so explicitly remove.
        del a.ecs_latitude
    else:
        a.ecs_latitude = ecs_latitude
    if ecs_longitude is None:
        del a.ecs_longitude
    else:
        a.ecs_longitude = ecs_longitude
    return a


def _make_so(
    name: str = "SAL-ORD-2026-001",
    customer: str = "ACME",
    shipping_address_name: str | None = None,
) -> MagicMock:
    so = MagicMock()
    so.name = name
    so.customer = customer
    so.shipping_address_name = shipping_address_name
    return so


class TestBuildCustomerBlock(unittest.TestCase):
    def _run(
        self,
        *,
        so,
        customer,
        billing_addr,
        shipping_addr,
        customer_id="EE-CUST-9001",
        include_lat_long=False,
    ):
        # frappe.get_doc dispatches by doctype + name; mock the order:
        #   1st call: Customer
        #   2nd call: Address (billing)
        #   3rd call: Address (shipping)
        get_doc = MagicMock(
            side_effect=[customer, billing_addr, shipping_addr]
        )
        with (
            patch.object(frappe, "get_doc", get_doc),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value=customer_id,
            ),
        ):
            return build_customer_block(so, include_lat_long=include_lat_long)

    def test_billing_and_shipping_both_present_on_so(self) -> None:
        """Happy path: SO has its own shipping address."""
        customer = _make_customer(primary_address="BILLING-ADDR")
        billing = _make_address(
            address_line1="Billing Lane", city="Bengaluru"
        )
        shipping = _make_address(
            address_line1="Shipping Lane", city="Hosur"
        )
        so = _make_so(shipping_address_name="SHIPPING-ADDR")
        result = self._run(
            so=so,
            customer=customer,
            billing_addr=billing,
            shipping_addr=shipping,
        )
        self.assertEqual(result["customerId"], "EE-CUST-9001")
        self.assertEqual(result["billing"]["addressLine1"], "Billing Lane")
        self.assertEqual(result["billing"]["city"], "Bengaluru")
        self.assertEqual(result["shipping"]["addressLine1"], "Shipping Lane")
        self.assertEqual(result["shipping"]["city"], "Hosur")

    def test_shipping_defaults_to_customer_primary_when_so_has_none(
        self,
    ) -> None:
        """SO with no shipping_address_name → both billing and
        shipping use customer's primary address."""
        customer = _make_customer(primary_address="BILLING-ADDR")
        primary = _make_address(
            address_line1="Primary Lane", city="Bengaluru"
        )
        # When shipping_address_name is None, the code uses
        # billing_addr_name for shipping too — frappe.get_doc gets
        # called only twice (Customer + Address-once), but the
        # MagicMock side_effect needs three; pass the same address
        # twice so the second/third gets the right thing.
        so = _make_so(shipping_address_name=None)
        result = self._run(
            so=so,
            customer=customer,
            billing_addr=primary,
            shipping_addr=primary,
        )
        self.assertEqual(
            result["billing"]["addressLine1"], "Primary Lane"
        )
        self.assertEqual(
            result["shipping"]["addressLine1"], "Primary Lane"
        )

    def test_missing_billing_address_throws_with_specific_message(
        self,
    ) -> None:
        """Refusal contract: missing primary address → throw with the
        exact §11 packet refusal text."""
        customer = _make_customer(primary_address=None)
        so = _make_so()
        get_doc = MagicMock(return_value=customer)
        with (
            patch.object(frappe, "get_doc", get_doc),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value="EE-CUST-9001",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            build_customer_block(so, include_lat_long=False)
        msg = str(exc_ctx.exception)
        self.assertIn("primary billing address", msg)
        self.assertIn("ACME", msg)

    def test_lat_long_omitted_when_geo_fields_absent(self) -> None:
        """include_lat_long=True but Address has no ecs_latitude /
        ecs_longitude Custom Fields → block omits lat/lng. Safe
        no-op (current codebase has no geo Custom Fields)."""
        customer = _make_customer(primary_address="BILLING")
        billing = _make_address()
        shipping = _make_address()
        so = _make_so(shipping_address_name="SHIPPING")
        result = self._run(
            so=so,
            customer=customer,
            billing_addr=billing,
            shipping_addr=shipping,
            include_lat_long=True,
        )
        self.assertNotIn("latitude", result["shipping"])
        self.assertNotIn("longitude", result["shipping"])

    def test_lat_long_emitted_when_geo_fields_present(self) -> None:
        """When the Address DOES carry ecs_latitude/_longitude (future
        Custom Fields), include_lat_long=True emits them as strings."""
        customer = _make_customer(primary_address="BILLING")
        billing = _make_address()
        shipping = _make_address(
            ecs_latitude="12.9716", ecs_longitude="77.5946"
        )
        so = _make_so(shipping_address_name="SHIPPING")
        result = self._run(
            so=so,
            customer=customer,
            billing_addr=billing,
            shipping_addr=shipping,
            include_lat_long=True,
        )
        self.assertEqual(result["shipping"]["latitude"], "12.9716")
        self.assertEqual(result["shipping"]["longitude"], "77.5946")

    def test_contact_falls_through_customer_to_address(self) -> None:
        """Customer's mobile_no preferred; falls back to Address.phone
        when customer has no mobile."""
        customer = _make_customer(mobile_no=None)
        billing = _make_address(phone="0800-111-2222")
        shipping = _make_address(phone="0800-333-4444")
        so = _make_so(shipping_address_name="SHIPPING")
        result = self._run(
            so=so,
            customer=customer,
            billing_addr=billing,
            shipping_addr=shipping,
        )
        # Address.phone takes over since customer.mobile_no is None.
        self.assertEqual(result["billing"]["contact"], "0800-111-2222")
        self.assertEqual(result["shipping"]["contact"], "0800-333-4444")


if __name__ == "__main__":
    unittest.main()
