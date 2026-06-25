"""gh#86 — `retry_now` must actually re-fire the push for entity
doctypes that don't have a polling tick.

Pre-fix: `retry_now` only flipped status `Failed → Pending` and cleared
`last_error`. The docstring promised "flow handlers detect Pending
Sync Records on their next polling tick or on doc-event fire", but §10
Delivery Note has neither — push fires on `DN.on_submit`, and an
already-submitted DN can't re-fire that hook. So Sync Records were
stranded in Pending with no Queue Job + no API Call.

Post-fix: a `_REFIRE_HANDLERS` registry maps `entity_doctype` →
re-fire callable. Delivery Note gets the registered handler that
enqueues a "Transfer Push" job. Doctypes without a registered handler
fall back to the original flag-flip-only behaviour (backwards-compat
with flows that own their own re-fire path).

These tests cover the dispatcher logic only — the actual enqueue
shape for Delivery Note is exercised in the integration test suite
when it ships.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.doctype.easyecom_sync_record import (
    easyecom_sync_record as sr_mod,
)


def _fake_failed_sr(
    *,
    name="ECS-SR-2026-TEST-001",
    entity_doctype="Delivery Note",
    entity_name="DL-261251",
    company="Modern Marwar Private Limited",
):
    doc = MagicMock()
    doc.name = name
    doc.status = "Failed"
    doc.entity_doctype = entity_doctype
    doc.entity_name = entity_name
    doc.company = company
    doc.attempts = 1
    doc.idempotency_key = "test-key"
    return doc


class TestRetryNowDispatchesRefire(unittest.TestCase):
    """The Failed → Pending flip still happens, AND the registered
    handler is invoked for Delivery Note entries."""

    def test_delivery_note_refire_enqueues_transfer_push_job(self) -> None:
        """gh#86 headline — the bug scenario. A Failed §10 Sync Record
        on a Delivery Note must end up with a queued Transfer Push job
        after Retry, not just a flag flip."""
        sr = _fake_failed_sr()
        fake_qj = MagicMock()
        fake_qj.name = "ECS-QJ-TEST-001"

        with (
            patch("frappe.get_doc", return_value=sr),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value=fake_qj,
            ) as enqueue_mock,
            patch(
                "ecommerce_super.easyecom.utils.idempotency.internal_job_key",
                return_value="job-key-xyz",
            ),
        ):
            result = sr_mod.retry_now(sr.name)

        # Status flip still happens.
        sr.db_set.assert_called_once()
        # The refire actually fired.
        self.assertTrue(result["refire"]["enqueued"])
        self.assertEqual(result["refire"]["job_type"], "Transfer Push")
        self.assertEqual(result["refire"]["queue_job_name"], "ECS-QJ-TEST-001")
        # And the enqueue call uses the right shape for §10.
        enqueue_mock.assert_called_once()
        kw = enqueue_mock.call_args.kwargs
        self.assertEqual(kw["job_type"], "Transfer Push")
        self.assertEqual(kw["target_doctype"], "Delivery Note")
        self.assertEqual(kw["target_name"], "DL-261251")
        self.assertEqual(kw["company"], "Modern Marwar Private Limited")

    def test_unregistered_doctype_returns_no_handler_reason(self) -> None:
        """A doctype without a registered re-fire handler still gets
        the status flip (backwards-compat path — the flow's own sweep
        picks up the Pending record). The response makes it explicit
        that no re-fire was dispatched. Prevents the FDE from thinking
        they queued something they didn't."""
        sr = _fake_failed_sr(
            entity_doctype="EasyEcom Location",
            entity_name="ECS-LOC-TEST",
        )

        with patch("frappe.get_doc", return_value=sr):
            result = sr_mod.retry_now(sr.name)

        sr.db_set.assert_called_once()
        self.assertEqual(result["status"], "Pending")
        self.assertFalse(result["refire"]["enqueued"])
        self.assertIn("No retry-refire handler", result["refire"]["reason"])
        self.assertIn("EasyEcom Location", result["refire"]["reason"])

    def test_refire_handler_exception_leaves_status_failed(self) -> None:
        """gh#86-reopen regression guard: if the refire handler raises
        (e.g. the Queue Job DocType rejects the job_type, or
        enqueue_easyecom_job hits a DB error), the Sync Record must
        STAY Failed with last_error preserved — flipping to Pending
        leaves the record stranded (the exact symptom the original
        gh#86 fix was meant to address). The pre-gh#86-reopen code
        flipped status FIRST then enqueued; this caused the same
        Pending-but-no-Queue-Job stranding the reporter hit on
        live16version.frappe.cloud."""
        sr = _fake_failed_sr()

        with (
            patch("frappe.get_doc", return_value=sr),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                side_effect=RuntimeError("simulated DB hiccup"),
            ),
            patch(
                "ecommerce_super.easyecom.utils.idempotency.internal_job_key",
                return_value="job-key-xyz",
            ),
            patch("frappe.log_error") as log_error,
        ):
            result = sr_mod.retry_now(sr.name)

        # Status flip MUST NOT happen — record stays Failed.
        sr.db_set.assert_not_called()
        self.assertEqual(result["status"], "Failed")
        self.assertFalse(result["refire"]["enqueued"])
        self.assertIn("RuntimeError", result["refire"]["reason"])
        log_error.assert_called_once()

    def test_refire_returning_enqueued_false_leaves_status_failed(self) -> None:
        """gh#86-reopen — same guard as above but for the
        no-enabled-Account skip path. A handler that runs cleanly but
        returns `enqueued: False` (e.g. Item/Customer/Supplier refire
        with no enabled EE Account) MUST also leave status Failed —
        we can't satisfy §6.5.1's 'next attempt enqueued' rule, so we
        shouldn't pretend by flipping status."""
        sr = _fake_failed_sr(entity_doctype="Item", entity_name="SKU-A")

        with (
            patch("frappe.get_doc", return_value=sr),
            # _first_enabled_ee_account returns None → handler skips
            patch("frappe.db.get_value", return_value=None),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
            ) as enq,
        ):
            result = sr_mod.retry_now(sr.name)

        sr.db_set.assert_not_called()
        self.assertEqual(result["status"], "Failed")
        self.assertFalse(result["refire"]["enqueued"])
        enq.assert_not_called()

    def test_pending_status_still_rejected(self) -> None:
        """Pre-existing guard: only Failed/Cancelled can retry. A
        Pending record must still throw — refire dispatch must not
        run for those."""
        import frappe

        sr = _fake_failed_sr()
        sr.status = "Pending"

        with patch("frappe.get_doc", return_value=sr):
            with self.assertRaises(frappe.ValidationError):
                sr_mod.retry_now(sr.name)

        sr.db_set.assert_not_called()


