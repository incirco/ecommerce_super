"""gh#26 — `warehouse_query` must tolerate the missing
`ecs_ee_location_label` column on Warehouse.

On `mmpl16` (Frappe Cloud UAT, 2026-06-12), the
`add_warehouse_ee_location_label` patch silently no-op'd — Patch Log
records "executed" but the column doesn't exist. Every DN form load
hit `Unknown column 'ecs_ee_location_label'` because
`predict_section10_branch` blindly read the field.

This test freezes the contract: both whitelisted endpoints in
`warehouse_query` must inspect `frappe.db.has_column` once and degrade
gracefully (empty labels, no crash) when the column is absent. The
rescue patch creates the column; this is the belt-and-braces
defensive guard so an env mismatch can't take down every warehouse
picker on the site.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


class TestWarehouseLabelColumnOptional(unittest.TestCase):
    def setUp(self) -> None:
        # The helper memoises via lru_cache; clear between cases so the
        # `has_column` mock takes effect.
        from ecommerce_super.easyecom.api.warehouse_query import (
            _warehouse_has_label_column,
        )
        _warehouse_has_label_column.cache_clear()

    def test_helper_returns_true_when_column_present(self) -> None:
        from ecommerce_super.easyecom.api.warehouse_query import (
            _warehouse_has_label_column,
        )
        with patch.object(frappe.db, "has_column", return_value=True):
            self.assertTrue(_warehouse_has_label_column())

    def test_helper_returns_false_when_column_missing(self) -> None:
        from ecommerce_super.easyecom.api.warehouse_query import (
            _warehouse_has_label_column,
        )
        with patch.object(frappe.db, "has_column", return_value=False):
            self.assertFalse(_warehouse_has_label_column())

    def test_helper_returns_false_when_has_column_raises(self) -> None:
        """Defensive — a corrupted schema cache shouldn't take down the
        whole module load path."""
        from ecommerce_super.easyecom.api.warehouse_query import (
            _warehouse_has_label_column,
        )
        with patch.object(
            frappe.db, "has_column", side_effect=Exception("boom")
        ):
            self.assertFalse(_warehouse_has_label_column())

    def test_predict_branch_no_crash_when_column_missing(self) -> None:
        """gh#26 — `predict_section10_branch` must NOT raise when the
        label column is missing. It should return the same dict shape
        as the green path, with empty source_label / target_label."""
        from ecommerce_super.easyecom.api import warehouse_query

        # _is_ee_mapped_warehouse is unrelated to the label column —
        # mock it to focus on the label-column path.
        with (
            patch.object(frappe.db, "has_column", return_value=False),
            patch(
                "ecommerce_super.easyecom.flows.transfer_push._is_ee_mapped_warehouse",
                return_value=True,
            ),
            # The function shouldn't even call get_value when the
            # column is missing; assert that here.
            patch.object(frappe.db, "get_value") as get_value_mock,
        ):
            result = warehouse_query.predict_section10_branch(
                source_warehouse="WH-SRC - TC",
                target_warehouse="WH-TGT - TC",
            )

        # Branch resolution still works.
        self.assertEqual(result["branch"], "STN")
        # Labels are empty rather than raising.
        self.assertEqual(result["source_label"], "")
        self.assertEqual(result["target_label"], "")
        # No label lookup attempted.
        get_value_mock.assert_not_called()

    def test_predict_branch_uses_get_value_when_column_present(self) -> None:
        """Sanity check the green path — when the column exists, the
        helper still queries it. Mirrors pre-patch behavior."""
        from ecommerce_super.easyecom.api import warehouse_query

        def _get_value(_doctype, name, _field):
            return f"[EE] {name}"

        with (
            patch.object(frappe.db, "has_column", return_value=True),
            patch(
                "ecommerce_super.easyecom.flows.transfer_push._is_ee_mapped_warehouse",
                return_value=True,
            ),
            patch.object(frappe.db, "get_value", side_effect=_get_value),
        ):
            result = warehouse_query.predict_section10_branch(
                source_warehouse="WH-SRC - TC",
                target_warehouse="WH-TGT - TC",
            )

        self.assertEqual(result["source_label"], "[EE] WH-SRC - TC")
        self.assertEqual(result["target_label"], "[EE] WH-TGT - TC")


if __name__ == "__main__":
    unittest.main()
