"""gh#241 — empty-items guard in `mirror_si_from_ee_response`.

REGRESSION CONTEXT

Live symptom on MMPL 2026-07-28: GenerateB2BInvoiceJob failed on
SI-2603459 + SI-2603465 with the opaque:

  MandatoryError: [Sales Invoice, SI-XXXXXXX]: items

Reported by @garv999. Client confirmed the failure hits ONLY on
multi-invoice / short-pick orders — i.e. when EE requests a fresh
invoice against an already-(partially-or-fully)-billed SO.

Root cause: `make_sales_invoice(so.name)` returns `qty = so.qty -
billed_qty - returned_qty` per line. When prior SI(s) have consumed
all billable qty, every line comes back with qty=0 (or the item is
dropped) → empty items list. `_apply_ee_qtys_and_drop_zero_lines` then
has nothing to override. `si.insert()` throws MandatoryError.

The gh#227 multi-invoice-per-SO fix explicitly SUPPORTS multiple SIs
per SO but doesn't guard against the case where the SO is already
fully billed — the invoice_id idempotency check misses fresh invoice_ids.

THE FIX

Two guards added to `mirror_si_from_ee_response`:

  1. AFTER `make_sales_invoice` — if items list is empty, the SO is
     fully billed → raise `InvoiceMirrorSOFullyBilled` (subclass of
     InvoiceMirrorError) with the existing SI names attached. Callers
     translate to graceful "already invoiced" responses.

  2. AFTER `_apply_ee_qtys_and_drop_zero_lines` — if items list is
     now empty (SO had items but EE's invoice references different
     SKUs), raise `InvoiceMirrorError` with the specific SKU mismatch
     detail (EE SKUs vs SO SKUs) instead of the opaque MandatoryError.

Neither guard fires if items list is non-empty at the check point.

These lock: (a) fully-billed SO raises the specific exception; (b) SKU
mismatch raises the specific error message; (c) normal happy path is
unaffected; (d) `InvoiceMirrorSOFullyBilled` is a subclass of
InvoiceMirrorError so existing catchers still fire.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror as mod


def _ee_row(**overrides):
    base = {
        "invoice_id": 999888,
        "invoice_number": "TEST-INV-001",
        "invoice_date": "2026-07-29",
        "total_amount": 1000.0,
        "order_items": [
            {"sku": "SKU-A", "item_quantity": 5},
            {"sku": "SKU-B", "item_quantity": 3},
        ],
    }
    base.update(overrides)
    return base


def _map_doc(sales_order="SO-TEST-001", name="ECS-B2B-SO-TEST-001"):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.company = "MMPL"
    m.sales_invoice = None
    m.get = lambda k, default=None: {"sales_invoice": None}.get(k, default)
    return m


def _si_with_items(items=None, name="SI-NEW-001"):
    """Fabricate a MagicMock SI with a controllable items list."""
    si = MagicMock()
    si.name = name
    si.items = list(items) if items is not None else [
        SimpleNamespace(item_code="ITEM-A", qty=5, rate=100, amount=500, idx=1),
        SimpleNamespace(item_code="ITEM-B", qty=3, rate=200, amount=600, idx=2),
    ]
    si.grand_total = 1000.0
    si.total_taxes_and_charges = 0
    si.insert = MagicMock()
    si.flags = SimpleNamespace(ignore_permissions=False)
    return si


# ============================================================
# The subclass-of-error guarantee
# ============================================================


class TestExceptionInheritance(unittest.TestCase):
    """InvoiceMirrorSOFullyBilled MUST subclass InvoiceMirrorError so
    every existing `except InvoiceMirrorError` catch still fires."""

    def test_so_fully_billed_is_invoice_mirror_error(self):
        self.assertTrue(
            issubclass(mod.InvoiceMirrorSOFullyBilled, mod.InvoiceMirrorError)
        )

    def test_so_fully_billed_carries_existing_si_names(self):
        exc = mod.InvoiceMirrorSOFullyBilled(
            "test message", existing_si_names=["SI-A", "SI-B"],
        )
        self.assertEqual(exc.existing_si_names, ["SI-A", "SI-B"])
        self.assertEqual(str(exc), "test message")

    def test_so_fully_billed_default_empty_list(self):
        exc = mod.InvoiceMirrorSOFullyBilled("no names given")
        self.assertEqual(exc.existing_si_names, [])


# ============================================================
# Guard 1: fully-billed SO → InvoiceMirrorSOFullyBilled
# ============================================================


class TestFullyBilledGuard(unittest.TestCase):
    """When `make_sales_invoice` returns an SI with no items (the SO is
    already fully billed), raise InvoiceMirrorSOFullyBilled instead of
    letting `si.insert()` throw the opaque MandatoryError."""

    def _run(self, *, msi_items, existing_sis=None):
        si = _si_with_items(items=msi_items)

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            filters = kwargs.get("filters") or (args[1] if len(args) > 1 else None)
            field = (
                kwargs.get("field")
                or kwargs.get("fieldname")
                or (args[2] if len(args) > 2 else None)
            )
            if doctype == "Sales Invoice":
                if isinstance(filters, dict) and \
                        "ecs_easyecom_invoice_id" in filters:
                    return None  # step-1 idempotency miss
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(
                mod.frappe.db, "sql",
                return_value=[{"name": n} for n in (existing_sis or [])],
            ),
            patch(
                "erpnext.selling.doctype.sales_order.sales_order."
                "make_sales_invoice",
                return_value=si,
            ),
        ):
            with self.assertRaises(mod.InvoiceMirrorSOFullyBilled) as ctx:
                mod.mirror_si_from_ee_response(
                    map_doc=_map_doc(), ee_row=_ee_row(),
                )
        return ctx.exception, si

    def test_empty_items_from_msi_raises_fully_billed(self):
        """The exact live scenario: make_sales_invoice returns empty items."""
        exc, si = self._run(msi_items=[])
        self.assertIn("already fully billed", str(exc))
        self.assertIn("SO-TEST-001", str(exc))
        # SI must NOT be inserted — we raised before that
        si.insert.assert_not_called()

    def test_fully_billed_carries_existing_si_names(self):
        """The exception carries the existing SIs so callers can name
        them in the response."""
        exc, _si = self._run(
            msi_items=[],
            existing_sis=["SI-EXISTING-001", "SI-EXISTING-002"],
        )
        self.assertEqual(
            exc.existing_si_names,
            ["SI-EXISTING-001", "SI-EXISTING-002"],
        )
        self.assertIn("SI-EXISTING-001", str(exc))

    def test_message_names_the_specific_trigger(self):
        """Regression guard on the error message shape — FDE reading the
        alert needs to know WHY (empty items) not just WHAT (mirror
        failed). Locks the words so future refactors keep the signal."""
        exc, _si = self._run(msi_items=[])
        msg = str(exc)
        self.assertIn("no billable lines", msg)
        self.assertIn("invoice_id", msg)
        self.assertIn("999888", msg)  # the EE invoice_id from _ee_row()


# ============================================================
# Guard 2: SKU mismatch → InvoiceMirrorError with clear message
# ============================================================


class TestSkuMismatchGuard(unittest.TestCase):
    """When `make_sales_invoice` returned items BUT
    `_apply_ee_qtys_and_drop_zero_lines` emptied them (no SO SKU
    appears in EE's invoice payload), raise InvoiceMirrorError with a
    specific SKU-mismatch message instead of opaque MandatoryError."""

    def test_all_items_dropped_by_sku_mismatch_raises_clear_error(self):
        """SO has ITEM-A / ITEM-B mapped to SKU-A / SKU-B. EE's invoice
        references SKU-Z (not on the SO). Override drops everything."""
        si = _si_with_items(items=[
            SimpleNamespace(item_code="ITEM-A", qty=5, rate=100, amount=500, idx=1),
        ])

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            filters = kwargs.get("filters") or (args[1] if len(args) > 1 else None)
            field = (
                kwargs.get("field")
                or kwargs.get("fieldname")
                or (args[2] if len(args) > 2 else None)
            )
            if doctype == "Sales Invoice":
                if isinstance(filters, dict) and \
                        "ecs_easyecom_invoice_id" in filters:
                    return None
            if doctype == "EasyEcom Item Map":
                if isinstance(filters, dict):
                    code = filters.get("erpnext_name")
                    # SO item ITEM-A maps to SKU-A
                    if code == "ITEM-A":
                        return "SKU-A"
                    # SO item lookup for helper (mismatch message)
                return None
            return None

        def _sql(*args, **kwargs):
            # SO item lookup for _get_so_item_skus helper
            query = args[0] if args else ""
            if "Sales Order Item" in query:
                return [{"item_code": "ITEM-A"}]
            return []

        # EE invoice references SKU-Z (not SKU-A) → drops everything
        ee_row_mismatch = _ee_row(order_items=[
            {"sku": "SKU-Z", "item_quantity": 5},
        ])

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(mod.frappe.db, "sql", side_effect=_sql),
            patch(
                "erpnext.selling.doctype.sales_order.sales_order."
                "make_sales_invoice",
                return_value=si,
            ),
        ):
            with self.assertRaises(mod.InvoiceMirrorError) as ctx:
                mod.mirror_si_from_ee_response(
                    map_doc=_map_doc(), ee_row=ee_row_mismatch,
                )

        # NOT the SOFullyBilled subclass — this is the mismatch case
        self.assertNotIsInstance(ctx.exception, mod.InvoiceMirrorSOFullyBilled)

        msg = str(ctx.exception)
        self.assertIn("SKU mismatch", msg)
        self.assertIn("SKU-Z", msg)  # EE's SKU named
        self.assertIn("SKU-A", msg)  # SO's SKU named
        # SI must NOT be inserted
        si.insert.assert_not_called()


# ============================================================
# Helper: _find_existing_sis_for_so
# ============================================================


class TestFindExistingSisForSo(unittest.TestCase):
    """Read-only helper that returns SI names linked to an SO."""

    def test_empty_so_name_returns_empty(self):
        self.assertEqual(mod._find_existing_sis_for_so(""), [])

    def test_sql_exception_returns_empty(self):
        """Defensive: DB error → return [] so mirror can still raise
        SOFullyBilled with just the message (no existing_si_names)."""
        with patch.object(mod.frappe.db, "sql", side_effect=RuntimeError("db down")):
            self.assertEqual(mod._find_existing_sis_for_so("SO-X"), [])

    def test_returns_si_names_from_sql(self):
        with patch.object(mod.frappe.db, "sql", return_value=[
            {"name": "SI-A"}, {"name": "SI-B"},
        ]):
            result = mod._find_existing_sis_for_so("SO-X")
        self.assertEqual(result, ["SI-A", "SI-B"])


# ============================================================
# Handler-level: translate SOFullyBilled → GSPHandlerNoOp → HTTP 200
# ============================================================


class TestHandlerTranslatesSOFullyBilledToNoOp(unittest.TestCase):
    """Contract: gsp_handler catches InvoiceMirrorSOFullyBilled and
    re-raises as GSPHandlerNoOp — endpoint returns HTTP 200 with a
    graceful "already invoiced" message + existing SI references, so
    EE stops retrying with fresh invoice_ids."""

    def test_handler_translates_fully_billed_to_noop(self):
        from ecommerce_super.easyecom.flows.b2b_sales import (
            gsp_handler as handler_mod,
        )

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            if doctype == "Sales Invoice":
                return None  # step-1 idempotency miss
            if doctype == "EasyEcom B2B Order Map":
                return "MAP-FULLY-BILLED"
            return None

        map_doc_stub = MagicMock()
        map_doc_stub.name = "MAP-FULLY-BILLED"
        map_doc_stub.sales_order = "SO-BILLED-001"

        mirror_stub = MagicMock(side_effect=mod.InvoiceMirrorSOFullyBilled(
            "Source SO already fully billed",
            existing_si_names=["SI-FIRST-001"],
        ))

        with (
            patch.object(handler_mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(handler_mod.frappe, "get_doc", return_value=map_doc_stub),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                mirror_stub,
            ),
        ):
            with self.assertRaises(handler_mod.GSPHandlerNoOp) as ctx:
                handler_mod.find_or_create_si_for_gsp(
                    ee_row={"invoice_id": "INV-999", "reference_code": "SO-BILLED-001"},
                    ee_account="MMPL Account",
                )
        self.assertIn("SI-FIRST-001", str(ctx.exception))
        self.assertIn("already fully invoiced", str(ctx.exception))

    def test_handler_still_translates_other_mirror_errors_to_gsp_error(self):
        """Regression guard: only SOFullyBilled becomes NoOp. Other
        InvoiceMirrorError subclasses still translate to GSPHandlerError
        (→ HTTP 422). Locks that the new subclass check doesn't over-
        swallow real errors."""
        from ecommerce_super.easyecom.flows.b2b_sales import (
            gsp_handler as handler_mod,
        )

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            if doctype == "Sales Invoice":
                return None
            if doctype == "EasyEcom B2B Order Map":
                return "MAP-X"
            return None

        map_doc_stub = MagicMock()
        map_doc_stub.name = "MAP-X"
        map_doc_stub.sales_order = "SO-X"

        # A GENERIC InvoiceMirrorError (not the SOFullyBilled subclass)
        mirror_stub = MagicMock(side_effect=mod.InvoiceMirrorError(
            "Some other prerequisite failed"
        ))

        with (
            patch.object(handler_mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(handler_mod.frappe, "get_doc", return_value=map_doc_stub),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                mirror_stub,
            ),
        ):
            with self.assertRaises(handler_mod.GSPHandlerError) as ctx:
                handler_mod.find_or_create_si_for_gsp(
                    ee_row={"invoice_id": "INV-X", "reference_code": "SO-X"},
                    ee_account="MMPL Account",
                )
        # Was translated to GSPHandlerError (HTTP 422), NOT NoOp (HTTP 200)
        self.assertNotIsInstance(ctx.exception, handler_mod.GSPHandlerNoOp)


if __name__ == "__main__":
    unittest.main()
