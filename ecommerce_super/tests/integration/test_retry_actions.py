"""Integration tests for §6.5.1 Retry Now (Sync Record) and the
existing Queue Job retry_job / cancel_job server methods used by the JS
buttons added in this packet."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_sync_record.easyecom_sync_record import (
    retry_now as sync_record_retry_now,
)
from ecommerce_super.easyecom.queue import cancel_job, enqueue_easyecom_job, retry_job
from ecommerce_super.easyecom.utils import idempotency
from ecommerce_super.tests.factories import cleanup_easyecom_state, make_location


def _cleanup_sr() -> None:
    for n in frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"correlation_id": ("like", "test-retry-%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Sync Record", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _cleanup_qj(marker: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Queue Job",
        filters={"idempotency_key": ("like", f"%{marker}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Queue Job", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestSyncRecordRetryNow(FrappeTestCase):
    """§6.5.1: status Failed → Pending, attempts preserved, idempotency
    key preserved, last_error cleared.

    Uses EasyEcom Location as the entity to avoid Item/HSN/UOM validation
    that depends on india_compliance fixtures — Sync Record's entity_type
    Select includes "Warehouse" which maps naturally to a Location.
    """

    LOC_KEY = "TEST-SR-RETRY-LOC"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cleanup_easyecom_state()
        cls.entity_name = make_location(location_key=cls.LOC_KEY)

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_easyecom_state()
        super().tearDownClass()

    def setUp(self) -> None:
        _cleanup_sr()

    def tearDown(self) -> None:
        _cleanup_sr()

    def _make_failed_sr(self, *, attempts: int, key: str) -> str:
        doc = frappe.new_doc("EasyEcom Sync Record")
        doc.update(
            {
                "company": "_Test Company",
                "entity_doctype": "EasyEcom Location",
                "entity_name": self.entity_name,
                "entity_type": "Warehouse",
                "direction": "Pull",
                "status": "Pending",
                "correlation_id": "test-retry-corr-1",
                "idempotency_key": key,
                "attempts": attempts,
            }
        )
        doc.insert(ignore_permissions=True)
        # Walk Pending → Running → Failed (legal per ALLOWED_TRANSITIONS).
        doc.db_set("status", "Running", commit=True)
        doc.reload()
        doc.db_set(
            {"status": "Failed", "last_error": "synthetic test failure"},
            commit=True,
        )
        return doc.name

    def test_failed_to_pending_preserves_attempts_and_key(self) -> None:
        key = idempotency.item_push_key(
            company="_Test Company",
            item_code="SKU-A",
            ee_location_key=self.entity_name,
            change_hash="hash-a",
        )
        sr_name = self._make_failed_sr(attempts=3, key=key)

        result = sync_record_retry_now(sr_name)
        self.assertEqual(result["status"], "Pending")
        self.assertEqual(result["attempts_preserved"], 3)
        self.assertEqual(result["idempotency_key_preserved"], key)

        sr = frappe.get_doc("EasyEcom Sync Record", sr_name)
        self.assertEqual(sr.status, "Pending")
        self.assertEqual(sr.attempts, 3)
        self.assertEqual(sr.idempotency_key, key)
        self.assertIsNone(sr.last_error)

    def test_pending_sync_record_cannot_retry(self) -> None:
        """Retry Now only legal from Failed / Cancelled."""
        key = idempotency.item_push_key(
            company="_Test Company",
            item_code="SKU-B",
            ee_location_key=self.entity_name,
            change_hash="hash-b",
        )
        doc = frappe.new_doc("EasyEcom Sync Record")
        doc.update(
            {
                "company": "_Test Company",
                "entity_doctype": "EasyEcom Location",
                "entity_name": self.entity_name,
                "entity_type": "Warehouse",
                "direction": "Pull",
                "status": "Pending",
                "correlation_id": "test-retry-corr-2",
                "idempotency_key": key,
                "attempts": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        with self.assertRaises(frappe.ValidationError):
            sync_record_retry_now(doc.name)


class TestQueueJobRetryAndCancel(FrappeTestCase):
    """Verify the existing server methods used by the JS buttons."""

    MARKER = "test-qj-retry-cancel"

    def setUp(self) -> None:
        _cleanup_qj(self.MARKER)

    def tearDown(self) -> None:
        _cleanup_qj(self.MARKER)

    def _enqueue(self) -> str:
        key = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="_Test Company",
            payload={"_marker": self.MARKER},
        )
        return enqueue_easyecom_job(
            job_type="Webhook Process",
            company="_Test Company",
            idempotency_key=key,
        )

    def test_cancel_moves_to_cancelled(self) -> None:
        job_id = self._enqueue()
        cancel_job(job_id, "Test cancel")
        qj = frappe.get_doc("EasyEcom Queue Job", job_id)
        self.assertEqual(qj.state, "Cancelled")
        self.assertIn("Cancelled: Test cancel", qj.last_error)

    def test_retry_from_failed_returns_to_queued_with_preserved_corr(self) -> None:
        job_id = self._enqueue()
        qj = frappe.get_doc("EasyEcom Queue Job", job_id)
        original_corr = qj.correlation_id
        # Drive it to Failed via legal transitions: Queued → Running → Failed.
        qj.transition_to_running()
        qj.reload()
        qj.transition_to_failed(error="synthetic", translation_key="TEST")
        retry_job(job_id)
        qj.reload()
        self.assertEqual(qj.state, "Queued")
        # The §6.5.1 contract: correlation_id is preserved on retry.
        self.assertEqual(qj.correlation_id, original_corr)
        self.assertEqual(qj.attempts, 0)
        self.assertIsNone(qj.last_error)

    def test_retry_rejected_from_success_state(self) -> None:
        job_id = self._enqueue()
        qj = frappe.get_doc("EasyEcom Queue Job", job_id)
        qj.transition_to_running()
        qj.reload()
        qj.transition_to_success()
        with self.assertRaises(frappe.ValidationError):
            retry_job(job_id)
