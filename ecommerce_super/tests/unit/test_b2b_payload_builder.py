"""§11 Phase 1 — Payload builder tests (Old B2B + New B2B).

Module-level concerns:
  - Old B2B: Quantity is STRING; paymentGateway present; lat/long
    block. Refuses URP customers.
  - New B2B: Quantity is INTEGER; no paymentGateway, no lat/long;
    is_pricing_master=false; queue=1; URP fallback for missing GSTIN.
  - compute_payload_hash: SHA-256 of canonical JSON; deterministic
    under key reordering and across runs.

Tests mock master_resolution (no DB) and frappe.get_doc (Customer
lookups inside the builders). Payment derivation is mocked too so
each test asserts payload SHAPE, not payment logic.
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.payload_builder import (
    build_new_b2b_item,
    build_new_b2b_payload,
    build_old_b2b_item,
    build_old_b2b_payload,
    compute_payload_hash,
    get_shipping_charge,
    resolve_ee_sku_or_throw,
)


def _make_so(
    name="SAL-ORD-2026-001",
    customer="ACME",
    grand_total=10000.0,
    transaction_date=None,
    delivery_date=None,
    terms="Net 30",
    discount_amount=0,
    taxes=None,
    items=None,
    shipping_address_name=None,
):
    so = MagicMock()
    so.name = name
    so.customer = customer
    so.grand_total = grand_total
    so.transaction_date = transaction_date or date(2026, 6, 14)
    so.delivery_date = delivery_date or date(2026, 6, 20)
    so.terms = terms
    so.discount_amount = discount_amount
    so.taxes = taxes or []
    so.items = items or []
    so.shipping_address_name = shipping_address_name
    return so


def _make_so_item(
    item_code="WIDGET-001",
    item_name="Stainless Widget",
    qty=5,
    rate=200.0,
    idx=1,
    discount_amount=0,
):
    it = MagicMock()
    it.item_code = item_code
    it.item_name = item_name
    it.qty = qty
    it.rate = rate
    it.idx = idx
    it.discount_amount = discount_amount
    return it


def _make_tax(account_head, tax_amount):
    t = MagicMock()
    t.account_head = account_head
    t.tax_amount = tax_amount
    return t


def _make_customer(
    name="ACME",
    customer_name="ACME Industries",
    tax_id="29ABCDE1234F1Z5",
    primary_address="ACME-Billing",
    mobile_no="9000000000",
    email_id="ops@acme.example",
):
    c = MagicMock()
    c.name = name
    c.customer_name = customer_name
    c.tax_id = tax_id
    c.customer_primary_address = primary_address
    c.mobile_no = mobile_no
    c.email_id = email_id
    return c


def _make_address():
    a = MagicMock()
    a.address_line1 = "Plot 42"
    a.address_line2 = "Sector 7"
    a.pincode = "560001"
    a.city = "Bengaluru"
    a.state = "Karnataka"
    a.country = "India"
    a.phone = None
    a.email_id = None
    # No geo Custom Fields on this codebase.
    del a.ecs_latitude
    del a.ecs_longitude
    return a


_PAYMENT_PREPAID = {
    "paymentMode": 5,
    "paymentTransactionNumber": "UTR-XYZ",
    "collectableAmount": 0.0,
    "shippingMethod": 3,
}
_PAYMENT_CREDIT = {
    "paymentMode": 2,
    "paymentTransactionNumber": "",
    "collectableAmount": 10000.0,
    "shippingMethod": 1,
}


class TestResolveEeSkuOrThrow(unittest.TestCase):
    def test_returns_sku_when_mapped(self) -> None:
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
            return_value="EE-SKU-999",
        ):
            self.assertEqual(
                resolve_ee_sku_or_throw("WIDGET-001"), "EE-SKU-999"
            )

    def test_throws_with_specific_message_when_unmapped(self) -> None:
        with (
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
                return_value=None,
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            resolve_ee_sku_or_throw("UNMAPPED-001")
        self.assertIn("UNMAPPED-001", str(exc_ctx.exception))
        self.assertIn("not synced", str(exc_ctx.exception))


class TestGetShippingCharge(unittest.TestCase):
    def test_substring_match_on_account_head(self) -> None:
        so = _make_so(
            taxes=[
                _make_tax("Shipping Charges - TC", 250.0),
                _make_tax("CGST - TC", 90.0),
            ]
        )
        self.assertEqual(get_shipping_charge(so), 250.0)

    def test_case_insensitive(self) -> None:
        so = _make_so(taxes=[_make_tax("SHIPPING - TC", 100.0)])
        self.assertEqual(get_shipping_charge(so), 100.0)

    def test_zero_when_no_shipping_tax(self) -> None:
        so = _make_so(taxes=[_make_tax("CGST - TC", 90.0)])
        self.assertEqual(get_shipping_charge(so), 0.0)

    def test_zero_when_no_taxes_at_all(self) -> None:
        so = _make_so(taxes=[])
        self.assertEqual(get_shipping_charge(so), 0.0)


class TestBuildOldB2bItem(unittest.TestCase):
    def test_quantity_is_string(self) -> None:
        """Old B2B's documented quirk — Quantity rendered as a string."""
        so = _make_so()
        item = _make_so_item(qty=5)
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
            return_value="EE-SKU-001",
        ):
            result = build_old_b2b_item(so, item)
        self.assertEqual(result["Quantity"], "5")
        self.assertIsInstance(result["Quantity"], str)

    def test_order_item_id_includes_idx(self) -> None:
        so = _make_so(name="SAL-ORD-100")
        item = _make_so_item(idx=3)
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
            return_value="EE-SKU-001",
        ):
            result = build_old_b2b_item(so, item)
        self.assertEqual(result["OrderItemId"], "SAL-ORD-100-line-3")


