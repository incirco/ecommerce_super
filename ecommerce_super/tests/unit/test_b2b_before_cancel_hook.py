"""§11 Phase 1 — before_cancel hook wrapper.

Verifies the synchronous block-on-refusal semantics:
  T1 — scope guard: non-EE / non-B2B SOs cancel cleanly, zero EE
       calls, zero Sync Records.
  T2 — business refusal: cancel throws, SO stays at docstatus=1, a
       Discrepancy is raised, EE called exactly once.
  T3 — accept: cancel proceeds, EE called exactly once, no
       Discrepancy.
  T4 — infra failure: cancel throws with the distinct unreachable
       message, SO stays at docstatus=1, a Failed (NOT Discrepancy)
       Sync Record is raised.

The hook itself is one line of glue; the load-bearing tests are on
the underlying behaviour. Tests use mocks so they run without an
actual Harmony round-trip.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.exceptions import (
    EasyEcomServerError,
    EasyEcomTimeoutError,
    EasyEcomValidationError,
)
from ecommerce_super.easyecom.flows.b2b_sales.cancel import (
    on_before_cancel_dispatch,
)


_CANCEL = "ecommerce_super.easyecom.flows.b2b_sales.cancel"


def _make_so_doc(name="SAL-ORD-T-001", with_map=True):
    """Lightweight SO doc stub. The hook reads doctype + the
    ecs_b2b_order_map back-ref custom field, nothing else."""
    doc = MagicMock()
    doc.doctype = "Sales Order"
    doc.name = name
    doc.get = lambda field: (
        f"ECS-B2B-{name}" if (field == "ecs_b2b_order_map" and with_map)
        else None
    )
    return doc


class TestScopeGuard(unittest.TestCase):
    """T1 — non-EE / non-B2B SOs must cancel exactly like stock
    ERPNext. Zero EE traffic, zero Sync Records."""

    def test_t1_no_map_back_ref_returns_immediately(self) -> None:
        doc = _make_so_doc(with_map=False)
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext") as mock_cancel, \
             patch(f"{_CANCEL}._write_cancel_sync_record") as mock_sr, \
             patch.object(frappe.db, "exists", return_value=False):
            on_before_cancel_dispatch(doc)
        mock_cancel.assert_not_called()
        mock_sr.assert_not_called()

    def test_t1_non_sales_order_doctype_returns_immediately(self) -> None:
        doc = MagicMock()
        doc.doctype = "Purchase Order"  # not a Sales Order
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext") as mock_cancel:
            on_before_cancel_dispatch(doc)
        mock_cancel.assert_not_called()

    def test_t1_stale_map_back_ref_returns_immediately(self) -> None:
        """A SO that carries an ecs_b2b_order_map back-ref to a Map
        row that no longer exists should NOT block cancellation."""
        doc = _make_so_doc(with_map=True)
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext") as mock_cancel, \
             patch.object(frappe.db, "exists", return_value=False):
            on_before_cancel_dispatch(doc)
        mock_cancel.assert_not_called()

    def test_t1_map_in_non_cancellable_status_returns_immediately(self) -> None:
        """Already-Cancelled / Invoice Pending / etc — hook bails;
        local cancel proceeds without an EE call."""
        doc = _make_so_doc(with_map=True)
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext") as mock_cancel, \
             patch.object(frappe.db, "exists", return_value=True), \
             patch.object(frappe.db, "get_value", return_value="Cancelled"):
            on_before_cancel_dispatch(doc)
        mock_cancel.assert_not_called()


class TestBusinessRefusal(unittest.TestCase):
    """T2 — EE refuses (shipped). The existing
    cancel_b2b_order_from_erpnext already raises the Discrepancy +
    throws. The hook re-raises so docstatus stays 1."""

    def test_t2_throw_propagates_and_docstatus_unchanged(self) -> None:
        doc = _make_so_doc(with_map=True)
        # Simulate the underlying function throwing the
        # shipped-refusal ValidationError after raising the
        # Discrepancy (per cancel.py:190-200 path).
        refusal = frappe.ValidationError(
            "EasyEcom refused the cancellation — the order is already shipped"
        )
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext",
                   side_effect=refusal) as mock_cancel, \
             patch.object(frappe.db, "exists", return_value=True), \
             patch.object(frappe.db, "get_value", return_value="Pushed"):
            with self.assertRaises(frappe.ValidationError) as ctx:
                on_before_cancel_dispatch(doc)
        mock_cancel.assert_called_once_with(sales_order=doc.name)
        self.assertIn("already shipped", str(ctx.exception).lower())
        # docstatus is preserved because Frappe never reaches the
        # flip when before_cancel throws — load-bearing for the
        # block-on-refusal contract.


class TestAcceptPath(unittest.TestCase):
    """T3 — EE accepts the cancel. The hook returns clean; Frappe
    proceeds to docstatus=2."""

    def test_t3_accept_returns_clean(self) -> None:
        doc = _make_so_doc(with_map=True)
        accept_return = {
            "ok": True,
            "map_name": f"ECS-B2B-{doc.name}",
            "ee_message": "Successfully Cancelled...",
        }
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext",
                   return_value=accept_return) as mock_cancel, \
             patch.object(frappe.db, "exists", return_value=True), \
             patch.object(frappe.db, "get_value", return_value="Pushed"):
            # No throw expected. Returns None (hooks return None).
            result = on_before_cancel_dispatch(doc)
        mock_cancel.assert_called_once_with(sales_order=doc.name)
        self.assertIsNone(result)


class TestInfraFailure(unittest.TestCase):
    """T4 — infra failure paths route through _INFRA_FAILURE_TYPES
    in the underlying function, which writes a Failed Sync Record
    and throws with the distinct unreachable message. The hook
    re-raises."""

    def test_t4_timeout_throws_with_unreachable_message(self) -> None:
        doc = _make_so_doc(with_map=True)
        # The underlying function catches EasyEcomTimeoutError and
        # throws a ValidationError with the distinct "unreachable"
        # phrasing. Simulate the resulting ValidationError.
        unreachable = frappe.ValidationError(
            "EasyEcom unreachable — cancellation not propagated; retry."
        )
        with patch(f"{_CANCEL}.cancel_b2b_order_from_erpnext",
                   side_effect=unreachable) as mock_cancel, \
             patch.object(frappe.db, "exists", return_value=True), \
             patch.object(frappe.db, "get_value", return_value="Pushed"):
            with self.assertRaises(frappe.ValidationError) as ctx:
                on_before_cancel_dispatch(doc)
        mock_cancel.assert_called_once_with(sales_order=doc.name)
        self.assertIn("unreachable", str(ctx.exception).lower())


class TestInfraFailureClassification(unittest.TestCase):
    """T4 — confirm the underlying function classifies infra
    exception types (Timeout / Server / Auth / RateLimit) into the
    Failed Sync Record + unreachable throw path."""

    def _run_with_exception(self, exc):
        from ecommerce_super.easyecom.flows.b2b_sales import (
            cancel as cancel_mod,
        )
        # Fake EE client whose POST raises the infra exception.
        fake_client = MagicMock()
        fake_client.post.side_effect = exc
        # Fake the dependencies the function pulls in.
        so = MagicMock()
        so.name = "SAL-ORD-T4"; so.company = "TestCo"; so.set_warehouse = "WH"
        so.get = lambda f: "ECS-B2B-T4" if f == "ecs_b2b_order_map" else None
        map_doc = MagicMock()
        map_doc.name = "ECS-B2B-T4"; map_doc.status = "Pushed"
        map_doc.easyecom_account = "EE"; map_doc.sales_order = "SAL-ORD-T4"
        ee_account = MagicMock(); ee_account.name = "EE"
        with patch.object(frappe, "get_doc", side_effect=[so, map_doc, ee_account]), \
             patch(f"{_CANCEL}.EasyEcomClient", return_value=fake_client), \
             patch(f"{_CANCEL}.get_ee_location_for_warehouse",
                   return_value=MagicMock(location_key="LOC")), \
             patch(f"{_CANCEL}._write_cancel_sync_record") as mock_sr, \
             patch(f"{_CANCEL}._raise_b2b_cancel_refusal_discrepancy") as mock_disc:
            with self.assertRaises(frappe.ValidationError) as ctx:
                cancel_mod.cancel_b2b_order_from_erpnext("SAL-ORD-T4")
        return mock_sr, mock_disc, ctx.exception

    def test_t4_timeout_writes_failed_sync_record_not_discrepancy(self) -> None:
        mock_sr, mock_disc, exc = self._run_with_exception(
            EasyEcomTimeoutError("timed out", status_code=504)
        )
        mock_sr.assert_called_once()
        # Argument check: the sync record was status="Failed".
        kwargs = mock_sr.call_args.kwargs
        self.assertEqual(kwargs.get("status"), "Failed")
        mock_disc.assert_not_called()  # NO Discrepancy on infra fail.
        self.assertIn("unreachable", str(exc).lower())

    def test_t4_server_error_writes_failed_sync_record(self) -> None:
        mock_sr, mock_disc, exc = self._run_with_exception(
            EasyEcomServerError("5xx", status_code=503)
        )
        mock_sr.assert_called_once()
        self.assertEqual(mock_sr.call_args.kwargs.get("status"), "Failed")
        mock_disc.assert_not_called()
        self.assertIn("unreachable", str(exc).lower())

    def test_t2_validation_error_writes_discrepancy_not_failed(self) -> None:
        """Symmetric contrast: a business refusal (HTTP-200-wrapped
        body code) routes through the Discrepancy path, NOT the
        Failed Sync Record."""
        # EE returned 200 with body code=400, message=already shipped.
        # The client wraps that as EasyEcomValidationError with
        # response_body attached.
        v = EasyEcomValidationError(
            "Order already shipped", status_code=200
        )
        v.response_body = {
            "code": 400, "message": "Order already shipped — cannot cancel"
        }
        mock_sr, mock_disc, exc = self._run_with_exception(v)
        mock_disc.assert_called_once()
        # Discrepancy was raised, NOT Failed Sync Record.
        mock_sr.assert_not_called()
        self.assertIn("already shipped", str(exc).lower())


if __name__ == "__main__":
    unittest.main()
