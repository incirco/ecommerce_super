"""gh#205 part 2 — one-shot migration patch that heals any remaining
pre-fix Draft SIs (set_posting_time=0 with EE invoice_id back-ref) so
the runtime `_reassert_si_dates_for_submit` healer can be safely
deleted.

Locks:
  - Skips gracefully when the ecs_easyecom_invoice_id column is absent
    (fresh install, app not yet migrated)
  - No-op + log entry when zero candidates found (fresh install or
    already-healed sites)
  - Iterates candidates and heals each one (set_posting_time=1, due_date
    aligned, payment_terms_template cleared)
  - Per-SI heal failure is caught + logged; loop continues to next SI
  - Final summary log entry names count of healed SIs
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from ecommerce_super.patches.v0_1 import (
    heal_gh205_pre_fix_draft_si_dates as mod,
)


class TestHealPatchSafetyGuards(unittest.TestCase):
    def test_skips_when_column_missing_no_writes(self):
        """App not installed on this site (column absent) → early return."""
        set_value_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=False),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.execute()
        set_value_mock.assert_not_called()

    def test_no_candidates_logs_zero_and_returns(self):
        """Site with no pre-fix Drafts → logs a "zero found" entry and
        returns without any writes."""
        log_mock = MagicMock()
        set_value_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(mod.frappe, "get_all", return_value=[]),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error", side_effect=log_mock),
        ):
            mod.execute()
        set_value_mock.assert_not_called()
        # A log entry was written naming the zero-count outcome
        log_mock.assert_called_once()
        title = log_mock.call_args.kwargs.get("title", "")
        self.assertIn("no pre-fix Draft SIs", title)


class TestHealPatchHealsCandidates(unittest.TestCase):
    def _candidate(self, name="SI-PRE-FIX-01", **overrides):
        base = {
            "name": name,
            "posting_date": "2026-07-11",
            "due_date": "2026-07-11",
            "payment_terms_template": None,
        }
        base.update(overrides)
        return base

    def test_healed_si_gets_set_posting_time_1(self):
        set_value_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod.frappe, "get_all",
                return_value=[self._candidate("SI-01")],
            ),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.execute()
        # First positional arg = doctype; 2nd = name; 3rd = updates dict
        args = set_value_mock.call_args.args
        self.assertEqual(args[0], "Sales Invoice")
        self.assertEqual(args[1], "SI-01")
        self.assertEqual(args[2].get("set_posting_time"), 1)

    def test_due_date_aligned_when_before_posting_date(self):
        """Draft has due_date < posting_date — heal aligns due to posting."""
        set_value_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod.frappe, "get_all",
                return_value=[self._candidate(
                    "SI-02",
                    posting_date="2026-07-13",
                    due_date="2026-07-10",  # earlier — must push forward
                )],
            ),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.execute()
        updates = set_value_mock.call_args.args[2]
        self.assertEqual(updates.get("due_date"), "2026-07-13")

    def test_due_date_untouched_when_already_after_posting_date(self):
        """Correct due_date → not overwritten (only set_posting_time = 1)."""
        set_value_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod.frappe, "get_all",
                return_value=[self._candidate(
                    "SI-03",
                    posting_date="2026-07-11",
                    due_date="2026-07-14",  # later — must NOT be pushed back
                )],
            ),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.execute()
        updates = set_value_mock.call_args.args[2]
        self.assertNotIn("due_date", updates)

    def test_payment_terms_template_cleared_when_present(self):
        set_value_mock = MagicMock()
        delete_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod.frappe, "get_all",
                return_value=[self._candidate(
                    "SI-04",
                    payment_terms_template="Net 30",
                )],
            ),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "delete", side_effect=delete_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.execute()
        updates = set_value_mock.call_args.args[2]
        self.assertEqual(updates.get("payment_terms_template"), "")
        # payment_schedule child rows also cleared
        delete_mock.assert_called_once()
        self.assertEqual(
            delete_mock.call_args.args[0], "Payment Schedule",
        )

    def test_per_si_heal_failure_does_not_stop_loop(self):
        """A per-SI heal failure (e.g. SI vanished mid-migrate) must be
        caught + logged; the loop continues with the next candidate."""
        set_value_mock = MagicMock(side_effect=[
            Exception("SI-A vanished"),  # first heal fails
            None,                          # second heal succeeds
        ])
        log_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod.frappe, "get_all",
                return_value=[
                    self._candidate("SI-A"),
                    self._candidate("SI-B"),
                ],
            ),
            patch.object(mod.frappe.db, "set_value", side_effect=set_value_mock),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error", side_effect=log_mock),
        ):
            mod.execute()
        # set_value called twice (didn't stop at first exception)
        self.assertEqual(set_value_mock.call_count, 2)
        # Two log entries: per-SI failure + final summary
        titles = [c.kwargs.get("title", "") for c in log_mock.call_args_list]
        self.assertTrue(any("failed on SI-A" in t for t in titles))
        self.assertTrue(any("healed 1 of 2" in t for t in titles))

    def test_final_summary_names_healed_count(self):
        log_mock = MagicMock()
        with (
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod.frappe, "get_all",
                return_value=[
                    self._candidate("SI-A"),
                    self._candidate("SI-B"),
                    self._candidate("SI-C"),
                ],
            ),
            patch.object(mod.frappe.db, "set_value"),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod.frappe, "log_error", side_effect=log_mock),
        ):
            mod.execute()
        summary_title = log_mock.call_args_list[-1].kwargs.get("title", "")
        self.assertIn("healed 3 of 3", summary_title)


class TestRuntimeHealerRemoved(unittest.TestCase):
    """gh#205 part 2 — regression guard that the runtime healer stays
    deleted. Static-source check on gsp_handler."""

    def test_reassert_si_dates_for_submit_does_not_exist(self):
        """The function was deleted in gh#205 part 2. Any future
        contributor who tries to reintroduce it (via copy-paste or
        forgetting the migration patch) will hit this guard."""
        from ecommerce_super.easyecom.flows.b2b_sales import gsp_handler
        self.assertFalse(
            hasattr(gsp_handler, "_reassert_si_dates_for_submit"),
            "gh#205 regression: _reassert_si_dates_for_submit was "
            "reintroduced. It's dead code — the migration patch "
            "heal_gh205_pre_fix_draft_si_dates handles the same case "
            "at migrate time. Delete the runtime healer.",
        )


if __name__ == "__main__":
    unittest.main()
