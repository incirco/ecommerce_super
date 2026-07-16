"""§11 gh#152 — Custom GSP outbound-polling circuit breaker.

Purpose: when our inbound /einvoice/update is 5xxing for hours (bad
deploy, IC down, DB issue), the outbound polling loop keeps hitting
EE for the same orders — amplifying pressure on both sides. This
circuit breaker halts outbound polling per-account when inbound
success drops below threshold, then probes carefully to recover.

State machine (per EasyEcom Account):
  - Closed (default): polling runs normally
  - Open: polling paused for OPEN_DURATION_SECONDS
  - Half-Open: after Open expires, allow ONE poll; success → Closed;
    failure → back to Open

Storage:
  - State: EasyEcom Account.ecs_gsp_circuit_state + circuit_opened_at
    (DB-persisted so state survives restarts)
  - Rolling counters: Redis via frappe.cache() (WINDOW_SECONDS TTL)
    so counter noise doesn't persist across window boundaries

Trip conditions (tuned from initial gh#152 spec; adjust from live data):
  - 3+ failures AND >50% failure rate in the last WINDOW_SECONDS

Where hooked:
  - `should_allow_poll(account)` called from polling entry (polling.py)
  - `record_inbound_result(account, success)` called from inbound
    handler paths (success/failure)

Failure modes are defensive throughout — cache misses, transient
DB failures, missing fields all default to "circuit closed / poll
proceeds" so the breaker never becomes an operational blocker itself.
"""
from __future__ import annotations

from typing import Literal

import frappe
from frappe.utils import add_to_date, now_datetime


# --- Tunable thresholds (adjust from live data) ---

WINDOW_SECONDS = 600            # 10 min rolling window for the failure rate
OPEN_DURATION_SECONDS = 900     # 15 min pause when tripped
FAILURE_MIN_ABSOLUTE = 3        # need at least this many failures to trip
FAILURE_RATE_THRESHOLD = 0.50   # AND >50% failure rate

STATE_CLOSED = "Closed"
STATE_OPEN = "Open"
STATE_HALF_OPEN = "Half-Open"

CircuitState = Literal["Closed", "Open", "Half-Open"]


# --- Cache keys ---


def _success_key(account: str) -> str:
    return f"ecs_gsp_inbound_success:{account}"


def _failure_key(account: str) -> str:
    return f"ecs_gsp_inbound_failure:{account}"


# --- Public API ---


def should_allow_poll(account: str) -> bool:
    """Called before each per-account polling tick. Returns True when
    the circuit permits the outbound call; False when we should skip.

    Half-Open state permits exactly one probe per Open cooldown
    expiry — the FIRST caller in Half-Open gets True; subsequent
    Half-Open callers (before the probe returns) get False.

    Defensively returns True on any error — the breaker should never
    itself block operational polling due to a bug in the breaker."""
    try:
        state = _read_state(account)
    except Exception:  # noqa: BLE001
        return True  # never block polling due to breaker fault

    if state == STATE_CLOSED:
        return True

    if state == STATE_OPEN:
        # If cooldown expired, transition to Half-Open and permit probe
        if _cooldown_expired(account):
            _transition_to(account, STATE_HALF_OPEN)
            return True
        return False

    if state == STATE_HALF_OPEN:
        # Probe already in flight — don't allow another until it lands
        return False

    return True


def record_inbound_result(account: str, *, success: bool) -> None:
    """Called from the inbound /einvoice/update handler at completion.
    Increments the rolling counters and, when threshold crossed,
    transitions the circuit.

    Also called from the polling side to record the outcome of a
    Half-Open probe — success closes the circuit, failure re-opens.

    Never raises — the outer call site relies on this being safe."""
    try:
        _increment_counter(account, success)
        current = _read_state(account)

        if success and current == STATE_HALF_OPEN:
            _transition_to(account, STATE_CLOSED)
            _reset_counters(account)
            return

        if not success and current == STATE_HALF_OPEN:
            _transition_to(account, STATE_OPEN)
            return

        if current == STATE_CLOSED and _should_trip(account):
            _transition_to(account, STATE_OPEN)
            frappe.log_error(
                title=(
                    f"gh#152: Custom GSP circuit OPENED for account "
                    f"{account!r}"
                ),
                message=(
                    f"Inbound failure rate exceeded threshold "
                    f"({FAILURE_RATE_THRESHOLD * 100:.0f}%) with at "
                    f"least {FAILURE_MIN_ABSOLUTE} failures in the "
                    f"last {WINDOW_SECONDS}s. Outbound polling paused "
                    f"for {OPEN_DURATION_SECONDS}s."
                ),
            )
    except Exception:  # noqa: BLE001
        # Never propagate a breaker fault back to the caller
        return


# --- State machine internals ---


def _read_state(account: str) -> str:
    """Load the current circuit state from the Account row. Cached
    lookups would be nicer but a DB roundtrip per polling tick is
    fine (polling is per-account, not per-order)."""
    if not frappe.db.exists("EasyEcom Account", account):
        return STATE_CLOSED  # unknown account → default open path
    state = frappe.db.get_value(
        "EasyEcom Account", account, "ecs_gsp_circuit_state"
    )
    return state or STATE_CLOSED


def _transition_to(account: str, new_state: str) -> None:
    """Persist a state transition to the Account row. Also stamps
    circuit_opened_at when transitioning INTO Open."""
    updates = {"ecs_gsp_circuit_state": new_state}
    if new_state == STATE_OPEN:
        updates["ecs_gsp_circuit_opened_at"] = now_datetime()
    elif new_state == STATE_CLOSED:
        updates["ecs_gsp_circuit_opened_at"] = None
    frappe.db.set_value(
        "EasyEcom Account", account, updates, update_modified=False,
    )
    frappe.db.commit()


def _cooldown_expired(account: str) -> bool:
    """True when OPEN_DURATION_SECONDS has elapsed since the last
    Open transition."""
    opened_at = frappe.db.get_value(
        "EasyEcom Account", account, "ecs_gsp_circuit_opened_at"
    )
    if not opened_at:
        return True  # no timestamp → treat as expired (permits probe)
    expiry = add_to_date(opened_at, seconds=OPEN_DURATION_SECONDS)
    return now_datetime() >= expiry


def _should_trip(account: str) -> bool:
    """Read the rolling counters and decide whether to trip."""
    success = _read_counter(_success_key(account))
    failure = _read_counter(_failure_key(account))
    total = success + failure
    if failure < FAILURE_MIN_ABSOLUTE:
        return False
    if total == 0:
        return False
    return (failure / total) > FAILURE_RATE_THRESHOLD


# --- Counter helpers (Redis via frappe.cache) ---


def _increment_counter(account: str, success: bool) -> None:
    key = _success_key(account) if success else _failure_key(account)
    cache = frappe.cache()
    current = _read_counter(key)
    cache.set_value(key, current + 1, expires_in_sec=WINDOW_SECONDS)


def _read_counter(key: str) -> int:
    val = frappe.cache().get_value(key)
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _reset_counters(account: str) -> None:
    """Clear the rolling counters — called on Half-Open → Closed
    transition so the fresh Closed state starts with a clean window."""
    frappe.cache().delete_value(_success_key(account))
    frappe.cache().delete_value(_failure_key(account))
