"""gh#150 Part 1 — Custom GSP Health metrics endpoint tests.

Locks:
  - Permission gate: requires read on EasyEcom Account
  - Payload shape: 6 keys (5 metrics + as_of)
  - Each metric is _safe-wrapped — one broken query doesn't blank the
    whole card (degrades to zero/None instead)
  - Individual metric queries return the right types
  - Empty-database returns zeros/None/[] cleanly
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.api import gsp_health as mod


class TestGetMetricsPayloadShape(unittest.TestCase):
    """The endpoint returns a fixed set of keys — the frontend depends
    on this shape being stable."""

    def test_returns_all_5_metrics_plus_as_of(self):
        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(mod, "_last_successful_einvoice_at", return_value=None),
            patch.object(mod, "_count_inbound_today", return_value=3),
            patch.object(mod, "_count_failed_inbound_today", return_value=1),
            patch.object(mod, "_top_failure_reasons", return_value=[]),
            patch.object(mod, "_count_stuck_orders_over_6h", return_value=0),
        ):
            result = mod.get_metrics()
        expected_keys = {
            "last_success_at",
            "inbound_today",
            "failed_inbound_today",
            "top_failure_reasons",
            "stuck_orders_6h",
            "as_of",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_empty_database_returns_sensible_defaults(self):
        """Fresh install / zero-data scenario. Each metric should
        return its identity value, not raise."""
        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(mod, "_last_successful_einvoice_at", return_value=None),
            patch.object(mod, "_count_inbound_today", return_value=0),
            patch.object(mod, "_count_failed_inbound_today", return_value=0),
            patch.object(mod, "_top_failure_reasons", return_value=[]),
            patch.object(mod, "_count_stuck_orders_over_6h", return_value=0),
        ):
            result = mod.get_metrics()
        self.assertIsNone(result["last_success_at"])
        self.assertEqual(result["inbound_today"], 0)
        self.assertEqual(result["failed_inbound_today"], 0)
        self.assertEqual(result["top_failure_reasons"], [])
        self.assertEqual(result["stuck_orders_6h"], 0)


class TestGetMetricsPermissionGate(unittest.TestCase):
    def test_refuses_without_easyecom_account_read(self):
        with (
            patch.object(mod.frappe, "has_permission", return_value=False),
            self.assertRaises(Exception) as ctx,
        ):
            mod.get_metrics()
        self.assertIn("Not permitted", str(ctx.exception))


class TestPerMetricFailureIsolation(unittest.TestCase):
    """Cornerstone safety: any single metric query failure must NOT
    blank the entire card. _safe wraps each metric and degrades that
    specific one to zero/None on any exception."""

    def test_one_broken_metric_does_not_blank_others(self):
        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(
                mod, "_last_successful_einvoice_at",
                side_effect=Exception("column missing"),
            ),
            patch.object(mod, "_count_inbound_today", return_value=42),
            patch.object(mod, "_count_failed_inbound_today", return_value=3),
            patch.object(mod, "_top_failure_reasons", return_value=[
                {"reason": "IC error", "count": 2},
            ]),
            patch.object(mod, "_count_stuck_orders_over_6h", return_value=5),
        ):
            result = mod.get_metrics()
        # Broken metric degraded to None
        self.assertIsNone(result["last_success_at"])
        # Others still populated
        self.assertEqual(result["inbound_today"], 42)
        self.assertEqual(result["failed_inbound_today"], 3)
        self.assertEqual(len(result["top_failure_reasons"]), 1)
        self.assertEqual(result["stuck_orders_6h"], 5)

    def test_all_metrics_broken_still_returns_defaults_not_500(self):
        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(mod, "_last_successful_einvoice_at",
                         side_effect=Exception("boom")),
            patch.object(mod, "_count_inbound_today",
                         side_effect=Exception("boom")),
            patch.object(mod, "_count_failed_inbound_today",
                         side_effect=Exception("boom")),
            patch.object(mod, "_top_failure_reasons",
                         side_effect=Exception("boom")),
            patch.object(mod, "_count_stuck_orders_over_6h",
                         side_effect=Exception("boom")),
        ):
            # Must NOT raise
            result = mod.get_metrics()
        self.assertIsNone(result["last_success_at"])
        self.assertEqual(result["inbound_today"], 0)
        self.assertEqual(result["failed_inbound_today"], 0)
        self.assertEqual(result["top_failure_reasons"], [])
        self.assertEqual(result["stuck_orders_6h"], 0)


class TestIndividualMetricQueries(unittest.TestCase):
    """Test the SQL query wrappers return the right types + shapes.
    Actual query correctness is verified against live data on MMPL
    (unit tests only lock the plumbing)."""

    def test_last_successful_einvoice_returns_none_on_empty(self):
        with patch.object(mod.frappe.db, "sql", return_value=[{"ts": None}]):
            self.assertIsNone(mod._last_successful_einvoice_at())

    def test_last_successful_einvoice_returns_iso_string(self):
        with patch.object(
            mod.frappe.db, "sql",
            return_value=[{"ts": "2026-07-16 14:03:00"}],
        ):
            result = mod._last_successful_einvoice_at()
        self.assertEqual(result, "2026-07-16 14:03:00")

    def test_count_inbound_today_returns_int(self):
        with patch.object(mod.frappe.db, "sql", return_value=[{"n": 7}]):
            self.assertEqual(mod._count_inbound_today(), 7)

    def test_count_inbound_today_empty_returns_zero(self):
        with patch.object(mod.frappe.db, "sql", return_value=[{"n": None}]):
            self.assertEqual(mod._count_inbound_today(), 0)

    def test_count_failed_inbound_today_returns_int(self):
        with patch.object(mod.frappe.db, "sql", return_value=[{"n": 2}]):
            self.assertEqual(mod._count_failed_inbound_today(), 2)

    def test_stuck_orders_returns_int(self):
        with patch.object(mod.frappe.db, "sql", return_value=[{"n": 4}]):
            self.assertEqual(mod._count_stuck_orders_over_6h(), 4)

    def test_top_failure_reasons_returns_list_of_dicts(self):
        with (
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(mod.frappe.db, "sql", return_value=[
                {"reason": "IC timeout", "count": 5},
                {"reason": "GSTIN mismatch", "count": 3},
                {"reason": "Item Map missing", "count": 1},
            ]),
        ):
            reasons = mod._top_failure_reasons()
        self.assertEqual(len(reasons), 3)
        self.assertEqual(reasons[0]["reason"], "IC timeout")
        self.assertEqual(reasons[0]["count"], 5)
        # All counts are int
        for r in reasons:
            self.assertIsInstance(r["count"], int)

    def test_top_failure_reasons_gracefully_skips_when_doctype_missing(self):
        """Fresh install without Sync Record DocType yet — degrade to []."""
        with patch.object(mod.frappe.db, "exists", return_value=False):
            self.assertEqual(mod._top_failure_reasons(), [])

    def test_top_failure_reasons_gracefully_skips_when_column_missing(self):
        """Older schema without last_error column — degrade to []."""
        with (
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(mod.frappe.db, "has_column", return_value=False),
        ):
            self.assertEqual(mod._top_failure_reasons(), [])


class TestSafeWrapper(unittest.TestCase):
    """_safe(fn, default) — the isolation primitive."""

    def test_returns_fn_result_on_success(self):
        self.assertEqual(mod._safe(lambda: 42), 42)

    def test_returns_default_on_exception(self):
        def _boom():
            raise RuntimeError("nope")
        self.assertEqual(mod._safe(_boom, default=99), 99)

    def test_returns_none_default_when_not_specified(self):
        def _boom():
            raise RuntimeError("nope")
        self.assertIsNone(mod._safe(_boom))


if __name__ == "__main__":
    unittest.main()
