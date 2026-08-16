"""Unit tests for PR D — multi-currency handling for foreign B2C invoices.

Covers `_resolve_currency_conversion_rate`, the helper that resolves
FX rates for foreign-currency exports (SGD / CAD / NPR / etc.):

  1. Same-currency short-circuit (INR → INR = 1.0, no DB call)
  2. Successful ERPNext Currency Exchange lookup
  3. Missing rate raises B2CBuilderError (no silent 1.0 fallback)
  4. Zero rate treated the same as missing (defensive)

Real-money constraint: the helper must never silently succeed with a
rate of 1.0 or 0 for a non-INR invoice, because that would land wildly
wrong INR base amounts on the ledger. Fail loud, ops fixes, re-poll.
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
    B2CBuilderError,
    _resolve_currency_conversion_rate,
)


class TestSameCurrencyShortCircuit(unittest.TestCase):

    def test_inr_to_inr_returns_one_without_lookup(self):
        """The common domestic case — no DB call, returns 1.0 flat."""
        # No mock needed; short-circuit runs before any import
        self.assertEqual(
            _resolve_currency_conversion_rate(
                from_currency="INR",
                to_currency="INR",
                transaction_date=date(2026, 8, 17),
            ),
            1.0,
        )

    def test_matching_foreign_currency_short_circuits(self):
        """CAD → CAD should also short-circuit (e.g., company reporting
        currency happens to match invoice currency for a hypothetical
        Canadian entity). Defensive — this case doesn't happen at
        Puresta today, but the helper shouldn't error."""
        self.assertEqual(
            _resolve_currency_conversion_rate(
                from_currency="CAD",
                to_currency="CAD",
                transaction_date=date(2026, 8, 17),
            ),
            1.0,
        )


class TestForeignCurrencyLookup(unittest.TestCase):

    @patch("erpnext.setup.utils.get_exchange_rate", return_value=68.21)
    def test_cad_to_inr_returns_rate_from_erpnext(self, mock_ex):
        """Happy path — ERPNext resolves the rate and we return it."""
        result = _resolve_currency_conversion_rate(
            from_currency="CAD",
            to_currency="INR",
            transaction_date=date(2026, 7, 31),
        )
        self.assertEqual(result, 68.21)
        # Confirm we passed the right args through
        mock_ex.assert_called_once_with(
            from_currency="CAD",
            to_currency="INR",
            transaction_date=date(2026, 7, 31),
        )

    @patch("erpnext.setup.utils.get_exchange_rate", return_value=74.19)
    def test_sgd_to_inr_returns_rate(self, _mock):
        """SGD case — matches the exact rate the Aug 2026 Puresta
        reconciliation exercise identified for BHR52627-417."""
        self.assertEqual(
            _resolve_currency_conversion_rate(
                from_currency="SGD",
                to_currency="INR",
                transaction_date=date(2026, 7, 31),
            ),
            74.19,
        )

    @patch("erpnext.setup.utils.get_exchange_rate", return_value=0.63)
    def test_npr_to_inr_returns_sub_unity_rate(self, _mock):
        """NPR is worth less than INR — 1 NPR ≈ 0.63 INR. The helper
        must not treat 'rate < 1' as invalid; only 'rate <= 0' is."""
        self.assertEqual(
            _resolve_currency_conversion_rate(
                from_currency="NPR",
                to_currency="INR",
                transaction_date=date(2026, 7, 31),
            ),
            0.63,
        )


class TestMissingRateRaises(unittest.TestCase):

    @patch("erpnext.setup.utils.get_exchange_rate", return_value=0)
    def test_zero_rate_raises_b2c_builder_error(self, _mock):
        """A rate of 0 would divide-by-zero downstream — refuse.
        This is the real-world 'Currency Exchange row missing' case."""
        with self.assertRaises(B2CBuilderError) as ctx:
            _resolve_currency_conversion_rate(
                from_currency="CAD",
                to_currency="INR",
                transaction_date=date(2026, 8, 17),
            )
        msg = str(ctx.exception)
        # Error message must be actionable — name the currencies + date
        # + point to the fix (Currency Exchange DocType).
        self.assertIn("CAD", msg)
        self.assertIn("INR", msg)
        self.assertIn("Currency Exchange", msg)

    @patch("erpnext.setup.utils.get_exchange_rate", return_value=None)
    def test_none_rate_raises_b2c_builder_error(self, _mock):
        """Some ERPNext versions return None instead of 0."""
        with self.assertRaises(B2CBuilderError):
            _resolve_currency_conversion_rate(
                from_currency="SGD",
                to_currency="INR",
                transaction_date=date(2026, 8, 17),
            )

    @patch("erpnext.setup.utils.get_exchange_rate", return_value=-0.5)
    def test_negative_rate_raises_b2c_builder_error(self, _mock):
        """Defensive — negative rate is nonsense, treat as missing."""
        with self.assertRaises(B2CBuilderError):
            _resolve_currency_conversion_rate(
                from_currency="EUR",
                to_currency="INR",
                transaction_date=date(2026, 8, 17),
            )


if __name__ == "__main__":
    unittest.main()
