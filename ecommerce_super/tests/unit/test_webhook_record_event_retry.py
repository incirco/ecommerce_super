"""gh#4 — burst-safe `_record_event` retry on DB transient errors.

Under N6 concurrency testing (>5 req/sec on Default tier), concurrent
inserts into EasyEcom Webhook Event were colliding on row/index locks
and producing raw 500s with no Webhook Event row created. SPEC §7.2
requires every receipt to land on a row — silent drops violate that.

The fix layers two responses:
  - Retry on QueryDeadlockError / QueryTimeoutError with short backoff.
  - On final failure, the caller writes an Error Log entry (visible in
    the desk's Error Log list) and returns 503 (not 500), so EE knows
    to retry and the failure isn't invisible.

These tests cover the retry helper itself with a mocked _record_event;
the caller-side log + 503 behavior is exercised in the existing webhook
integration tests via a separate burst scenario.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestRecordEventRetry(unittest.TestCase):
    def _kwargs(self) -> dict:
        return dict(
            account=MagicMock(),
            event_type="dispatch",
            ee_event_id="evt-001",
            payload={"a": 1},
            source_ip="1.1.1.1",
            auth_header_used="Access-token",
            ip_check="Skipped",
            location_key="LOC",
            company="_Test Co",
            processing_state="Pending",
            processing_error=None,
        )

    def test_first_attempt_success_no_retry(self) -> None:
        from ecommerce_super.easyecom.api import webhook

        with patch.object(webhook, "_record_event", return_value="EVT-1") as m:
            with patch.object(webhook.time, "sleep") as sleep_mock:
                result = webhook._record_event_with_retry(**self._kwargs())
        self.assertEqual(result, "EVT-1")
        self.assertEqual(m.call_count, 1)
        sleep_mock.assert_not_called()

    def test_unique_validation_error_does_not_retry(self) -> None:
        from ecommerce_super.easyecom.api import webhook

        with patch.object(
            webhook,
            "_record_event",
            side_effect=frappe.exceptions.UniqueValidationError("dup"),
        ) as m:
            with patch.object(webhook.time, "sleep") as sleep_mock:
                with self.assertRaises(frappe.exceptions.UniqueValidationError):
                    webhook._record_event_with_retry(**self._kwargs())
        self.assertEqual(m.call_count, 1)
        sleep_mock.assert_not_called()

    def test_deadlock_then_success(self) -> None:
        from ecommerce_super.easyecom.api import webhook

        side_effects = [
            frappe.exceptions.QueryDeadlockError("deadlock"),
            "EVT-RETRY",
        ]
        with patch.object(webhook, "_record_event", side_effect=side_effects) as m:
            with patch.object(webhook.time, "sleep") as sleep_mock:
                with patch.object(frappe.db, "rollback"):
                    result = webhook._record_event_with_retry(**self._kwargs())
        self.assertEqual(result, "EVT-RETRY")
        self.assertEqual(m.call_count, 2)
        # Slept once before retry.
        self.assertEqual(sleep_mock.call_count, 1)

    def test_timeout_then_success(self) -> None:
        from ecommerce_super.easyecom.api import webhook

        side_effects = [
            frappe.exceptions.QueryTimeoutError("lock wait timeout"),
            "EVT-RETRY",
        ]
        with patch.object(webhook, "_record_event", side_effect=side_effects):
            with patch.object(webhook.time, "sleep"):
                with patch.object(frappe.db, "rollback"):
                    result = webhook._record_event_with_retry(**self._kwargs())
        self.assertEqual(result, "EVT-RETRY")

    def test_persistent_deadlock_exhausts_retries_and_raises(self) -> None:
        from ecommerce_super.easyecom.api import webhook

        with patch.object(
            webhook,
            "_record_event",
            side_effect=frappe.exceptions.QueryDeadlockError("deadlock"),
        ) as m:
            with patch.object(webhook.time, "sleep") as sleep_mock:
                with patch.object(frappe.db, "rollback"):
                    with self.assertRaises(frappe.exceptions.QueryDeadlockError):
                        webhook._record_event_with_retry(**self._kwargs())
        self.assertEqual(m.call_count, webhook._RECORD_EVENT_MAX_ATTEMPTS)
        # Sleeps between attempts only (not after the last).
        self.assertEqual(sleep_mock.call_count, webhook._RECORD_EVENT_MAX_ATTEMPTS - 1)

    def test_non_transient_exception_does_not_retry(self) -> None:
        from ecommerce_super.easyecom.api import webhook

        class _Boom(Exception):
            pass

        with patch.object(webhook, "_record_event", side_effect=_Boom("boom")) as m:
            with patch.object(webhook.time, "sleep") as sleep_mock:
                with self.assertRaises(_Boom):
                    webhook._record_event_with_retry(**self._kwargs())
        self.assertEqual(m.call_count, 1)
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
