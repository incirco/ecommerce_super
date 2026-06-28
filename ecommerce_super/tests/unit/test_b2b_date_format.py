"""§11 Phase 1 — Date formatter tests.

Two formatters (post-2026-06-28: both IST):
  - format_ist_datetime(date, time=None) → orderDate (IST)
  - format_ist_date(date)                → expDeliveryDate (IST midnight)

Both return "YYYY-MM-DD HH:MM:SS" strings without timezone markers
(EE accepts IST and displays IST — wire format matches UI display).

`format_utc_datetime` is retained as a deprecated alias pointing at
`format_ist_datetime`. New code should use the accurate name.

Live-verified against Harmony 2026-06-28: EE accepts IST cleanly,
displays it back as the same string. No timezone shift visible in
the EE UI when the wire format matches IST.
"""

from __future__ import annotations

import unittest
from datetime import date

from ecommerce_super.easyecom.flows.b2b_sales.date_format import (
    format_ist_date,
    format_ist_datetime,
    format_utc_datetime,
)


class TestFormatIstDatetime(unittest.TestCase):
    """orderDate formatter — IST midnight, string match between wire
    and EE UI display."""

    def test_single_arg_midnight_ist_with_explicit_offset(self) -> None:
        """The headline case: SO.transaction_date = 2026-06-14, no
        time-of-day → "2026-06-14 00:00:00+05:30". The trailing
        +05:30 is required for EE to honor IST (verified live
        2026-06-28); without it, EE treats input as UTC and adds
        +5:30 shift visible in display."""
        result = format_ist_datetime(date(2026, 6, 14))
        self.assertEqual(result, "2026-06-14 00:00:00+05:30")

    def test_explicit_time_part_preserved_in_ist_with_offset(self) -> None:
        """When a time is passed (forward-compat for a future
        transaction_time Custom Field), it's used as-is in IST. Same
        +05:30 offset suffix."""
        from datetime import datetime as dt

        result = format_ist_datetime(
            date(2026, 6, 14), dt(2026, 6, 14, 9, 0, 0)
        )
        self.assertEqual(result, "2026-06-14 09:00:00+05:30")

    def test_year_boundary_dec_31_midnight_ist(self) -> None:
        """Edge case: Dec 31 midnight IST stays Dec 31 — no
        year-rollover surprise."""
        result = format_ist_datetime(date(2026, 12, 31))
        self.assertEqual(result, "2026-12-31 00:00:00+05:30")

    def test_returns_string(self) -> None:
        """Type contract — EE expects a string, not a datetime."""
        result = format_ist_datetime(date(2026, 6, 14))
        self.assertIsInstance(result, str)

    def test_always_emits_plus_0530_suffix(self) -> None:
        """IST is fixed (no daylight saving) so the offset is
        always +05:30 — sanity guard against any timezone drift."""
        result = format_ist_datetime(date(2026, 6, 14))
        self.assertTrue(result.endswith("+05:30"))


class TestFormatUtcDatetimeIsDeprecatedAlias(unittest.TestCase):
    """The old name is preserved as an alias for backwards-compat but
    must produce identical output to the IST formatter."""

    def test_alias_produces_same_output(self) -> None:
        """format_utc_datetime is now an alias for format_ist_datetime
        — same input must yield same string."""
        a = format_ist_datetime(date(2026, 6, 14))
        b = format_utc_datetime(date(2026, 6, 14))
        self.assertEqual(a, b)
        self.assertEqual(b, "2026-06-14 00:00:00+05:30")


class TestFormatIstDate(unittest.TestCase):
    def test_returns_midnight_ist_no_timezone_marker(self) -> None:
        """SO.delivery_date = 2026-06-20 → "2026-06-20 00:00:00".
        IST is implicit in the field-level semantic; the string
        carries no timezone marker."""
        result = format_ist_date(date(2026, 6, 20))
        self.assertEqual(result, "2026-06-20 00:00:00")

    def test_no_wrap_for_ist_midnight(self) -> None:
        """expDeliveryDate stays in IST so Dec 31 in IST is Dec 31 in
        the output. (Also true now for orderDate post-2026-06-28.)"""
        result = format_ist_date(date(2027, 1, 1))
        self.assertEqual(result, "2027-01-01 00:00:00")


if __name__ == "__main__":
    unittest.main()
