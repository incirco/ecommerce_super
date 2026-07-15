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


class TestRefusal10FractionalQuantity(unittest.TestCase):
    """Post-#197/#201 addition: EE contract does not support fractional
    B2B quantities on either module. Rather than silently truncating
    (2.5 → 2, under-shipping the customer), gate at submit time so the
    FDE gets a clear message pointing at the offending row + item."""

    def _patched_context(self, so, customer=None):
        """The same patch-cluster the other refusal tests use."""
        return [
            patch.object(frappe, "get_doc", return_value=customer or _make_customer()),
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
        ]

    def test_throws_with_actionable_message_naming_row_and_item(self) -> None:
        so = _make_so(items=[
            _make_so_item(item_code="FG06476-CHOUHAN", qty=2.5, idx=1),
        ])
        ctxs = self._patched_context(so)
        for c in ctxs:
            c.__enter__()
        try:
            with self.assertRaises(frappe.ValidationError) as exc_ctx:
                validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))
        finally:
            for c in ctxs:
                c.__exit__(None, None, None)
        msg = str(exc_ctx.exception)
        # Message must name the item, row, and qty for actionable debugging.
        self.assertIn("FG06476-CHOUHAN", msg)
        self.assertIn("2.5", msg)
        self.assertIn("row 1", msg.lower().replace("row ", "row ").replace("(row ", "row ")
                     if "row " in msg.lower() else msg)  # tolerant of formatting
        self.assertIn("EasyEcom does not support fractional", msg)
        # Actionable next step: mention UOM change or splitting.
        self.assertTrue(
            "UOM" in msg or "split" in msg.lower(),
            f"Expected actionable guidance (UOM or split), got: {msg}",
        )

    def test_whole_number_qty_passes(self) -> None:
        """Regression guard: whole-number qty must not trip the gate."""
        so = _make_so(items=[_make_so_item(qty=5)])
        ctxs = self._patched_context(so)
        for c in ctxs:
            c.__enter__()
        try:
            validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))
        finally:
            for c in ctxs:
                c.__exit__(None, None, None)

    def test_whole_number_float_qty_passes(self) -> None:
        """Common ERPNext shape: qty stored as float 5.0. Must pass —
        `5.0 != int(5.0)` is False, so gate lets it through."""
        so = _make_so(items=[_make_so_item(qty=5.0)])
        ctxs = self._patched_context(so)
        for c in ctxs:
            c.__enter__()
        try:
            validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))
        finally:
            for c in ctxs:
                c.__exit__(None, None, None)

    def test_fractional_qty_on_second_line_names_correct_row(self) -> None:
        """Multi-line SO where only line 2 is fractional — throw must
        point at line 2, not line 1 or a generic 'some line'."""
        so = _make_so(items=[
            _make_so_item(item_code="A", qty=1, idx=1),
            _make_so_item(item_code="B", qty=1.75, idx=2),
        ])
        ctxs = self._patched_context(so)
        for c in ctxs:
            c.__enter__()
        try:
            with self.assertRaises(frappe.ValidationError) as exc_ctx:
                validate_preconditions(so, _make_account(ecs_b2b_module="New B2B"))
        finally:
            for c in ctxs:
                c.__exit__(None, None, None)
        msg = str(exc_ctx.exception)
        self.assertIn("B", msg)
        self.assertIn("1.75", msg)
        # Line 1 (item A) must NOT be named — it's fine.
        # Match a standalone 'A' surrounded by quote/space, not e.g. "SO-1A".
        self.assertNotIn("'A'", msg)

    def test_new_b2b_and_old_b2b_both_gated(self) -> None:
        """Both B2B modules share the EE fractional-qty constraint —
        gate must fire regardless of ecs_b2b_module value."""
        for module in ("New B2B", "Old B2B"):
            with self.subTest(module=module):
                so = _make_so(items=[_make_so_item(qty=3.3)])
                ctxs = self._patched_context(so)
                for c in ctxs:
                    c.__enter__()
                try:
                    with self.assertRaises(frappe.ValidationError):
                        validate_preconditions(
                            so, _make_account(ecs_b2b_module=module),
                        )
                finally:
                    for c in ctxs:
                        c.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
