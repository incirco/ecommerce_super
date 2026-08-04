"""gh#252 — GRN sweep per-GRN isolation.

A single GRN whose `process_one_grn` raises (e.g. a §10 IPR that fails on
GL/valuation and whose failure-recording itself raised LinkValidationError)
must NOT abort the whole sweep. Every other GRN on the page still pulls, and
the bad one is recorded as a Failed GRNOutcome via
`_record_isolated_grn_failure`.

REGRESSION CONTEXT

Live (Modern Marwar, live16version, 2026-08-04): one §10 GRN (2280859)
failed its IPR submit with "Incorrect number of General Ledger Entries",
the failure-recording then raised `LinkValidationError: Could not find
Entity: ECS-GRN-2280859`, and — because the sweep loop had no per-GRN
isolation — the whole pull aborted with "Pulled 0 GRN(s) across 1
location(s)". The fix wraps each GRN in the §7.1 `for_each_record`
savepoint primitive (the same helper every other batch flow uses).
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows import grn_pull as mod


class _Boom(Exception):
    """Stand-in for the LinkValidationError seen live."""


class TestGrnSweepIsolation(unittest.TestCase):
    def _run(self, page_rows, *, bad_grn_id):
        """One-page sweep where process_one_grn raises for `bad_grn_id`
        and succeeds (Receipted) for everything else."""
        fake_client = MagicMock()
        fake_client.get.return_value = {"data": page_rows, "nextUrl": None}

        def _process(grn_row, *, account_name=None, client=None):
            gid = int((grn_row or {}).get("grn_id") or 0)
            if gid == bad_grn_id:
                raise _Boom(f"Could not find Entity: ECS-GRN-{gid}")
            return mod.GRNOutcome(
                ee_grn_id=gid, operation="noop", grn_map_status="Receipted"
            )

        with (
            patch.object(mod, "process_one_grn", side_effect=_process),
            patch.object(mod.frappe.db, "get_value", return_value=None),
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback"),
            patch.object(mod.frappe.db, "set_value"),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod, "_upsert_grn_map_failed"),
            patch.object(mod.frappe, "log_error"),
            patch.object(
                mod, "now_datetime", return_value=datetime(2026, 8, 4, 12, 0, 0)
            ),
        ):
            return mod.pull_grns_for_location(
                location_key="LOC-1",
                account_name="Acc",
                client=fake_client,
                max_pages=1,
            )

    def test_bad_grn_does_not_abort_sweep(self) -> None:
        # The headline: a raising GRN in the MIDDLE of the page must not
        # abort. Pre-fix this returned 0 GRNs; now all three are recorded.
        rows = [
            {"grn_id": 2270064, "grn_created_at": "2026-08-01 10:00:00"},
            {"grn_id": 2280859, "grn_created_at": "2026-08-02 10:00:00"},  # raises
            {"grn_id": 2269648, "grn_created_at": "2026-08-03 10:00:00"},
        ]
        sweep = self._run(rows, bad_grn_id=2280859)
        self.assertEqual(sweep.grns_processed, 3)
        by_id = {o.ee_grn_id: o for o in sweep.outcomes}
        self.assertEqual(by_id[2270064].grn_map_status, "Receipted")
        # The good GRN AFTER the bad one still processed.
        self.assertEqual(by_id[2269648].grn_map_status, "Receipted")
        # The bad one is recorded Failed, not lost, not aborting.
        self.assertEqual(by_id[2280859].grn_map_status, "Failed")
        self.assertEqual(by_id[2280859].operation, "failed")

    def test_bad_grn_first_still_pulls_rest(self) -> None:
        rows = [
            {"grn_id": 2280859, "grn_created_at": "2026-08-02 10:00:00"},  # raises, first
            {"grn_id": 2270064, "grn_created_at": "2026-08-01 10:00:00"},
        ]
        sweep = self._run(rows, bad_grn_id=2280859)
        self.assertEqual(sweep.grns_processed, 2)
        by_id = {o.ee_grn_id: o for o in sweep.outcomes}
        self.assertEqual(by_id[2270064].grn_map_status, "Receipted")

    def test_all_good_page_unchanged(self) -> None:
        # No failure → behaviour identical to before (every GRN receipted).
        rows = [
            {"grn_id": 111, "grn_created_at": "2026-08-01 10:00:00"},
            {"grn_id": 222, "grn_created_at": "2026-08-02 10:00:00"},
        ]
        sweep = self._run(rows, bad_grn_id=-1)  # nothing matches → no failures
        self.assertEqual(sweep.grns_processed, 2)
        self.assertTrue(all(o.grn_map_status == "Receipted" for o in sweep.outcomes))


class TestRecordIsolatedGrnFailure(unittest.TestCase):
    def test_marks_failed_and_upserts_map(self) -> None:
        grn_row = {
            "grn_id": 2280859,
            "inwarded_warehouse_c_id": 263433,
            "vendor_c_id": 263433,
        }
        with (
            patch.object(mod, "_upsert_grn_map_failed") as m_upsert,
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback"),
            patch.object(mod.frappe, "log_error"),
        ):
            out = mod._record_isolated_grn_failure(
                ee_grn_id=2280859,
                grn_row=grn_row,
                exc=_Boom("Could not find Entity: ECS-GRN-2280859"),
            )
        self.assertEqual(out.operation, "failed")
        self.assertEqual(out.grn_map_status, "Failed")
        self.assertIn("2280859", out.flag_reasons[0])
        m_upsert.assert_called_once()
        self.assertEqual(m_upsert.call_args.kwargs.get("ee_grn_id"), 2280859)

    def test_never_raises_even_if_recorder_fails(self) -> None:
        # If the Failed-map write itself blows up, the helper still returns
        # a Failed outcome — it must never re-raise into the sweep.
        with (
            patch.object(
                mod, "_upsert_grn_map_failed", side_effect=RuntimeError("db down")
            ),
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback"),
            patch.object(mod.frappe, "log_error"),
        ):
            out = mod._record_isolated_grn_failure(
                ee_grn_id=1, grn_row={}, exc=_Boom("x")
            )
        self.assertEqual(out.operation, "failed")
        self.assertEqual(out.grn_map_status, "Failed")


if __name__ == "__main__":
    unittest.main()
