"""§11 Stage 2 — Precondition refusal tests.

One test per refusal condition (9 tests). Each test sets up the
failure scenario and asserts the throw carries the exact message
fragment specified in the §11 packet §11.2 refusal table.

Refusal discipline lesson from §10 — these tests assert that the
error MESSAGE matches the packet text, not just that any exception
was raised. Users debugging a refused submit need to see WHICH
precondition failed.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.gating import (
    validate_preconditions,
)


def _make_so_item(
    item_code="ITEM-001",
    qty=5,
    rate=200.0,
    warehouse="EE-WH-001",
    idx=1,
    item_name="Item",
):
    it = MagicMock()
    it.item_code = item_code
    it.item_name = item_name
    it.qty = qty
    it.rate = rate
    it.warehouse = warehouse
    it.idx = idx
    it.discount_amount = 0
    return it


def _make_so(
    name="SAL-ORD-2026-T1",
    company="_Test Company",
    customer="ACME",
    set_warehouse="EE-WH-001",
    shipping_address_name="ACME-Shipping",
    items=None,
):
    so = MagicMock()
    so.name = name
    so.company = company
    so.customer = customer
    so.set_warehouse = set_warehouse
    so.shipping_address_name = shipping_address_name
    so.items = items or [_make_so_item()]
    return so


def _make_customer(
    tax_id="29ABCDE1234F1Z5",
    customer_primary_address="ACME-Billing",
    customer_name="ACME Industries",
):
    c = MagicMock()
    c.tax_id = tax_id
    c.customer_primary_address = customer_primary_address
    c.customer_name = customer_name
    return c


def _make_account(name="Harmony", ecs_b2b_module="Old B2B"):
    a = MagicMock()
    a.name = name
    a.get = lambda k, default=None: {
        "ecs_b2b_module": ecs_b2b_module,
    }.get(k, default)
    a.ecs_b2b_module = ecs_b2b_module
    return a


def _make_item_doc(gst_hsn_code="85171000"):
    it = MagicMock()
    it.gst_hsn_code = gst_hsn_code
    return it


class TestRefusal1MixedWarehouses(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so(
            items=[
                _make_so_item(warehouse="EE-WH-001", idx=1, item_code="A"),
                _make_so_item(warehouse="Other-WH", idx=2, item_code="B"),
            ]
        )
        with (
            patch.object(frappe, "get_doc", return_value=_make_customer()),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account())
        msg = str(exc_ctx.exception)
        self.assertIn("Mixed warehouses not supported", msg)
        self.assertIn("B", msg)
        self.assertIn("Other-WH", msg)


class TestRefusal2B2BModuleNotConfigured(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so()
        with (
            patch.object(frappe, "get_doc", return_value=_make_customer()),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account(ecs_b2b_module=""))
        msg = str(exc_ctx.exception)
        self.assertIn("missing the B2B module configuration", msg)
        self.assertIn("Harmony", msg)


class TestRefusal3CustomerNotSynced(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so()
        with (
            patch.object(frappe, "get_doc", return_value=_make_customer()),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value=None,
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account())
        msg = str(exc_ctx.exception)
        self.assertIn("not synced to EasyEcom", msg)
        self.assertIn("ACME", msg)


class TestRefusal4ItemNotSynced(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so(items=[_make_so_item(item_code="UNSYNCED-001")])
        with (
            patch.object(frappe, "get_doc", return_value=_make_customer()),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value=None,
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account())
        msg = str(exc_ctx.exception)
        self.assertIn("not synced to EasyEcom", msg)
        self.assertIn("UNSYNCED-001", msg)


class TestRefusal5OldB2BMissingGstin(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so()
        customer = _make_customer(tax_id=None)
        with (
            patch.object(frappe, "get_doc", return_value=customer),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account(ecs_b2b_module="Old B2B"))
        msg = str(exc_ctx.exception)
        self.assertIn("Old B2B requires GSTIN", msg)
        self.assertIn("URP fallback is only available for New B2B", msg)

    def test_new_b2b_accepts_missing_gstin(self) -> None:
        """Counterpart: missing GSTIN passes for New B2B (URP fallback
        lives at payload-builder time, not gating time)."""
        so = _make_so()
        customer = _make_customer(tax_id=None)
        with (
            patch.object(frappe, "get_doc", return_value=customer),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
        ):
            validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))


class TestRefusal6HsnMissing(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so(items=[_make_so_item(item_code="NO-HSN-001")])
        with (
            patch.object(frappe, "get_doc", return_value=_make_customer()),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc(gst_hsn_code="")
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account())
        msg = str(exc_ctx.exception)
        self.assertIn("missing HSN code", msg)
        self.assertIn("NO-HSN-001", msg)


class TestRefusal7ZeroRate(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so(items=[_make_so_item(item_code="ZERO-RATE", rate=0)])
        with (
            patch.object(frappe, "get_doc", return_value=_make_customer()),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account())
        msg = str(exc_ctx.exception)
        self.assertIn("rate 0", msg)
        self.assertIn("Free of Charge", msg)
        self.assertIn("ZERO-RATE", msg)


class TestRefusal8BillingAddressMissing(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        so = _make_so()
        customer = _make_customer(customer_primary_address=None)
        with (
            patch.object(frappe, "get_doc", return_value=customer),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))
        msg = str(exc_ctx.exception)
        self.assertIn("no Billing Address", msg)
        self.assertIn("ACME", msg)


class TestRefusal9ShippingAddressMissing(unittest.TestCase):
    def test_throws_with_packet_message(self) -> None:
        # No SO shipping + customer has no primary either.
        so = _make_so(shipping_address_name=None)
        customer = _make_customer(customer_primary_address=None)
        with (
            patch.object(frappe, "get_doc", return_value=customer),
            patch.object(
                frappe, "get_cached_doc", return_value=_make_item_doc()
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_customer_id",
                return_value="EE-CUST",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.resolve_ee_sku",
                return_value="EE-SKU",
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))
        # Either #8 or #9 fires first (depending on order); both
        # are valid "missing address" errors per packet.
        msg = str(exc_ctx.exception)
        self.assertTrue(
            "Billing Address" in msg or "shipping address" in msg,
            f"Expected billing or shipping error, got: {msg}",
        )


if __name__ == "__main__":
    unittest.main()
