"""gh#152 — Custom GSP outbound polling circuit breaker.

Locks:
  - State machine: Closed → Open when threshold crossed
  - Open → Half-Open when cooldown expires
  - Half-Open → Closed on success (single probe worked)
  - Half-Open → Open on failure (probe still failing)
  - should_allow_poll returns True/False per state
  - Trip threshold: 3+ failures AND >50% rate
  - Counter reads/writes use frappe.cache
  - Any breaker fault degrades to "poll allowed" (defensive)
  - record_inbound_result never propagates exceptions
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.api import gsp_circuit as mod


class TestShouldAllowPollByState(unittest.TestCase):
    """The main polling-gate: does the poll proceed?"""

    def test_closed_state_allows_poll(self):
        with patch.object(mod, "_read_state", return_value=mod.STATE_CLOSED):
            self.assertTrue(mod.should_allow_poll("TEST-ACC"))

    def test_open_state_blocks_poll_when_cooldown_active(self):
        with (
            patch.object(mod, "_read_state", return_value=mod.STATE_OPEN),
            patch.object(mod, "_cooldown_expired", return_value=False),
        ):
            self.assertFalse(mod.should_allow_poll("TEST-ACC"))

    def test_open_state_transitions_to_half_open_when_cooldown_expires(self):
        transition_mock = MagicMock()
        with (
            patch.object(mod, "_read_state", return_value=mod.STATE_OPEN),
            patch.object(mod, "_cooldown_expired", return_value=True),
            patch.object(mod, "_transition_to", side_effect=transition_mock),
        ):
            allowed = mod.should_allow_poll("TEST-ACC")
        self.assertTrue(allowed)
        transition_mock.assert_called_once_with("TEST-ACC", mod.STATE_HALF_OPEN)

    def test_half_open_state_blocks_subsequent_polls(self):
        """Half-Open permits ONE probe; further polls are blocked
        until the probe outcome flips state."""
        with patch.object(
            mod, "_read_state", return_value=mod.STATE_HALF_OPEN,
        ):
            self.assertFalse(mod.should_allow_poll("TEST-ACC"))

    def test_breaker_fault_degrades_to_allowing_poll(self):
        """If _read_state itself throws (DB down, schema quirk),
        should_allow_poll returns True — breaker never blocks
        operational polling due to its own bug."""
        with patch.object(
            mod, "_read_state", side_effect=RuntimeError("DB down"),
        ):
            self.assertTrue(mod.should_allow_poll("TEST-ACC"))


class TestRecordInboundResultTransitions(unittest.TestCase):
    """State machine transitions triggered by inbound outcomes."""

    def _run(self, *, current_state, success):
        """Invoke record_inbound_result with the current state stubbed."""
        transitions = []

        def _capture_transition(acc, new_state):
            transitions.append(new_state)

        with (
            patch.object(mod, "_increment_counter"),
            patch.object(mod, "_read_state", return_value=current_state),
            patch.object(
                mod, "_transition_to", side_effect=_capture_transition,
            ),
            patch.object(mod, "_should_trip", return_value=False),
            patch.object(mod, "_reset_counters"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.record_inbound_result("TEST-ACC", success=success)
        return transitions

    def test_half_open_success_closes_circuit(self):
        transitions = self._run(
            current_state=mod.STATE_HALF_OPEN, success=True,
        )
        self.assertEqual(transitions, [mod.STATE_CLOSED])

    def test_half_open_failure_reopens_circuit(self):
        transitions = self._run(
            current_state=mod.STATE_HALF_OPEN, success=False,
        )
        self.assertEqual(transitions, [mod.STATE_OPEN])

    def test_closed_success_does_not_transition(self):
        transitions = self._run(
            current_state=mod.STATE_CLOSED, success=True,
        )
        self.assertEqual(transitions, [])

    def test_closed_failure_transitions_to_open_when_should_trip(self):
        with (
            patch.object(mod, "_increment_counter"),
            patch.object(mod, "_read_state", return_value=mod.STATE_CLOSED),
            patch.object(mod, "_should_trip", return_value=True),
            patch.object(mod, "_transition_to") as tt,
            patch.object(mod, "_reset_counters"),
            patch.object(mod.frappe, "log_error"),
        ):
            mod.record_inbound_result("TEST-ACC", success=False)
        tt.assert_called_once_with("TEST-ACC", mod.STATE_OPEN)

    def test_closed_failure_does_not_transition_when_below_threshold(self):
        with (
            patch.object(mod, "_increment_counter"),
            patch.object(mod, "_read_state", return_value=mod.STATE_CLOSED),
            patch.object(mod, "_should_trip", return_value=False),
            patch.object(mod, "_transition_to") as tt,
        ):
            mod.record_inbound_result("TEST-ACC", success=False)
        tt.assert_not_called()

    def test_never_raises_on_internal_exception(self):
        """record_inbound_result must never propagate — the inbound
        handler relies on this being safe."""
        with patch.object(
            mod, "_increment_counter",
            side_effect=RuntimeError("cache down"),
        ):
            # Must NOT raise
            mod.record_inbound_result("TEST-ACC", success=True)


class TestShouldTripThreshold(unittest.TestCase):
    """The trip decision: 3+ failures AND >50% failure rate."""

    def _run(self, *, success_count, failure_count):
        def _read(key):
            return failure_count if "failure" in key else success_count
        with patch.object(mod, "_read_counter", side_effect=_read):
            return mod._should_trip("TEST-ACC")

    def test_below_minimum_failures_does_not_trip(self):
        """2 failures = below FAILURE_MIN_ABSOLUTE=3 → no trip."""
        self.assertFalse(self._run(success_count=0, failure_count=2))

    def test_low_rate_does_not_trip(self):
        """3 failures out of 30 total = 10% rate → below threshold → no trip."""
        self.assertFalse(self._run(success_count=27, failure_count=3))

    def test_high_rate_and_min_failures_trips(self):
        """3 failures out of 5 total = 60% rate → trips."""
        self.assertTrue(self._run(success_count=2, failure_count=3))

    def test_all_failures_trips(self):
        """3 failures, 0 success = 100% rate → trips."""
        self.assertTrue(self._run(success_count=0, failure_count=3))

    def test_exactly_50_percent_does_not_trip(self):
        """5 failures out of 10 total = 50% rate → NOT >50% → no trip.
        Boundary must be strict."""
        self.assertFalse(self._run(success_count=5, failure_count=5))

    def test_zero_total_does_not_trip(self):
        """No events at all → no trip (defensive divide-by-zero guard)."""
        self.assertFalse(self._run(success_count=0, failure_count=0))


class TestCounterHelpers(unittest.TestCase):
    """Redis-backed counters via frappe.cache."""

    def test_read_counter_returns_zero_when_absent(self):
        fake_cache = MagicMock()
        fake_cache.get_value = MagicMock(return_value=None)
        with patch.object(mod.frappe, "cache", return_value=fake_cache):
            self.assertEqual(mod._read_counter("some-key"), 0)

    def test_read_counter_returns_int(self):
        fake_cache = MagicMock()
        fake_cache.get_value = MagicMock(return_value=7)
        with patch.object(mod.frappe, "cache", return_value=fake_cache):
            self.assertEqual(mod._read_counter("some-key"), 7)

    def test_read_counter_returns_zero_on_bad_type(self):
        """Cache stringification (rare) shouldn't crash."""
        fake_cache = MagicMock()
        fake_cache.get_value = MagicMock(return_value="not-an-int")
        with patch.object(mod.frappe, "cache", return_value=fake_cache):
            self.assertEqual(mod._read_counter("some-key"), 0)

    def test_increment_counter_writes_with_ttl(self):
        fake_cache = MagicMock()
        fake_cache.get_value = MagicMock(return_value=3)
        fake_cache.set_value = MagicMock()
        with patch.object(mod.frappe, "cache", return_value=fake_cache):
            mod._increment_counter("TEST-ACC", success=False)
        fake_cache.set_value.assert_called_once()
        # Signature: set_value(key, value, expires_in_sec=WINDOW_SECONDS)
        call = fake_cache.set_value.call_args
        self.assertEqual(call.args[0], mod._failure_key("TEST-ACC"))
        self.assertEqual(call.args[1], 4)  # was 3, now +1
        self.assertEqual(call.kwargs["expires_in_sec"], mod.WINDOW_SECONDS)


class TestCooldownExpiry(unittest.TestCase):
    """Open → Half-Open time gate."""

    def test_no_opened_at_treats_as_expired(self):
        """Missing timestamp → treat as expired → permits probe.
        (Defensive: don't leave circuit stuck Open forever due to a
        missing field.)"""
        with patch.object(
            mod.frappe.db, "get_value", return_value=None,
        ):
            self.assertTrue(mod._cooldown_expired("TEST-ACC"))