class TestBuildNewB2bItem(unittest.TestCase):
    def test_quantity_is_integer(self) -> None:
        """New B2B's documented quirk — Quantity is an integer."""
        so = _make_so()
        item = _make_so_item(qty=5)
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
            return_value="EE-SKU-001",
        ):
            result = build_new_b2b_item(so, item)
        self.assertEqual(result["Quantity"], 5)
        self.assertIsInstance(result["Quantity"], int)

    def test_no_product_name_field(self) -> None:
        """New B2B doesn't carry productName at the line item level."""
        so = _make_so()
        item = _make_so_item()
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
            return_value="EE-SKU-001",
        ):
            result = build_new_b2b_item(so, item)
        self.assertNotIn("productName", result)


class TestBuildOldB2bPayload(unittest.TestCase):
    def _run_with_prepaid(self, customer, items, taxes=None):
        billing = _make_address()
        shipping = _make_address()
        so = _make_so(items=items, taxes=taxes or [])
        get_doc = MagicMock(side_effect=[customer, customer, billing, shipping])
        with (
            patch.object(frappe, "get_doc", get_doc),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.derive_payment_fields",
                return_value=_PAYMENT_PREPAID,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
                return_value="EE-SKU-001",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value="EE-CUST-9001",
            ),
        ):
            return build_old_b2b_payload(so, MagicMock())

    def test_invariants_locked_in_payload(self) -> None:
        customer = _make_customer()
        result = self._run_with_prepaid(
            customer=customer, items=[_make_so_item()]
        )
        self.assertEqual(result["orderType"], "businessorder")
        self.assertEqual(result["is_market_shipped"], 0)
        # Old B2B carries paymentGateway even when empty.
        self.assertIn("paymentGateway", result)
        self.assertEqual(result["paymentGateway"], "")

    def test_payment_fields_propagated(self) -> None:
        customer = _make_customer()
        result = self._run_with_prepaid(
            customer=customer, items=[_make_so_item()]
        )
        self.assertEqual(result["paymentMode"], 5)
        self.assertEqual(result["paymentTransactionNumber"], "UTR-XYZ")
        self.assertEqual(result["collectableAmount"], 0.0)
        self.assertEqual(result["shippingMethod"], 3)

    def test_customer_block_is_single_element_array(self) -> None:
        customer = _make_customer()
        result = self._run_with_prepaid(
            customer=customer, items=[_make_so_item()]
        )
        self.assertIsInstance(result["customer"], list)
        self.assertEqual(len(result["customer"]), 1)
        self.assertEqual(
            result["customer"][0]["customerId"], "EE-CUST-9001"
        )

    def test_credit_terms_renders_collectable_amount(self) -> None:
        customer = _make_customer()
        billing = _make_address()
        shipping = _make_address()
        so = _make_so(items=[_make_so_item()])
        get_doc = MagicMock(side_effect=[customer, customer, billing, shipping])
        with (
            patch.object(frappe, "get_doc", get_doc),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.derive_payment_fields",
                return_value=_PAYMENT_CREDIT,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
                return_value="EE-SKU-001",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value="EE-CUST-9001",
            ),
        ):
            result = build_old_b2b_payload(so, MagicMock())
        self.assertEqual(result["paymentMode"], 2)
        self.assertEqual(result["collectableAmount"], 10000.0)
        self.assertEqual(result["shippingMethod"], 1)

    def test_missing_gstin_throws_with_specific_message(self) -> None:
        """Old B2B refuses URP customers — refusal text from packet."""
        customer = _make_customer(tax_id=None)
        billing = _make_address()
        shipping = _make_address()
        so = _make_so(items=[_make_so_item()])
        get_doc = MagicMock(side_effect=[customer, billing, shipping])
        with (
            patch.object(frappe, "get_doc", get_doc),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.derive_payment_fields",
                return_value=_PAYMENT_PREPAID,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
                return_value="EE-SKU-001",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value="EE-CUST-9001",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            build_old_b2b_payload(so, MagicMock())
        self.assertIn("GSTIN", str(exc_ctx.exception))
        self.assertIn("URP", str(exc_ctx.exception))


class TestBuildNewB2bPayload(unittest.TestCase):
    def _run(self, customer, items):
        billing = _make_address()
        shipping = _make_address()
        so = _make_so(items=items)
        get_doc = MagicMock(side_effect=[customer, customer, billing, shipping])
        with (
            patch.object(frappe, "get_doc", get_doc),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.derive_payment_fields",
                return_value=_PAYMENT_PREPAID,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
                return_value="EE-SKU-001",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value="EE-CUST-9001",
            ),
        ):
            return build_new_b2b_payload(so, MagicMock())

    def test_invariants_locked_in_payload(self) -> None:
        customer = _make_customer()
        result = self._run(customer=customer, items=[_make_so_item()])
        self.assertEqual(result["orderType"], "businessorder")
        self.assertEqual(result["is_market_shipped"], 0)
        self.assertEqual(result["is_pricing_master"], False)
        self.assertEqual(result["queue"], 1)
        # New B2B does NOT carry paymentGateway / packageWeight etc.
        self.assertNotIn("paymentGateway", result)
        self.assertNotIn("packageWeight", result)
        self.assertNotIn("expDeliveryDate", result)

    def test_urp_fallback_when_no_gstin(self) -> None:
        """New B2B accepts unregistered customers — taxIdentificationNumber
        becomes "URP" rather than throwing."""
        customer = _make_customer(tax_id=None)
        result = self._run(customer=customer, items=[_make_so_item()])
        self.assertEqual(result["taxIdentificationNumber"], "URP")

    def test_uses_customer_gstin_when_present(self) -> None:
        customer = _make_customer(tax_id="29ABCDE1234F1Z5")
        result = self._run(customer=customer, items=[_make_so_item()])
        self.assertEqual(
            result["taxIdentificationNumber"], "29ABCDE1234F1Z5"
        )


class TestComputePayloadHash(unittest.TestCase):
    def test_deterministic_under_key_reordering(self) -> None:
        """Same data + different insertion order → same hash."""
        h1 = compute_payload_hash({"a": 1, "b": 2, "c": [3, 4]})
        h2 = compute_payload_hash({"c": [3, 4], "b": 2, "a": 1})
        self.assertEqual(h1, h2)

    def test_hash_is_64_hex_chars(self) -> None:
        h = compute_payload_hash({"orderNumber": "SAL-001"})
        self.assertEqual(len(h), 64)
        # Must be hex.
        int(h, 16)

    def test_different_values_produce_different_hashes(self) -> None:
        h1 = compute_payload_hash({"orderNumber": "SAL-001"})
        h2 = compute_payload_hash({"orderNumber": "SAL-002"})
        self.assertNotEqual(h1, h2)

    def test_handles_nested_structures(self) -> None:
        """Lists and nested dicts canonicalise cleanly — JSON-encoder
        sorts at every dict level."""
        h1 = compute_payload_hash(
            {"items": [{"Sku": "A"}, {"Sku": "B"}]}
        )
        h2 = compute_payload_hash(
            {"items": [{"Sku": "A"}, {"Sku": "B"}]}
        )
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
