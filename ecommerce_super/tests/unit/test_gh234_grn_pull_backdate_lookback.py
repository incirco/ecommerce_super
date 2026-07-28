"""gh#234: GRN pull rolling-window look-back on top of the forward
watermark, so back-dated GRNs (grn_created_at earlier than the
watermark) aren't silently skipped.

Locks the helper `_apply_backdate_lookback` and its integration into
`pull_grns_for_location`. The helper produces the effective
`created_after` sent to EE's `/Grn/V2/getGrnDetails`:

  effective_cutoff = min(watermark, now - N days)

where N is `EasyEcom Account.grn_pull_backdate_window_days` (default 7).

The four scenarios locked below:
  1. N > 0 AND watermark > (now - N days) → widen to (now - N days)
  2. N > 0 AND watermark <= (now - N days) → keep watermark
  3. N == 0 (feature disabled) → keep watermark verbatim
  4. Watermark NULL (bootstrap) → return None (EE 7-day backstop path)

Plus:
  - Any DB exception during the flag read falls back to watermark
    (defensive; never let this shim block the normal pull path)
  - When called from `pull_grns_for_location` with a manual
    `created_after` override, the look-back is NOT applied (respects
    explicit overrides used by tests + FDE replay tooling)

REGRESSION CONTEXT

Original bug (reported by @garv999 2026-07-28): the forward-only
`created_after = grn_pull_high_watermark` delta pull skips GRNs whose
`grn_created_at` is EARLIER than the current watermark. Same-date
GRNs pull fine; back-dated GRNs (dated a day or two in the past) do
not. Silent data loss for any inward recorded with a past date.

Verified against MMPL live: watermark was `2026-07-28 09:50:24`
today — any GRN with `grn_created_at < that` landing after would
be permanently skipped.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows import grn_pull as mod


# ============================================================
# The core helper: _apply_backdate_lookback
# ============================================================


class TestApplyBackdateLookback(unittest.TestCase):

    def _run(self, *, watermark, window_days, mocked_now=None):
        """Apply the shim with a fixed 'now' anchor for determinism."""
        now_anchor = mocked_now or datetime(2026, 7, 28, 12, 0, 0)

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            field = (
                kwargs.get("field")
                or kwargs.get("fieldname")
                or (args[2] if len(args) > 2 else None)
            )
            if doctype == "EasyEcom Account" and field == "grn_pull_backdate_window_days":
                return window_days
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod, "now_datetime", return_value=now_anchor),
        ):
            return mod._apply_backdate_lookback(
                watermark, account_name="Test Account",
            )

    def test_watermark_recent_within_window_gets_widened(self):
        """Watermark 2 days ago, window 7 days → return (now - 7 days),
        widening the cutoff so the previous 7 days get re-scanned."""
        # now = 2026-07-28 12:00, watermark 2 days ago = 2026-07-26 12:00
        # window = 7 days, so (now - 7 days) = 2026-07-21 12:00
        # Expected: (now - 7 days) is EARLIER than watermark, so widen
        watermark = datetime(2026, 7, 26, 12, 0, 0)
        result = self._run(watermark=watermark, window_days=7)
        self.assertEqual(result, datetime(2026, 7, 21, 12, 0, 0))
        # Prove we widened, didn't just echo watermark
        self.assertLess(result, watermark)

    def test_watermark_already_earlier_than_window_kept_as_is(self):
        """Watermark 10 days ago, window 7 days → keep watermark
        (already older than the window cutoff, no widening needed)."""
        watermark = datetime(2026, 7, 18, 12, 0, 0)  # 10 days ago
        result = self._run(watermark=watermark, window_days=7)
        self.assertEqual(result, watermark)

    def test_window_zero_disables_lookback(self):
        """N=0 (opt-out) → return watermark verbatim, no widening.
        Restores strict forward-only behavior for accounts that want it."""
        watermark = datetime(2026, 7, 26, 12, 0, 0)
        result = self._run(watermark=watermark, window_days=0)
        self.assertEqual(result, watermark)

    def test_window_none_treated_as_disabled(self):
        """Legacy accounts without the field populated → treat as
        N=0. Guards against sites where the field hasn't loaded yet."""
        watermark = datetime(2026, 7, 26, 12, 0, 0)
        result = self._run(watermark=watermark, window_days=None)
        self.assertEqual(result, watermark)

    def test_watermark_null_returns_null(self):
        """Bootstrap path (NULL watermark): return None unchanged so
        EE's default 7-day backstop kicks in. Look-back irrelevant."""
        result = self._run(watermark=None, window_days=7)
        self.assertIsNone(result)

    def test_watermark_empty_string_returns_empty(self):
        """Same as NULL — falsy watermark short-circuits without
        touching the flag."""
        result = self._run(watermark="", window_days=7)
        self.assertEqual(result, "")

    def test_db_exception_falls_back_to_watermark(self):
        """Defensive: if the flag lookup raises (e.g. field not yet
        migrated on a site), never crash the pull — return watermark
        as-is (current forward-only behavior)."""
        watermark = datetime(2026, 7, 26, 12, 0, 0)
        with (
            patch.object(mod.frappe.db, "get_value", side_effect=RuntimeError("boom")),
        ):
            result = mod._apply_backdate_lookback(
                watermark, account_name="Test Account",
            )
        self.assertEqual(result, watermark)

    def test_negative_window_treated_as_disabled(self):
        """Data corruption guard: N<0 → treat as disabled (0). Never
        widen the future — that would be nonsensical."""
        watermark = datetime(2026, 7, 26, 12, 0, 0)
        result = self._run(watermark=watermark, window_days=-5)
        self.assertEqual(result, watermark)

    def test_window_widens_by_exactly_n_days(self):
        """Precision guard: N=14 with a recent watermark → cutoff is
        exactly (now - 14 days), not (now - 13) or (now - 15)."""
        watermark = datetime(2026, 7, 27, 12, 0, 0)  # 1 day ago
        result = self._run(
            watermark=watermark, window_days=14,
        )
        expected = datetime(2026, 7, 14, 12, 0, 0)  # 14 days from now
        self.assertEqual(result, expected)