class TestRefireHandlersForOtherEntities(unittest.TestCase):
    """gh#90 — re-fire handlers registered for every entity_doctype
    that uses Sync Records. Without these, the Retry button just
    flipped status and waited for the next sweep tick to actually
    re-fire — a §6.5.1 violation in spirit (handlers picked it up
    eventually, but "next attempt enqueued" wasn't true at the
    moment of the click)."""

    def _fake(self, *, entity_doctype, entity_name, ee_location_key=""):
        doc = MagicMock()
        doc.name = f"ECS-SR-TEST-{entity_doctype.replace(' ', '_')}"
        doc.status = "Failed"
        doc.entity_doctype = entity_doctype
        doc.entity_name = entity_name
        doc.company = "_Test Company"
        doc.attempts = 1
        doc.idempotency_key = "test-key"
        # Mock the .get() call used by the PO/SO refire handlers.
        doc.get = lambda field, default=None: (
            ee_location_key if field == "ee_location_key" else default
        )
        return doc

    def test_item_refire_enqueues_item_push_job(self) -> None:
        sr = self._fake(entity_doctype="Item", entity_name="SKU-A")
        fake_qj = MagicMock()
        fake_qj.name = "ECS-QJ-ITEM-001"
        with (
            patch("frappe.get_doc", return_value=sr),
            patch("frappe.db.get_value", return_value="EE-ACCT-MAIN"),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value=fake_qj,
            ) as enq,
        ):
            result = sr_mod.retry_now(sr.name)

        self.assertTrue(result["refire"]["enqueued"])
        self.assertEqual(result["refire"]["job_type"], "Item Push")
        kw = enq.call_args.kwargs
        self.assertEqual(kw["job_type"], "Item Push")
        self.assertEqual(kw["target_doctype"], "Item")
        self.assertEqual(kw["target_name"], "SKU-A")
        self.assertEqual(kw["payload"]["item_code"], "SKU-A")
        self.assertEqual(kw["payload"]["account_name"], "EE-ACCT-MAIN")

    def test_customer_refire_enqueues_customer_push_job(self) -> None:
        sr = self._fake(entity_doctype="Customer", entity_name="CUST-001")
        fake_qj = MagicMock()
        fake_qj.name = "ECS-QJ-CUST-001"
        with (
            patch("frappe.get_doc", return_value=sr),
            patch("frappe.db.get_value", return_value="EE-ACCT-MAIN"),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value=fake_qj,
            ) as enq,
        ):
            result = sr_mod.retry_now(sr.name)

        self.assertTrue(result["refire"]["enqueued"])
        self.assertEqual(result["refire"]["job_type"], "Customer Push")
        kw = enq.call_args.kwargs
        self.assertEqual(kw["job_type"], "Customer Push")
        self.assertEqual(kw["target_doctype"], "Customer")
        self.assertEqual(kw["payload"]["customer_docname"], "CUST-001")

    def test_supplier_refire_enqueues_supplier_push_job(self) -> None:
        sr = self._fake(entity_doctype="Supplier", entity_name="SUPP-001")
        fake_qj = MagicMock()
        fake_qj.name = "ECS-QJ-SUPP-001"
        with (
            patch("frappe.get_doc", return_value=sr),
            patch("frappe.db.get_value", return_value="EE-ACCT-MAIN"),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value=fake_qj,
            ) as enq,
        ):
            result = sr_mod.retry_now(sr.name)

        self.assertTrue(result["refire"]["enqueued"])
        self.assertEqual(result["refire"]["job_type"], "Supplier Push")
        kw = enq.call_args.kwargs
        self.assertEqual(kw["job_type"], "Supplier Push")
        self.assertEqual(kw["target_doctype"], "Supplier")
        self.assertEqual(kw["payload"]["supplier_docname"], "SUPP-001")

    def test_account_aware_handlers_skip_when_no_enabled_account(self) -> None:
        """Item / Customer / Supplier handlers need an EE Account to
        push against. When none is enabled, the handler must report a
        clear reason — not crash, not enqueue with a bogus None
        account. gh#86-reopen: status must also stay Failed (the
        enqueue didn't happen, so we can't honour §6.5.1's
        'next attempt enqueued' rule — flipping anyway would strand
        the record)."""
        sr = self._fake(entity_doctype="Item", entity_name="SKU-A")
        with (
            patch("frappe.get_doc", return_value=sr),
            patch("frappe.db.get_value", return_value=None),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
            ) as enq,
        ):
            result = sr_mod.retry_now(sr.name)

        self.assertFalse(result["refire"]["enqueued"])
        self.assertIn("No enabled EasyEcom Account", result["refire"]["reason"])
        enq.assert_not_called()
        # gh#86-reopen: status stays Failed when enqueue didn't happen.
        sr.db_set.assert_not_called()
        self.assertEqual(result["status"], "Failed")

    def test_po_refire_enqueues_po_push_job_with_location_key(self) -> None:
        sr = self._fake(
            entity_doctype="Purchase Order",
            entity_name="PO-2026-001",
            ee_location_key="ee69396945489",
        )
        fake_qj = MagicMock()
        fake_qj.name = "ECS-QJ-PO-001"
        with (
            patch("frappe.get_doc", return_value=sr),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value=fake_qj,
            ) as enq,
        ):
            result = sr_mod.retry_now(sr.name)

        self.assertTrue(result["refire"]["enqueued"])
        self.assertEqual(result["refire"]["job_type"], "PO Push")
        kw = enq.call_args.kwargs
        self.assertEqual(kw["job_type"], "PO Push")
        self.assertEqual(kw["target_doctype"], "Purchase Order")
        self.assertEqual(kw["target_name"], "PO-2026-001")
        # Retry doesn't trigger the status-push side-effect; FDE can
        # invoke that separately if they need it.
        self.assertEqual(kw["payload"]["push_status_after_content"], 0)

    def test_so_refire_enqueues_so_push_job_with_correlation_id(self) -> None:
        sr = self._fake(
            entity_doctype="Sales Order",
            entity_name="SO-2026-001",
            ee_location_key="ee69396945489",
        )
        fake_qj = MagicMock()
        fake_qj.name = "ECS-QJ-SO-001"
        with (
            patch("frappe.get_doc", return_value=sr),
            patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job",
                return_value=fake_qj,
            ) as enq,
            patch(
                "ecommerce_super.easyecom.utils.correlation.new_correlation_id",
                return_value="corr-test-001",
            ),
        ):
            result = sr_mod.retry_now(sr.name)

        self.assertTrue(result["refire"]["enqueued"])
        self.assertEqual(result["refire"]["job_type"], "SO Push")
        kw = enq.call_args.kwargs
        self.assertEqual(kw["job_type"], "SO Push")
        self.assertEqual(kw["target_doctype"], "Sales Order")
        self.assertEqual(kw["target_name"], "SO-2026-001")
        # SO Push carries a fresh correlation_id per attempt
        # (matches the on_submit hook's behaviour at push.py:118).
        self.assertEqual(kw["correlation_id"], "corr-test-001")


if __name__ == "__main__":
    unittest.main()
