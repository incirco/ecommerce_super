"""Date formatters for the §11 createOrder payload.

**EE timezone behaviour (verified against Harmony 2026-06-28)**:

EE's `orderDate` parser is timezone-aware:

  - Without offset (`"2026-06-28 00:00:00"`), EE treats input as UTC
    and adds +5:30 when displaying in IST UI → `"2026-06-28 05:30:00"`.
    Wire and display don't match.
  - With explicit IST offset (`"2026-06-28 00:00:00+05:30"`), EE
    honors the timezone and stores the moment correctly. Display
    strips the offset and shows `"2026-06-28 00:00:00"`. The date
    and time portions on wire match the display verbatim.

We send **`"YYYY-MM-DD HH:MM:SS+05:30"`** (IST with explicit offset)
so the wire and display match. The trailing `+05:30` is a wire-only
protocol marker (EE strips it on display) — it's the cost of getting
EE to honor IST instead of defaulting to UTC interpretation.

  - `format_ist_datetime(date, time=None)` → orderDate (IST with `+05:30`)
  - `format_ist_date(date)`               → expDeliveryDate (IST midnight, no offset)

Why no offset on `expDeliveryDate`? Verified: that field doesn't
get the +5:30 shift even without an offset — EE treats `expDeliveryDate`
as IST by default and `orderDate` as UTC by default. Per-field
asymmetry on EE side; we mirror it field-by-field.

`format_utc_datetime` is retained as a deprecated alias pointing at
`format_ist_datetime`. The old name was a misnomer (it produced UTC
strings under the prior convention); the alias preserves any
external callers during cutover.

ERPNext SO has `transaction_date` (Date, no time-of-day) and
`delivery_date` (Date). The standard SO doesn't store a
`transaction_time`; the §11 packet referenced one that doesn't
exist, and the design-lead's pre-Stage-1 ruling was to drop the
time component entirely.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from frappe.utils import get_datetime, getdate


IST = ZoneInfo("Asia/Kolkata")


def format_ist_datetime(date_part: Any, time_part: Any = None) -> str:
    """Format ERPNext date (+ optional time) as an IST datetime string
    with explicit `+05:30` timezone offset for `orderDate`.

    Returns `"YYYY-MM-DD HH:MM:SS+05:30"`. The trailing offset is
    required because EE's `orderDate` parser defaults to UTC
    interpretation without it (verified live 2026-06-28). With the
    offset, EE honors IST and strips the marker on display — wire
    date AND time match the EE UI display verbatim.

    If `time_part` is None, default to 00:00:00 IST. `time_part` is
    accepted for forward compatibility (Custom-Field transaction_time,
    if/when added) but §11 callers pass only the date.
    """
    d = getdate(date_part)
    if time_part is None:
        t = time(0, 0, 0)
    else:
        t = get_datetime(time_part).time()
    dt_ist = datetime.combine(d, t, tzinfo=IST)
    # Hardcode `+05:30` suffix — IST is fixed (no daylight saving).
    # Python's %z produces `+0530` without the colon; EE accepts the
    # colon-separated form per the live probe (2026-06-28).
    return dt_ist.strftime("%Y-%m-%d %H:%M:%S") + "+05:30"


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


# Backwards-compat alias for any external caller. The old name was a
# misnomer (it produced UTC strings); the new name is accurate. Delete
# this alias in a future cleanup once we confirm no external callers.
format_utc_datetime = format_ist_datetime  # DEPRECATED: use format_ist_datetime