# ============================================================
# Integration: pull_grns_for_location honors the widened cutoff
# ============================================================


class TestPullGrnsIntegration(unittest.TestCase):
    """Ensure pull_grns_for_location passes the widened cutoff (not
    the raw watermark) to EE's getGrnDetails."""

    def _run_pull(self, *, watermark, window_days, explicit_override=None):
        """Fake a one-page sweep and return the `created_after` that
        was passed to the EE client."""
        captured_params = {}

        fake_client = MagicMock()
        def _fake_get(url, params=None):
            captured_params["url"] = url
            captured_params["params"] = params
            return {"data": [], "nextUrl": None}
        fake_client.get.side_effect = _fake_get

        def _get_value(*args, **kwargs):
            """Tolerant get_value stub — Frappe calls this with a mix
            of positional and keyword arg patterns depending on caller.
            We only care about the specific EasyEcom Account field
            lookups for this test."""
            # Normalize: doctype, filters (or name), field (fieldname)
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            field = (
                kwargs.get("field")
                or kwargs.get("fieldname")
                or (args[2] if len(args) > 2 else None)
            )
            if doctype == "EasyEcom Account":
                if field == "grn_pull_high_watermark":
                    return watermark
                if field == "grn_pull_backdate_window_days":
                    return window_days
            return None

        now_anchor = datetime(2026, 7, 28, 12, 0, 0)

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod, "now_datetime", return_value=now_anchor),
        ):
            mod.pull_grns_for_location(
                location_key="LOC-001",
                account_name="Test Account",
                client=fake_client,
                max_pages=1,
                created_after=explicit_override,
            )
        return captured_params.get("params", {})

    def test_pull_uses_widened_cutoff_when_watermark_recent(self):
        """Recent watermark + N=7 → EE sees (now - 7 days) as
        created_after, not the raw watermark."""
        params = self._run_pull(
            watermark=datetime(2026, 7, 26, 12, 0, 0),  # 2 days ago
            window_days=7,
        )
        # (now - 7 days) = 2026-07-21 12:00:00, formatted for EE
        self.assertEqual(params["created_after"], "2026-07-21 12:00:00")

    def test_pull_uses_raw_watermark_when_watermark_already_old(self):
        """Watermark older than the window → EE sees the watermark
        (no need to widen)."""
        params = self._run_pull(
            watermark=datetime(2026, 7, 18, 12, 0, 0),  # 10 days ago
            window_days=7,
        )
        self.assertEqual(params["created_after"], "2026-07-18 12:00:00")

    def test_explicit_override_is_used_verbatim(self):
        """When caller passes explicit `created_after` (tests / FDE
        replay), we honor it exactly — do NOT apply the widening
        shim, or the override would be surprising."""
        params = self._run_pull(
            watermark=datetime(2026, 7, 26, 12, 0, 0),
            window_days=7,
            explicit_override="2026-06-01 00:00:00",
        )
        self.assertEqual(params["created_after"], "2026-06-01 00:00:00")

    def test_pull_with_disabled_window_uses_watermark_as_is(self):
        """N=0 opt-out → back to strict forward-only behavior."""
        params = self._run_pull(
            watermark=datetime(2026, 7, 26, 12, 0, 0),
            window_days=0,
        )
        self.assertEqual(params["created_after"], "2026-07-26 12:00:00")


if __name__ == "__main__":
    unittest.main()
