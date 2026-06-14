"""Date formatters for the §11 createOrder payload.

EE expects two distinct datetime strings on a B2B order:
  - `orderDate`         — UTC, "YYYY-MM-DD HH:MM:SS"
  - `expDeliveryDate`   — IST, "YYYY-MM-DD HH:MM:SS"

ERPNext SO has `transaction_date` (Date, no time-of-day) and
`delivery_date` (Date). The standard SO doesn't store a
`transaction_time`; the §11 packet referenced one that doesn't
exist, and the design-lead's pre-Stage-1 ruling was to drop the
time component entirely: midnight IST → UTC is deterministic,
idempotent under retries, and matches ERPNext's accounting
semantic of `transaction_date` as the order's official date.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from frappe.utils import get_datetime, getdate


IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def format_utc_datetime(date_part: Any, time_part: Any = None) -> str:
    """Format ERPNext date (+ optional time) as a UTC string for orderDate.

    EE expects `YYYY-MM-DD HH:MM:SS` in UTC. If `time_part` is None,
    default to 00:00:00 IST, then convert to UTC — produces the
    previous day's 18:30:00 in UTC for an Indian midnight.

    `time_part` is accepted for forward compatibility (Custom-Field
    transaction_time, if/when added) but §11 Phase 1 callers pass
    only the date.
    """
    d = getdate(date_part)
    if time_part is None:
        t = time(0, 0, 0)
    else:
        t = get_datetime(time_part).time()
    dt_ist = datetime.combine(d, t, tzinfo=IST)
    dt_utc = dt_ist.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


def format_ist_date(date_part: Any) -> str:
    """Format ERPNext date as an IST midnight datetime string for
    expDeliveryDate.

    EE expects `YYYY-MM-DD HH:MM:SS` in IST. SO `delivery_date` is a
    Date — use 00:00:00 IST. No timezone marker in the output (EE's
    field-level semantic carries the timezone, not the string).
    """
    d = getdate(date_part)
    dt_ist = datetime.combine(d, time(0, 0, 0), tzinfo=IST)
    return dt_ist.strftime("%Y-%m-%d %H:%M:%S")
