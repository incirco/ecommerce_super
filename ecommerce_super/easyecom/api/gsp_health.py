"""§11.5.1 gh#150 Part 1 — Custom GSP Health metrics endpoint.

Feeds the "EasyEcom GSP Health" workspace card. Returns 5 aggregate
metrics in one call so the frontend renders in a single round-trip.

Metrics:
  1. last_success_at       Timestamp of the most recent inbound
                           /einvoice/update call that returned HTTP 200
  2. inbound_today         Count of inbound Custom GSP API Calls today
  3. failed_inbound_today  Same as above but HTTP status ≠ 200
  4. top_failure_reasons   Top 3 error reasons grouped by last_error
                           from Sync Records with direction='Inbound API'
                           status='Failed' in the last 24h
  5. stuck_orders_6h       Count of B2B Order Maps with status='Invoice
                           Pending' modified > 6 hours ago (indicates
                           stuck / awaiting invoice)

Read-only, permission-gated (users must be able to read EasyEcom
Account — effectively the FDE role scope).

All queries are defensive: if the underlying doctype / column is
missing (fresh install, IC not yet deployed), the metric degrades to
zero/None rather than crashing the whole card.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe import _


_STUCK_THRESHOLD_HOURS = 6


@frappe.whitelist()
def get_metrics() -> dict:
    """Return all 5 GSP health metrics in one payload. Read-only.

    Never raises. Any per-metric failure degrades that specific metric
    to zero/None so a single site-schema quirk doesn't blank the whole
    card.
    """
    if not frappe.has_permission("EasyEcom Account", "read"):
        frappe.throw(_("Not permitted to read GSP health metrics"))

    return {
        "last_success_at": _safe(_last_successful_einvoice_at),
        "inbound_today": _safe(_count_inbound_today, default=0),
        "failed_inbound_today": _safe(_count_failed_inbound_today, default=0),
        "top_failure_reasons": _safe(_top_failure_reasons, default=[]),
        "stuck_orders_6h": _safe(_count_stuck_orders_over_6h, default=0),
        "as_of": frappe.utils.now(),
    }


def _safe(fn, default=None):
    """Run fn, catching any Exception and returning default. Each
    metric is independent — one broken query shouldn't blank the card."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _last_successful_einvoice_at() -> str | None:
    """Most recent inbound /einvoice/update with HTTP 200. Returns
    ISO datetime string, or None if no successful call ever recorded.

    Uses SQL directly because we need MAX() with LIKE on endpoint —
    ORM's get_all doesn't support that shape cleanly."""
    row = frappe.db.sql(
        """SELECT MAX(creation) AS ts
           FROM `tabEasyEcom API Call`
           WHERE direction = 'Inbound'
             AND endpoint LIKE '%%einvoice/update%%'
             AND http_status = 200""",
        as_dict=True,
    )
    if not row or not row[0].get("ts"):
        return None
    return str(row[0]["ts"])


def _count_inbound_today() -> int:
    """All inbound Custom GSP API Calls today (any status)."""
    row = frappe.db.sql(
        """SELECT COUNT(*) AS n
           FROM `tabEasyEcom API Call`
           WHERE direction = 'Inbound'
             AND DATE(creation) = CURDATE()""",
        as_dict=True,
    )
    return int((row or [{}])[0].get("n", 0) or 0)


def _count_failed_inbound_today() -> int:
    """Inbound API Calls today with non-200 status (2xx = success)."""
    row = frappe.db.sql(
        """SELECT COUNT(*) AS n
           FROM `tabEasyEcom API Call`
           WHERE direction = 'Inbound'
             AND DATE(creation) = CURDATE()
             AND (http_status IS NULL OR http_status >= 300)""",
        as_dict=True,
    )
    return int((row or [{}])[0].get("n", 0) or 0)


def _top_failure_reasons() -> list[dict]:
    """Top 3 error reasons from Failed inbound Sync Records in the
    last 24h. Groups by last_error, returns [{reason, count}] sorted
    by count desc.

    Truncates each reason to 120 chars for display; the workspace
    card is not the place for wall-of-text stack traces."""
    if not frappe.db.exists("DocType", "EasyEcom Sync Record"):
        return []
    if not frappe.db.has_column("EasyEcom Sync Record", "last_error"):
        return []

    rows = frappe.db.sql(
        """SELECT
              SUBSTRING(COALESCE(last_error, '(no error message)'), 1, 120) AS reason,
              COUNT(*) AS count
           FROM `tabEasyEcom Sync Record`
           WHERE direction = 'Inbound API'
             AND status = 'Failed'
             AND creation >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
           GROUP BY reason
           ORDER BY count DESC
           LIMIT 3""",
        as_dict=True,
    )
    return [
        {"reason": r["reason"], "count": int(r["count"])}
        for r in (rows or [])
    ]


def _count_stuck_orders_over_6h() -> int:
    """B2B Order Maps in Invoice Pending state for more than 6 hours
    (the polling reconciler runs hourly, so anything > 6h is genuinely
    stuck and needs FDE attention)."""
    row = frappe.db.sql(
        """SELECT COUNT(*) AS n
           FROM `tabEasyEcom B2B Order Map`
           WHERE status = 'Invoice Pending'
             AND modified < DATE_SUB(NOW(), INTERVAL %s HOUR)""",
        (_STUCK_THRESHOLD_HOURS,),
        as_dict=True,
    )
    return int((row or [{}])[0].get("n", 0) or 0)
