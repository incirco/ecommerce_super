"""Integration tests for the §7.1 per-record savepoint isolation helper.

THE mandatory isolation test (per the §8a packet): a batch where record #2
of 3 raises must still commit #1 and #3, and #2 must be recorded Failed.
This is the contract every batch flow inherits.

Records are EasyEcom Account rows for convenience — any DocType with a
simple insert path would do.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows._isolation import (
    BatchOutcome,
    for_each_record,
)


def _wipe_accounts(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Account",
        filters={"account_name": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Account", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestForEachRecordIsolation(FrappeTestCase):
    """The §7.1 contract: per-record savepoints — one record's failure
    must NEVER abort siblings."""

    PREFIX = "isolation-test-"

    def setUp(self) -> None:
        _wipe_accounts(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_accounts(self.PREFIX)

    def _insert_account(self, name: str) -> None:
        """Per-record handler that writes one EasyEcom Account.

        enabled=0 deliberately: the §8.1 single-Account constraint (added
        as part of the §8d audit follow-up) forbids more than one
        enabled Account; this test creates multiple Accounts in one
        batch to exercise per-record savepoint isolation, not to
        exercise Account-semantics. The savepoint behaviour the
        test asserts is independent of the enabled flag."""
        doc = frappe.new_doc("EasyEcom Account")
        doc.update(
            {
                "account_name": name,
                "enabled": 0,
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.example.test",
                "x_api_key": "stub-key",
                "email": "stub@example.test",
                "password": "stub-pass",
                "rate_limit_tier": "Silver",
                "webhook_enabled": 0,
            }
        )
        doc.insert(ignore_permissions=True)

    def test_middle_record_failure_does_not_abort_siblings(self) -> None:
        """THE mandatory test: 3 records, #2 raises, #1 and #3 commit,
        #2 lands in BatchOutcome.failed."""
        records = [
            f"{self.PREFIX}A-survives",
            f"{self.PREFIX}B-fails",
            f"{self.PREFIX}C-survives",
        ]
        failed_records: list[tuple[str, BaseException]] = []

        def _handler(rec: str) -> None:
            self._insert_account(rec)
            # Make the middle record raise AFTER its insert lands in the
            # savepoint — the rollback must undo that insert too. If the
            # rollback fails, the test will see record B persisted, which
            # is the failure mode the contract forbids.
            if rec.endswith("B-fails"):
                raise RuntimeError("simulated per-record failure")

        def _on_failure(rec: str, exc: BaseException) -> None:
            failed_records.append((rec, exc))

        outcome = for_each_record(
            records,
            handler=_handler,
            on_failure=_on_failure,
            flow_name="isolation-test",
        )
        frappe.db.commit()

        # outcome.succeeded contains #1 and #3 (the records, unmodified).
        self.assertEqual(outcome.succeeded_count, 2)
        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual([r for r in outcome.succeeded], [records[0], records[2]])

        # on_failure was called for #2 with the right exception.
        self.assertEqual(len(failed_records), 1)
        rec, exc = failed_records[0]
        self.assertEqual(rec, records[1])
        self.assertIsInstance(exc, RuntimeError)

        # CRITICAL: #2's insert was rolled back.
        # #1 and #3 are committed; #2 is NOT in the DB.
        self.assertTrue(frappe.db.exists("EasyEcom Account", {"account_name": records[0]}))
        self.assertFalse(frappe.db.exists("EasyEcom Account", {"account_name": records[1]}))
        self.assertTrue(frappe.db.exists("EasyEcom Account", {"account_name": records[2]}))

    def test_empty_batch_returns_empty_outcome(self) -> None:
        outcome = for_each_record([], handler=lambda r: None)
        self.assertEqual(outcome.succeeded_count, 0)
        self.assertEqual(outcome.failed_count, 0)
        self.assertEqual(outcome.total, 0)

    def test_all_success_no_failed(self) -> None:
        records = [f"{self.PREFIX}all-ok-{i}" for i in range(3)]
        outcome = for_each_record(
            records,
            handler=self._insert_account,
            flow_name="isolation-allok",
        )
        frappe.db.commit()
        self.assertEqual(outcome.succeeded_count, 3)
        self.assertEqual(outcome.failed_count, 0)
        for r in records:
            self.assertTrue(frappe.db.exists("EasyEcom Account", {"account_name": r}))

    def test_all_failure_no_succeeded_and_nothing_committed(self) -> None:
        records = ["x", "y", "z"]

        def _always_raise(rec: str) -> None:
            raise ValueError(f"no good: {rec}")

        outcome = for_each_record(records, handler=_always_raise)
        self.assertEqual(outcome.succeeded_count, 0)
        self.assertEqual(outcome.failed_count, 3)

    def test_on_failure_writes_survive_rollback(self) -> None:
        """The on_failure callback runs AFTER the savepoint rollback so
        anything it writes survives. This is the §2.7 mechanism — the
        failure record itself must not be rolled back by the rollback
        that handled the original exception."""
        failure_marker = f"{self.PREFIX}failure-record"

        def _handler(rec: str) -> None:
            raise RuntimeError("planned")

        def _on_failure(rec: str, exc: BaseException) -> None:
            # Write a marker row to prove this fires outside the rollback.
            self._insert_account(failure_marker)

        outcome = for_each_record(["only-record"], handler=_handler, on_failure=_on_failure)
        frappe.db.commit()
        self.assertEqual(outcome.failed_count, 1)
        self.assertTrue(
            frappe.db.exists("EasyEcom Account", {"account_name": failure_marker})
        )

    def test_on_failure_raising_does_not_abort_batch(self) -> None:
        """A buggy on_failure that itself raises must not kill the whole
        loop — it's logged and the loop continues to the next record."""
        records = ["a", "b", "c"]

        def _handler(rec: str) -> None:
            raise RuntimeError("primary fail")

        def _broken_on_failure(rec: str, exc: BaseException) -> None:
            raise ValueError("bug in callback")

        # Should NOT raise; all three records should land in `failed`.
        outcome = for_each_record(records, handler=_handler, on_failure=_broken_on_failure)
        self.assertEqual(outcome.failed_count, 3)
