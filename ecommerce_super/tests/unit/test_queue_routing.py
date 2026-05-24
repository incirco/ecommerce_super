"""Unit tests for the queue facade's routing decisions and retry/backoff math."""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.queue import routing, workers


class TestQueueRouting(unittest.TestCase):
    def test_short_tier_for_webhook_processing(self) -> None:
        """Webhooks must process fast (§6.3.2)."""
        self.assertEqual(routing.queue_for("Webhook Process"), "short")
        self.assertEqual(routing.timeout_for("Webhook Process"), 60)

    def test_default_tier_for_routine_work(self) -> None:
        for jt in ("Item Push", "Customer Push", "PO Push", "Order Pull", "GRN Pull"):
            self.assertEqual(routing.queue_for(jt), "default")

    def test_long_tier_for_bulk_compute(self) -> None:
        for jt in ("Inventory Pull", "Master Sync Bulk", "Morning Brief Compute"):
            self.assertEqual(routing.queue_for(jt), "long")

    def test_unknown_job_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            routing.queue_for("Fake Job Type")

    def test_all_19_job_types_routed(self) -> None:
        """Every Queue Job state's job_type option has a routing entry."""
        self.assertEqual(len(routing.QUEUE_FOR_JOB_TYPE), 19)
        self.assertEqual(len(routing.TIMEOUT_FOR_JOB_TYPE), 19)
        # Same key set.
        self.assertEqual(
            set(routing.QUEUE_FOR_JOB_TYPE.keys()),
            set(routing.TIMEOUT_FOR_JOB_TYPE.keys()),
        )


class TestBackoff(unittest.TestCase):
    def test_backoff_grows_exponentially_then_caps(self) -> None:
        # First attempts: 30s base, doubling. Capped at 3600s (1h).
        b1 = workers.compute_backoff(1)
        b3 = workers.compute_backoff(3)
        b10 = workers.compute_backoff(10)
        self.assertTrue(60 <= b1 <= 90)  # 2*30 + jitter
        self.assertTrue(240 <= b3 <= 270)  # 8*30 + jitter
        self.assertTrue(3600 <= b10 <= 3630)  # capped + jitter

    def test_backoff_includes_jitter(self) -> None:
        """Multiple calls at same attempt produce different values."""
        values = {workers.compute_backoff(5) for _ in range(20)}
        # With ±0-30 jitter range and 20 samples, near-certain to have >1 distinct.
        self.assertGreater(len(values), 1)


if __name__ == "__main__":
    unittest.main()
