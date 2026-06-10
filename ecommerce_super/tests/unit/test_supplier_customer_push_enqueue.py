"""gh#27 — Supplier and Customer Push enqueue must call the facade correctly.

The batch-sweep `enqueue_push_all_pending` in `supplier_push.py` and
`customer_push.py` was calling `enqueue_easyecom_job` with stale kwargs
(`method=` / `kwargs=`) and without the required `company` and
`idempotency_key`. Every call raised TypeError or ValueError, was
caught by the broad `except`, and silently logged to Error Log —
surfacing only as "Considered: N, Enqueued: 0" with no FDE-visible
diagnostic.

Also: neither "Customer Push" nor "Supplier Push" was registered in
`JOB_TYPE_HANDLERS`, so even a successful enqueue would have failed
at `execute_job` time.

These tests freeze the correct shape of both call paths.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestSupplierPushEnqueueFacadeCall(unittest.TestCase):
    def _patch_company(self):
        return patch(
            "ecommerce_super.easyecom.flows.supplier_push._company_for_supplier_push",
            return_value="_Test Company",
        )

    def test_enqueue_push_all_pending_passes_required_facade_kwargs(self) -> None:
        from ecommerce_super.easyecom.flows import supplier_push

        with (
            self._patch_company(),
            patch.object(
                supplier_push,
                "candidate_suppliers_for_sweep",
                return_value=["SUP-001"],
            ),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value="ECS-QJ-1",
            ) as enqueue_mock,
        ):
            result = supplier_push.enqueue_push_all_pending(account_name="ACC1")

        self.assertEqual(result["total_considered"], 1)
        self.assertEqual(result["enqueued_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        # Inspect the call shape — every required facade kwarg is present.
        enqueue_mock.assert_called_once()
        call = enqueue_mock.call_args
        self.assertEqual(call.kwargs["job_type"], "Supplier Push")
        self.assertEqual(call.kwargs["company"], "_Test Company")
        self.assertEqual(call.kwargs["target_doctype"], "Supplier")
        self.assertEqual(call.kwargs["target_name"], "SUP-001")
        self.assertIn("idempotency_key", call.kwargs)
        self.assertTrue(call.kwargs["idempotency_key"])  # non-empty
        self.assertEqual(
            call.kwargs["payload"],
            {"supplier_docname": "SUP-001", "account_name": "ACC1"},
        )
        # Stale kwargs from the prior (broken) version must NOT be passed.
        self.assertNotIn("method", call.kwargs)
        self.assertNotIn("kwargs", call.kwargs)

    def test_enqueue_failure_surfaces_diagnostic_to_caller(self) -> None:
        """gh#27 contract: per-candidate failures must appear in
        failures_sample so the FDE doesn't see a silent black box."""
        from ecommerce_super.easyecom.flows import supplier_push

        with (
            self._patch_company(),
            patch.object(
                supplier_push,
                "candidate_suppliers_for_sweep",
                return_value=["SUP-001", "SUP-002"],
            ),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                side_effect=ValueError("boom"),
            ),
            patch.object(frappe, "log_error"),
        ):
            result = supplier_push.enqueue_push_all_pending(account_name="ACC1")

        self.assertEqual(result["total_considered"], 2)
        self.assertEqual(result["enqueued_count"], 0)
        self.assertEqual(result["failed_count"], 2)
        self.assertEqual(len(result["failures_sample"]), 2)
        # Each failure entry names the candidate AND the error.
        self.assertIn("ValueError: boom", result["failures_sample"][0]["error"])
        self.assertEqual(
            result["failures_sample"][0]["supplier_docname"], "SUP-001"
        )

    def test_idempotency_key_is_stable_for_same_input(self) -> None:
        from ecommerce_super.easyecom.flows.supplier_push import (
            _supplier_push_queue_idempotency_key,
        )

        k1 = _supplier_push_queue_idempotency_key(
            supplier_docname="SUP-001", account_name="ACC1", company="Co A"
        )
        k2 = _supplier_push_queue_idempotency_key(
            supplier_docname="SUP-001", account_name="ACC1", company="Co A"
        )
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)  # sha256 hex

    def test_idempotency_key_differs_when_input_differs(self) -> None:
        from ecommerce_super.easyecom.flows.supplier_push import (
            _supplier_push_queue_idempotency_key,
        )

        base = _supplier_push_queue_idempotency_key(
            supplier_docname="SUP-001", account_name="ACC1", company="Co A"
        )
        self.assertNotEqual(
            base,
            _supplier_push_queue_idempotency_key(
                supplier_docname="SUP-002", account_name="ACC1", company="Co A"
            ),
        )
        self.assertNotEqual(
            base,
            _supplier_push_queue_idempotency_key(
                supplier_docname="SUP-001", account_name="ACC2", company="Co A"
            ),
        )


class TestCustomerPushEnqueueFacadeCall(unittest.TestCase):
    def test_enqueue_push_all_pending_passes_required_facade_kwargs(self) -> None:
        from ecommerce_super.easyecom.flows import customer_push

        with (
            patch(
                "ecommerce_super.easyecom.flows.customer_push._company_for_customer_push",
                return_value="_Test Company",
            ),
            patch.object(
                customer_push,
                "candidate_customers_for_sweep",
                return_value=["CUST-001"],
            ),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value="ECS-QJ-1",
            ) as enqueue_mock,
        ):
            result = customer_push.enqueue_push_all_pending(account_name="ACC1")

        self.assertEqual(result["enqueued_count"], 1)
        call = enqueue_mock.call_args
        self.assertEqual(call.kwargs["job_type"], "Customer Push")
        self.assertEqual(call.kwargs["company"], "_Test Company")
        self.assertIn("idempotency_key", call.kwargs)
        self.assertNotIn("method", call.kwargs)
        self.assertNotIn("kwargs", call.kwargs)


class TestJobTypeHandlersRegistered(unittest.TestCase):
    """gh#27: the worker dispatch must know about Customer Push and
    Supplier Push, otherwise even a successful enqueue dies at
    execute_job time."""

    def test_customer_push_handler_registered(self) -> None:
        from ecommerce_super.easyecom.queue.workers import JOB_TYPE_HANDLERS

        self.assertIn("Customer Push", JOB_TYPE_HANDLERS)
        self.assertEqual(
            JOB_TYPE_HANDLERS["Customer Push"],
            "ecommerce_super.easyecom.flows.customer_push.customer_push_queue_handler",
        )

    def test_supplier_push_handler_registered(self) -> None:
        from ecommerce_super.easyecom.queue.workers import JOB_TYPE_HANDLERS

        self.assertIn("Supplier Push", JOB_TYPE_HANDLERS)
        self.assertEqual(
            JOB_TYPE_HANDLERS["Supplier Push"],
            "ecommerce_super.easyecom.flows.supplier_push.supplier_push_queue_handler",
        )


if __name__ == "__main__":
    unittest.main()
