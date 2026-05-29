"""Connection Health dashboard backend.

SPEC §3.9 — surfaces, at the account level and rolled up per Company:
  - Last successful authentication timestamp per location_key
  - API call success rate (last 1h, 24h, 7d)
  - Average latency per endpoint
  - Outstanding queue depth (Queued + Retrying)
  - Webhook receipt rate
  - Sync cursor lag
  - Daily API quota consumption against the rate_limit_tier ceiling

The full Connection Health dashboard UI is part of the operational surface
(§17); this module provides the data-shape it consumes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import frappe

from ecommerce_super.easyecom.client.rate_limit import (
    current_daily_quota,
    quota_consumption_pct,
    tier_for_account,
)


def account_health() -> dict[str, Any]:
    """Account-wide health rollup. Returns a dict ready for JSON serialisation."""
    account = frappe.db.get_value(
        "EasyEcom Account",
        filters={"enabled": 1},
        fieldname=["name", "rate_limit_tier", "connection_status"],
        as_dict=True,
    )
    if not account:
        return {"error": "No enabled EasyEcom Account configured."}

    locations = frappe.db.get_all(
        "EasyEcom Location",
        filters={"enabled": 1},
        fields=[
            "name",
            "location_key",
            "is_primary",
            "is_operational",
            "jwt_acquired_at",
            "jwt_expires_at",
            "frappe_company",
        ],
    )

    quota_used = current_daily_quota(account.name)
    _rate, _burst, quota_cap = tier_for_account(account.name)

    return {
        "account": account.name,
        "tier": account.rate_limit_tier,
        "connection_status": account.connection_status,
        "locations": locations,
        "daily_quota_used": quota_used,
        "daily_quota_cap": quota_cap,
        "daily_quota_pct": round(quota_consumption_pct(account.name) * 100, 2),
        "queue_depth": _outstanding_queue_depth(),
        "success_rate_1h": _api_success_rate(hours=1),
        "success_rate_24h": _api_success_rate(hours=24),
        "success_rate_7d": _api_success_rate(hours=24 * 7),
    }


def company_health(company: str) -> dict[str, Any]:
    """Per-Company rollup. Used by the per-Company settings header."""
    company_locations = frappe.db.get_all(
        "EasyEcom Location",
        filters={"enabled": 1, "frappe_company": company},
        fields=["name", "location_key", "jwt_acquired_at", "jwt_expires_at"],
    )

    return {
        "company": company,
        "locations": company_locations,
        "queue_depth": _outstanding_queue_depth(company=company),
        "success_rate_1h": _api_success_rate(hours=1, company=company),
        "success_rate_24h": _api_success_rate(hours=24, company=company),
        "last_successful_at": _last_successful_api_call(company=company),
        "webhook_count_24h": _webhook_count_24h(company=company),
    }


def update_account_connection_status() -> str:
    """Compute connection_status from the last few API calls and write it
    to the Account. Called every minute by a scheduler hook (or whenever
    connection state is checked).

    Returns the computed status string.
    """
    name = frappe.db.get_value(
        "EasyEcom Account", filters={"enabled": 1}, fieldname="name"
    )
    if not name:
        return "Disabled"

    # Look at the last 1 hour of foundational + entity-sync calls; classify.
    cutoff = frappe.utils.now_datetime() - timedelta(hours=1)
    rows = frappe.db.get_all(
        "EasyEcom API Call",
        filters={"attempted_at": [">=", cutoff]},
        fields=["status", "error_class"],
        limit_page_length=500,
    )
    if not rows:
        # No recent calls at all — keep prior status; don't downgrade
        # Connected to Down just because the site has been idle.
        return frappe.db.get_value("EasyEcom Account", name, "connection_status")

    # Rate-limit cooldowns (EE's §31.3.1 60s lockout, surfaced as
    # EasyEcomRateLimitError) are NOT connection-health failures — the
    # connection is fine; the caller just retried too soon. Counting them
    # would downgrade Connected → Degraded/Down for a user who clicked
    # Test Connection twice in a row, which is misleading (gh#2).
    total = len(rows)
    failed = sum(
        1 for r in rows
        if r.status != "Success" and r.error_class != "EasyEcomRateLimitError"
    )
    fail_rate = failed / total if total else 0

    if fail_rate == 0:
        status = "Connected"
    elif fail_rate < 0.20:
        status = "Degraded"
    else:
        status = "Down"

    frappe.db.set_value("EasyEcom Account", name, "connection_status", status)
    frappe.db.commit()
    return status


# ----- Internal queries -----


def _outstanding_queue_depth(company: str | None = None) -> int:
    filters = {"state": ["in", ["Queued", "Retrying"]]}
    if company:
        filters["company"] = company
    return frappe.db.count("EasyEcom Queue Job", filters=filters)


def _api_success_rate(*, hours: int, company: str | None = None) -> float:
    """Return success rate in [0, 1] over the last `hours`. None if no calls."""
    cutoff = frappe.utils.now_datetime() - timedelta(hours=hours)
    filters: dict[str, Any] = {"attempted_at": [">=", cutoff]}
    if company:
        filters["company"] = company
    total = frappe.db.count("EasyEcom API Call", filters=filters)
    if not total:
        return 1.0  # No data — don't trigger spurious "degraded" alerts.
    success = frappe.db.count(
        "EasyEcom API Call",
        filters={**filters, "status": "Success"},
    )
    return round(success / total, 4)


def _last_successful_api_call(*, company: str | None = None):
    filters: dict[str, Any] = {"status": "Success"}
    if company:
        filters["company"] = company
    row = frappe.db.get_all(
        "EasyEcom API Call",
        filters=filters,
        fields=["attempted_at"],
        order_by="attempted_at DESC",
        limit_page_length=1,
    )
    return row[0].attempted_at if row else None


def _webhook_count_24h(*, company: str | None = None) -> int:
    cutoff = frappe.utils.now_datetime() - timedelta(hours=24)
    filters: dict[str, Any] = {"received_at": [">=", cutoff]}
    if company:
        filters["company"] = company
    return frappe.db.count("EasyEcom Webhook Event", filters=filters)
