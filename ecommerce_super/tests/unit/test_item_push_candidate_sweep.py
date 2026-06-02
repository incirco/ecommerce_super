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


class TestBuildPushPayloadHsnGate(unittest.TestCase):
    """gh#17 follow-up — verify build_push_payload flags items without
    ProductTaxCode so column-less / IC-less sites don't silently push
    HSN-less payloads.

    The sweep-side fix (above) enqueues items even when gst_hsn_code is
    absent. Without this payload-side gate, the per-item worker would
    build a payload with no ProductTaxCode, the None-strip step would
    drop the field, and EE would receive an HSN-less product — a silent
    catalogue corruption. The flag turns it into a visible
    Flagged-Not-Pushed outcome instead.
    """

    def _call(self, ruleset_output: dict) -> tuple[dict, list[str]]:
        """Invoke build_push_payload with a stubbed executor that
        returns the supplied dict, and stubs around TaxRate /
        physical-dim resolution that aren't under test here."""
        from ecommerce_super.easyecom.flows import item_push

        executor = MagicMock()
        executor.push = MagicMock(return_value=dict(ruleset_output))

        item = MagicMock()
        item.barcodes = []

        # Patch the helpers build_push_payload calls inline so we
        # exercise only the HSN gate, not TaxRate / dim resolution.
        with (
            patch.object(item_push, "_ean_barcode", return_value=None),
            patch.object(item_push, "_resolve_tax_rate", return_value=18.0),
            patch.object(item_push, "_is_missing_or_zero", return_value=False),
        ):
            return item_push.build_push_payload(
                item, executor=executor, enabled_companies=["X"]
            )

    def test_hsn_present_no_flag(self) -> None:
        """ProductTaxCode in ruleset output → no HSN flag, payload
        carries the field."""
        payload, reasons = self._call(
            {
                "ProductTaxCode": "8516",
                "Weight": 100, "Length": 10, "Height": 10, "Width": 10,
            }
        )
        self.assertEqual(reasons, [])
        self.assertEqual(payload["ProductTaxCode"], "8516")

    def test_hsn_absent_raises_flag(self) -> None:
        """No ProductTaxCode (column missing / IC absent / Item HSN
        empty) → flag and the item must NOT push."""
        payload, reasons = self._call(
            {
                # No ProductTaxCode key at all.
                "Weight": 100, "Length": 10, "Height": 10, "Width": 10,
            }
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn("ProductTaxCode (HSN) missing", reasons[0])
        # Step (4) None-strip removes it; payload must NOT carry an
        # empty / null ProductTaxCode that would slip past EE's gate.
        self.assertNotIn("ProductTaxCode", payload)

    def test_hsn_empty_string_treated_as_absent(self) -> None:
        """ProductTaxCode='' (some rulesets emit empty when source is
        missing) → flag, same as outright absent."""
        payload, reasons = self._call(
            {
                "ProductTaxCode": "",
                "Weight": 100, "Length": 10, "Height": 10, "Width": 10,
            }
        )
        self.assertTrue(any("ProductTaxCode (HSN) missing" in r for r in reasons))
        self.assertNotIn("ProductTaxCode", payload)


if __name__ == "__main__":
    unittest.main()
