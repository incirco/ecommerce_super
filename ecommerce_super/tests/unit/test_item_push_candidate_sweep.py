"""gh#17 — `_candidate_items_for_sweep` must not crash when the IC
custom field `gst_hsn_code` is absent.

The previous query hard-referenced `i.gst_hsn_code` in the WHERE clause.
On sites without India Compliance installed (or with the IC custom
field migration not yet applied), the column is absent and MariaDB
raised `OperationalError: Unknown column 'i.gst_hsn_code' in 'WHERE'`
BEFORE any Queue Job could be enqueued — violating the resilience
contract the issue cites ("ERPNext actions should complete and create
background queue entries even when EasyEcom is unavailable").

The fix introspects `frappe.db.has_column("Item", "gst_hsn_code")`
once and drops the HSN clause when the column is missing. These tests
exercise the branching against a mocked frappe.db so we don't need a
live MariaDB / IC install to verify the logic.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestCandidateItemsForSweep(unittest.TestCase):
    def _run(self, *, has_column: bool, limit: int | None = None):
        from ecommerce_super.easyecom.flows import item_push

        captured_sql: dict = {}

        def _sql(query, *_args, **_kwargs):
            captured_sql["query"] = query
            return []

        with (
            patch.object(frappe.db, "has_column", return_value=has_column),
            patch.object(frappe.db, "sql", side_effect=_sql),
        ):
            result = item_push._candidate_items_for_sweep(limit=limit)
        return result, captured_sql["query"]

    def test_hsn_column_present_query_includes_filter(self) -> None:
        _, query = self._run(has_column=True)
        self.assertIn("i.gst_hsn_code IS NOT NULL", query)
        self.assertIn("i.gst_hsn_code != ''", query)

    def test_hsn_column_missing_query_omits_filter_no_crash(self) -> None:
        result, query = self._run(has_column=False)
        # No HSN reference anywhere in the WHERE clause.
        self.assertNotIn("gst_hsn_code", query)
        # Other gates still in place.
        self.assertIn("i.disabled = 0", query)
        self.assertIn("i.is_stock_item = 1", query)
        self.assertIn("m.name IS NULL", query)
        self.assertIn("pb.name IS NULL", query)
        # Result is well-formed (empty list from mocked sql).
        self.assertEqual(result, [])

    def test_limit_appended_when_provided(self) -> None:
        _, query = self._run(has_column=True, limit=25)
        self.assertIn("LIMIT 25", query)

    def test_limit_omitted_when_none(self) -> None:
        _, query = self._run(has_column=True, limit=None)
        self.assertNotIn("LIMIT", query)


if __name__ == "__main__":
    unittest.main()
