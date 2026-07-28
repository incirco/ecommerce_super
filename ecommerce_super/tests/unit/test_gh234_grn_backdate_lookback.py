"""gh#234 — GRN back-date look-back window.

`grn_pull._created_after_with_lookback` widens the pull's `created_after`
back to at least ``now - GRN_BACKDATE_LOOKBACK_DAYS`` so back-dated GRNs
(whose EE `grn_created_at` lands behind the advanced high watermark) are
re-fetched instead of silently missed. Already-Receipted GRNs short-
circuit as no-ops on re-scan, so the wider window is idempotent.

These lock the window math and its safety invariants:
  - back-dated GRNs within the window are caught (the fix)
  - the window may only widen backwards, never narrow (no covered GRN skipped)
  - NULL watermark (first pull) is preserved unchanged
  - the look-back is tunable / disable-able
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ecommerce_super.easyecom.flows import grn_pull as mod

_NOW = datetime(2026, 7, 28, 12, 0, 0)


def _d(days: int) -> datetime:
    return _NOW - timedelta(days=days)


class TestGrnBackdateLookback(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(mod, "now_datetime", return_value=_NOW)
        p.start()
        self.addCleanup(p.stop)

    def test_caught_up_watermark_rolls_back_to_window(self) -> None:
        # watermark == now → created_after becomes now - N days
        self.assertEqual(
            mod._created_after_with_lookback(_d(0)),
            _d(mod.GRN_BACKDATE_LOOKBACK_DAYS),
        )

    def test_recent_watermark_widens_back_to_window(self) -> None:
        # The fix: a 2-day-old watermark still looks back the full window,
        # so a GRN back-dated up to N days is re-fetched.
        self.assertEqual(
            mod._created_after_with_lookback(_d(2)),
            _d(mod.GRN_BACKDATE_LOOKBACK_DAYS),
        )

    def test_old_watermark_is_not_narrowed(self) -> None:
        # watermark older than the window → returned unchanged; the window
        # is never made SMALLER than the plain delta would have used.
        self.assertEqual(mod._created_after_with_lookback(_d(30)), _d(30))

    def test_first_pull_none_watermark_unchanged(self) -> None:
        # NULL watermark → preserve the existing full / backstop cold-start
        # behaviour (the deferred-cron safety gate depends on this).
        self.assertIsNone(mod._created_after_with_lookback(None))

    def test_lookback_zero_disables(self) -> None:
        self.assertEqual(
            mod._created_after_with_lookback(_d(0), lookback_days=0), _d(0)
        )

    def test_string_watermark_is_parsed(self) -> None:
        self.assertEqual(
            mod._created_after_with_lookback(
                _NOW.strftime("%Y-%m-%d %H:%M:%S")
            ),
            _d(mod.GRN_BACKDATE_LOOKBACK_DAYS),
        )

    def test_result_never_later_than_watermark_invariant(self) -> None:
        # The window may only widen backwards, never narrow — so no GRN the
        # plain delta would have covered is ever skipped.
        for days in range(0, 41):
            wm = _d(days)
            self.assertLessEqual(mod._created_after_with_lookback(wm), wm)


if __name__ == "__main__":
    unittest.main()
