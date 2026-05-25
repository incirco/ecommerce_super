"""Tier-aware rate limiting and daily-quota tracking.

SPEC §3.10 — EasyEcom rate-limits per X-API-KEY by tier:

| Tier    | Request rate | Burst | Daily quota |
|---------|--------------|-------|-------------|
| Default | 5/s          | 10    | 500         |
| Bronze  | 5/s          | 10    | 50,000      |
| Silver  | 20/s         | 40    | 200,000     |
| Gold    | 30/s         | 60    | 300,000     |
| Diamond | 30/s         | 60    | 500,000     |

The client throttles outbound calls to the configured tier's request_rate
and tracks consumption against the tier's daily_quota. As consumption
approaches the quota the client slows non-urgent work; the dashboard
(§3.9) surfaces consumption.

Implementation: a per-(account, location_key) token bucket backed by
frappe.cache(). The bucket refills at request_rate tokens/second up to
burst capacity. Acquiring a token may sleep briefly to spread bursty
callers; on quota exhaustion the call is deferred (raises
EasyEcomRateLimitError to trigger the queue's transient-retry path).

Cache schema:
  easyecom:rate:{account}:{location_key}:tokens     — current token count
  easyecom:rate:{account}:{location_key}:refilled   — last refill timestamp (s)
  easyecom:quota:{account}:{YYYY-MM-DD}             — daily call count
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import frappe

from ecommerce_super.easyecom.exceptions import EasyEcomRateLimitError

# Tier table per §3.10. (request_rate, burst, daily_quota).
TIER_LIMITS: dict[str, tuple[int, int, int]] = {
    "Default": (5, 10, 500),
    "Bronze": (5, 10, 50_000),
    "Silver": (20, 40, 200_000),
    "Gold": (30, 60, 300_000),
    "Diamond": (30, 60, 500_000),
}

# Quota threshold at which to start slowing non-urgent work (90% of daily).
QUOTA_WARN_THRESHOLD: float = 0.90


def tier_for_account(account_name: str) -> tuple[int, int, int]:
    """Return the (rate, burst, daily_quota) tuple for an Account's tier."""
    tier = frappe.db.get_value("EasyEcom Account", account_name, "rate_limit_tier")
    if not tier or tier not in TIER_LIMITS:
        # Refusing to guess — see §3.3.2 "no preset default".
        raise frappe.ValidationError(
            f"EasyEcom Account {account_name} has no rate_limit_tier set."
        )
    return TIER_LIMITS[tier]


def _bucket_key(account: str, location_key: str | None) -> str:
    return f"easyecom:rate:{account}:{location_key or '_account'}"


def _quota_key(account: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"easyecom:quota:{account}:{today}"


def acquire_token(account: str, location_key: str | None) -> None:
    """Block until a request slot is available, then consume it.

    Implements a simple sliding-window token bucket using Redis via
    frappe.cache():
      - Tokens refill at `request_rate` per second up to `burst`.
      - acquire_token() spins briefly until at least one token is available.
      - If the daily quota for the account is exhausted, raises
        EasyEcomRateLimitError immediately (queue treats as transient retry).
    """
    rate, burst, daily_quota = tier_for_account(account)

    # Check daily quota FIRST — refusing to consume a token if quota is gone.
    quota_used = current_daily_quota(account)
    if quota_used >= daily_quota:
        raise EasyEcomRateLimitError(
            f"Daily quota exhausted ({quota_used}/{daily_quota} for {account}).",
            retry_after=_seconds_until_next_utc_day(),
        )

    # Token bucket refill.
    base = _bucket_key(account, location_key)
    tokens_key = f"{base}:tokens"
    refilled_key = f"{base}:refilled"

    # Try up to `burst` attempts to grab a token, sleeping briefly between.
    # Practical upper bound: ~burst/rate seconds.
    deadline = time.monotonic() + max(burst / max(rate, 1), 2.0)
    while True:
        _refill(account, location_key, rate, burst, tokens_key, refilled_key)
        tokens = frappe.cache().get_value(tokens_key)
        try:
            tokens_int = int(tokens) if tokens is not None else burst
        except (TypeError, ValueError):
            tokens_int = burst

        if tokens_int >= 1:
            # Consume one token (decr is atomic).
            frappe.cache().decr(tokens_key)
            _bump_quota(account)
            return

        # Spin — wait long enough for one refill cycle.
        if time.monotonic() > deadline:
            raise EasyEcomRateLimitError(
                f"Rate limit not granted within {deadline:.1f}s; tier {account}.",
                retry_after=2,
            )
        time.sleep(max(1.0 / rate, 0.05))


def _refill(
    account: str,
    location_key: str | None,
    rate: int,
    burst: int,
    tokens_key: str,
    refilled_key: str,
) -> None:
    """Refill the bucket based on elapsed time since last refill."""
    now = time.monotonic()
    last = frappe.cache().get_value(refilled_key)
    if last is None:
        # First call — initialise to burst capacity.
        frappe.cache().set_value(tokens_key, burst)
        frappe.cache().set_value(refilled_key, now)
        return
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        last_f = now

    elapsed = now - last_f
    if elapsed <= 0:
        return

    add = int(elapsed * rate)
    if add <= 0:
        return

    try:
        current = int(frappe.cache().get_value(tokens_key) or 0)
    except (TypeError, ValueError):
        current = 0
    new_value = min(burst, current + add)
    frappe.cache().set_value(tokens_key, new_value)
    frappe.cache().set_value(refilled_key, now)


def _bump_quota(account: str) -> None:
    """Increment the daily call count for the account."""
    frappe.cache().incr(_quota_key(account))


def current_daily_quota(account: str) -> int:
    """Return today's call count for the account."""
    val = frappe.cache().get_value(_quota_key(account))
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def quota_consumption_pct(account: str) -> float:
    """Today's daily-quota consumption as a fraction in [0, 1+]."""
    _rate, _burst, quota = tier_for_account(account)
    used = current_daily_quota(account)
    return used / quota if quota else 0.0


def is_near_quota_warn(account: str) -> bool:
    """True when today's consumption has reached QUOTA_WARN_THRESHOLD.
    Used by Connection Health (§3.9) and the queue scheduler to slow
    non-urgent background work."""
    return quota_consumption_pct(account) >= QUOTA_WARN_THRESHOLD


def _seconds_until_next_utc_day() -> int:
    """Seconds until the daily quota counter resets (00:00 UTC)."""
    now = datetime.now(timezone.utc)
    next_day = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        + frappe.utils.timedelta(days=1)
    ).timestamp()
    return max(int(next_day - now.timestamp()), 1)
