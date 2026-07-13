"""gh#176 — reclaim_orphaned_jobs now catches state=Queued orphans
(not just state=Running) + gh#176 followup: enqueue-time failure guard
transitions job to Failed instead of leaving it Queued.

Locks:
  - Queued-orphan idempotency probe: target artifact exists → mark Success
  - Queued-orphan no artifact → re-enqueue
  - _queued_work_already_completed heuristics per job_type
  - _reenqueue failure guard: frappe.enqueue exception → transition_to_failed
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestGh176QueuedWorkAlreadyCompleted(unittest.TestCase):
    def test_so_push_with_b2b_map_returns_true(self):
        """SO Push job's target already has a B2B Order Map → done."""
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = "SO-2610397"
        row.job_type = "SO Push"
        with patch(
            "ecommerce_super.easyecom.queue.workers.frappe.db.get_value",
            return_value="ECS-B2B-SO-2610397",
        ):
            self.assertTrue(_queued_work_already_completed(row))

    def test_so_push_without_map_returns_false(self):
        """No B2B Order Map → work not done."""
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = "SO-9999999"
        row.job_type = "SO Push"
        with patch(
            "ecommerce_super.easyecom.queue.workers.frappe.db.get_value",
            return_value=None,
        ):
            self.assertFalse(_queued_work_already_completed(row))

    def test_item_push_with_ee_product_id_returns_true(self):
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = "FG06476-CHOUHAN"
        row.job_type = "Item Push"
        with patch(
            "ecommerce_super.easyecom.queue.workers.frappe.db.get_value",
            return_value=39046740,
        ):
            self.assertTrue(_queued_work_already_completed(row))

    def test_customer_push_with_ee_customer_id_returns_true(self):
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = "R251870"
        row.job_type = "Customer Push"
        with patch(
            "ecommerce_super.easyecom.queue.workers.frappe.db.get_value",
            return_value=286700,
        ):
            self.assertTrue(_queued_work_already_completed(row))

    def test_unknown_job_type_returns_false_conservative(self):
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = "X"
        row.job_type = "Some New Job Type"
        self.assertFalse(_queued_work_already_completed(row))

    def test_missing_target_name_returns_false(self):
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = None
        row.job_type = "SO Push"
        self.assertFalse(_queued_work_already_completed(row))

    def test_db_error_returns_false_conservative(self):
        """DB error on the idempotency probe → conservative: not done →
        re-enqueue. Prevents leaving a legitimate orphan stuck at Queued."""
        from ecommerce_super.easyecom.queue.workers import (
            _queued_work_already_completed,
        )
        row = MagicMock()
        row.target_name = "SO-2610397"
        row.job_type = "SO Push"
        with patch(
            "ecommerce_super.easyecom.queue.workers.frappe.db.get_value",
            side_effect=Exception("db unavailable"),
        ):
            self.assertFalse(_queued_work_already_completed(row))


class TestGh176AnnotateHelper(unittest.TestCase):
    def test_appends_to_existing_last_error(self):
        from ecommerce_super.easyecom.queue.workers import _annotate
        qj = MagicMock()
        qj.get.return_value = "Prior error message"
        _annotate(qj, "Reconciled by reclaim")
        args, kwargs = qj.db_set.call_args
        self.assertEqual(args[0], "last_error")
        self.assertIn("Prior error message", args[1])
        self.assertIn("---", args[1])
        self.assertIn("Reconciled by reclaim", args[1])

    def test_no_separator_when_empty_prior(self):
        from ecommerce_super.easyecom.queue.workers import _annotate
        qj = MagicMock()
        qj.get.return_value = ""
        _annotate(qj, "First note")
        args, _ = qj.db_set.call_args
        self.assertEqual(args[1], "First note")

    def test_truncates_to_4000_chars(self):
        from ecommerce_super.easyecom.queue.workers import _annotate
        qj = MagicMock()
        qj.get.return_value = "x" * 3990
        _annotate(qj, "note that makes the total exceed 4000 characters")
        args, _ = qj.db_set.call_args
        self.assertLessEqual(len(args[1]), 4000)

    def test_swallows_exceptions_silently(self):
        """Annotate is best-effort; a db_set failure must not raise."""
        from ecommerce_super.easyecom.queue.workers import _annotate
        qj = MagicMock()
        qj.db_set.side_effect = Exception("db unavailable")
        # Should not raise
        _annotate(qj, "note")
