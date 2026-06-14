"""§11 Phase 1 — Date formatter tests.

Two formatters:
  - format_utc_datetime(date, time=None) → orderDate (UTC)
  - format_ist_date(date)                → expDeliveryDate (IST midnight)

Both return "YYYY-MM-DD HH:MM:SS" strings without timezone markers
(EE's field-level semantic carries the timezone).

Key correctness assertion: midnight IST → 18:30:00 UTC the *previous*
day (IST is UTC+5:30). This is the single-arg call's exact behavior;
the §11 packet's pre-Stage-1 ruling was to drop the time-of-day
component (SO has no transaction_time), so this is the production
path.
"""

from __future__ import annotations

import unittest
from datetime import date, time

from ecommerce_super.easyecom.flows.b2b_sales.date_format import (
    format_ist_date,
    format_utc_datetime,
)


class TestFormatUtcDatetime(unittest.TestCase):
    def test_single_arg_midnight_ist_converts_to_previous_day_1830_utc(
        self,
    ) -> None:
        """The headline case: SO.transaction_date = 2026-06-14, no
        time-of-day → midnight IST → 2026-06-13 18:30:00 UTC."""
        result = format_utc_datetime(date(2026, 6, 14))
        self.assertEqual(result, "2026-06-13 18:30:00")

    def test_explicit_time_part_preserved(self) -> None:
        """When a time is passed (forward-compat for a future
        transaction_time Custom Field), it's used as-is in IST then
        converted."""
        from datetime import datetime as dt

        # 09:00:00 IST on 2026-06-14 → 03:30:00 UTC same day.
        result = format_utc_datetime(
            date(2026, 6, 14), dt(2026, 6, 14, 9, 0, 0)
        )
        self.assertEqual(result, "2026-06-14 03:30:00")

    def test_year_boundary_dec_31_midnight_ist_wraps_to_dec_30_utc(
        self,
    ) -> None:
        """Edge case: Dec 31 midnight IST → Dec 30 18:30 UTC. Year
        rollover happens in IST much earlier than in UTC; the
        formatter must respect that."""
        result = format_utc_datetime(date(2027, 1, 1))
        self.assertEqual(result, "2026-12-31 18:30:00")

    def test_returns_string(self) -> None:
        """Type contract — EE expects a string, not a datetime."""
        result = format_utc_datetime(date(2026, 6, 14))
        self.assertIsInstance(result, str)


class TestFormatIstDate(unittest.TestCase):
    def test_returns_midnight_ist_no_timezone_marker(self) -> None:
        """SO.delivery_date = 2026-06-20 → "2026-06-20 00:00:00".
        IST is implicit in the field-level semantic; the string
        carries no timezone marker."""
        result = format_ist_date(date(2026, 6, 20))
        self.assertEqual(result, "2026-06-20 00:00:00")

    def test_no_wrap_for_ist_midnight(self) -> None:
        """Unlike orderDate, expDeliveryDate stays in IST so Dec 31
        in IST is Dec 31 in the output — no UTC-conversion wrap."""
        result = format_ist_date(date(2027, 1, 1))
        self.assertEqual(result, "2027-01-01 00:00:00")


if __name__ == "__main__":
    unittest.main()
