"""Per-Company concurrency semaphore.

SPEC §6.3.7: Frappe RQ's worker count is global. For per-Company limits we
implement a semaphore on top of `frappe.cache()` (Redis-backed via Frappe's
own connection pool — no direct Redis client).

The semaphore is acquired by `execute_job` before dispatching to the
handler, and released in a `finally` block. If the cap is reached, the
worker raises `CompanyConcurrencyExceeded` which is classified as
transient — the job is re-enqueued with short back-off rather than the
worker blocking the slot.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import frappe

from ecommerce_super.easyecom.exceptions import CompanyConcurrencyExceeded

# Default per-Company cap if EasyEcom Account.max_concurrent_workers is not set.
DEFAULT_CAP: int = 4


def _cap_for_company(company: str) -> int:
    """Look up the per-Company concurrency cap. Falls back to the Account
    `max_concurrent_workers`, then to DEFAULT_CAP."""
    # There is exactly one EasyEcom Account per deployment (§3.1). Pull
    # whatever single account exists and read its cap. If no account is
    # configured yet (early bootstrap), use the default.
    account_name = frappe.db.get_value(
        "EasyEcom Account", filters={"enabled": 1}, fieldname="name"
    )
    if not account_name:
        return DEFAULT_CAP
    cap = frappe.db.get_value(
        "EasyEcom Account", account_name, "max_concurrent_workers"
    )
    return int(cap) if cap else DEFAULT_CAP


def _cache_key(company: str) -> str:
    return f"easyecom:concurrency:{company}"


@contextmanager
def company_concurrency_semaphore(company: str) -> Iterator[None]:
    """Acquire a per-Company slot. Raises CompanyConcurrencyExceeded if at
    capacity (caller should treat as transient and re-enqueue with back-off).

    Implemented via frappe.cache().incr/decr — atomic Redis ops through
    Frappe's connection pool, never a custom Redis client.
    """
    cap = _cap_for_company(company)
    key = _cache_key(company)

    current = frappe.cache().incr(key)
    if current > cap:
        # Roll back the increment and refuse the slot.
        frappe.cache().decr(key)
        raise CompanyConcurrencyExceeded(
            f"Company {company} at concurrency cap ({cap}); will retry."
        )

    try:
        yield
    finally:
        # Always release the slot, even if the handler raised.
        try:
            frappe.cache().decr(key)
        except Exception:
            # If decr fails (cache eviction, Redis hiccup), the counter will
            # drift but it's bounded — periodic reconciliation can reset it.
            # Don't let cleanup failure mask the real exception.
            pass


def current_count(company: str) -> int:
    """Return the current in-flight count for a Company (observability)."""
    val = frappe.cache().get_value(_cache_key(company))
    try:
        return int(val) if val is not None else 0
    except TypeError, ValueError:
        return 0


def reset(company: str) -> None:
    """Reset the counter for a Company. Used by reclaim_orphaned_jobs to
    clear drift from worker crashes."""
    frappe.cache().delete_value(_cache_key(company))
