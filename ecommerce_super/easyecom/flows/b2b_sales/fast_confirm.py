"""§11.3.5 — Fast-confirm queue check for New B2B push.

New B2B push returns "Successfully Queued" with no IDs in the body.
Without this module, the Map row sits with null OrderID/SuborderID/
InvoiceID until the */5 polling cron fires — up to 5 min latency
before the FDE can confirm the order really landed on EE.

Fast-confirm closes that gap. EE typically finishes queue jobs in
2-5 seconds. We poll `/getQueueStatus` up to 6 times at 5s intervals
(30s total ceiling). On status_id=3 (Finished), we backfill IDs
from the polling response IMMEDIATELY. On status_id=4 (Error), we
mark the Map → Drift and surface EE's error-report CSV URL in the
Sync Record. On timeout (still NEW/processing after 30s), we fall
through silently; the existing */5 polling cron + PR #101 backfill
catch up later.

Grounded against Thuraya 2026-06-28 (SAL-ORD-2026-00022):
  - getQueueStatus response carries `notes` field with
    `{"order_id":<int>,"reference_code":"<SO.name>"}` on Finished
  - status_id transitions 1 (NEW) → 3 (Finished) in ~5s
  - The notes order_id is the SAME as getOrderDetails' top-level
    order_id, so notes is sufficient as the primary lookup ID

Old B2B is NOT affected — Old B2B is synchronous, IDs arrive at
push time, no queue indirection.
"""
from __future__ import annotations

import json
import time
from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    ORDER_DETAILS_GET,
    QUEUE_STATUS_GET,
)
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomError,
)


# EE queue status enum (grounded against Thuraya 2026-06-28).
QUEUE_STATUS_NEW = "1"
QUEUE_STATUS_PROCESSING = "2"
QUEUE_STATUS_FINISHED = "3"
QUEUE_STATUS_ERROR = "4"

# Loop tuning. 6 attempts × 5s = 30s ceiling. EE typically finishes
# in 2-5s; 6 attempts is generous. Bigger ceiling would block the
# RQ worker too long for fast-confirm's nice-to-have value prop.
MAX_ATTEMPTS = 6
POLL_INTERVAL_SEC = 5


def fast_confirm_new_b2b(
    *,
    map_name: str,
    queue_id: str,
    location_key: str,
) -> dict[str, Any]:
    """Poll EE queue until terminal status or timeout. Backfill Map
    row IDs on Finished. Mark Map → Drift on Error.

    Returns a structured outcome dict with:
      - terminal_status_id: str ("3", "4", or None on timeout)
      - terminal_message: str
      - attempts: int
      - elapsed_sec: int
      - backfilled: dict | None (the IDs written to the Map)
      - error_csv_url: str | None (only on status_id=4)
      - timed_out: bool
    """
    client = EasyEcomClient(location_key=location_key)
    started_at = time.monotonic()
    snapshots: list[dict] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(
                QUEUE_STATUS_GET, params={"queueId": queue_id},
            )
        except (EasyEcomAPIError, EasyEcomError) as exc:
            # Transient error — log and fall through. Polling cron is
            # the safety net.
            return {
                "terminal_status_id": None,
                "terminal_message": f"{type(exc).__name__}: {exc}",
                "attempts": attempt,
                "elapsed_sec": int(time.monotonic() - started_at),
                "snapshots": snapshots,
                "backfilled": None,
                "error_csv_url": None,
                "timed_out": False,
                "exception": True,
            }

        data = response.get("data") or {}
        status_id = str(data.get("status_id") or "")
        snapshot = {
            "attempt": attempt,
            "status_id": status_id,
            "message": data.get("message"),
            "process_time": data.get("process_time"),
        }
        snapshots.append(snapshot)

        if status_id == QUEUE_STATUS_FINISHED:
            backfilled = _backfill_from_queue_finished(
                map_name=map_name, queue_status_data=data,
                client=client,
            )
            return {
                "terminal_status_id": status_id,
                "terminal_message": data.get("message") or "Finished",
                "attempts": attempt,
                "elapsed_sec": int(time.monotonic() - started_at),
                "snapshots": snapshots,
                "backfilled": backfilled,
                "error_csv_url": None,
                "timed_out": False,
            }

        if status_id == QUEUE_STATUS_ERROR:
            error_csv = data.get("result") or None
            _mark_map_drift(
                map_name=map_name,
                queue_status_data=data,
                error_csv_url=error_csv,
            )
            return {
                "terminal_status_id": status_id,
                "terminal_message": data.get("message") or "Error",
                "attempts": attempt,
                "elapsed_sec": int(time.monotonic() - started_at),
                "snapshots": snapshots,
                "backfilled": None,
                "error_csv_url": error_csv,
                "timed_out": False,
            }

        # Still NEW / PROCESSING / unknown — sleep and retry, unless
        # we've hit the ceiling.
        if attempt < MAX_ATTEMPTS:
            time.sleep(POLL_INTERVAL_SEC)

    # Timeout — exhausted MAX_ATTEMPTS without terminal status. The
    # */5 polling cron + PR #101 backfill will pick this up.
    return {
        "terminal_status_id": None,
        "terminal_message": "fast-confirm timeout — polling cron will backfill",
        "attempts": MAX_ATTEMPTS,
        "elapsed_sec": int(time.monotonic() - started_at),
        "snapshots": snapshots,
        "backfilled": None,
        "error_csv_url": None,
        "timed_out": True,
    }


def _backfill_from_queue_finished(
    *,
    map_name: str,
    queue_status_data: dict,
    client: EasyEcomClient,
) -> dict[str, Any]:
    """When queue is Finished, extract order_id from notes + fetch
    getOrderDetails for the suborder/invoice IDs, then write to Map.

    Notes field shape (grounded 2026-06-28):
      `{"order_id":561435048,"reference_code":"SAL-ORD-2026-00022"}`

    We need getOrderDetails for SuborderID and InvoiceID because
    those aren't in the queue status response. Same call the */5
    polling cron makes, just sooner.
    """
    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    notes_str = queue_status_data.get("notes") or ""

    notes_order_id: str | None = None
    try:
        if notes_str:
            notes = json.loads(notes_str)
            if isinstance(notes, dict) and notes.get("order_id"):
                notes_order_id = str(notes["order_id"])
    except (ValueError, TypeError):
        # Defensive — EE sometimes leaves notes as a non-JSON string.
        # Fall through to getOrderDetails, which is authoritative.
        pass

    # Call getOrderDetails for the full row (suborder + invoice IDs).
    # Reuse the same lookup key the */5 polling cron uses.
    sales_order = map_doc.sales_order
    try:
        details = client.get(
            ORDER_DETAILS_GET,
            params={"reference_code": sales_order},
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        # Queue says Finished but getOrderDetails failed. Backfill
        # only what we have from notes; polling cron will retry the
        # rest later.
        if notes_order_id and not map_doc.ee_order_id:
            updates = {"ee_order_id": notes_order_id}
            frappe.db.set_value(
                "EasyEcom B2B Order Map", map_name, updates,
                update_modified=False,
            )
            frappe.db.commit()
            return updates
        return {}

    rows = (details.get("data") or []) if isinstance(details, dict) else []
    b2b_rows = [r for r in rows if r.get("order_type_key") == "businessorder"]
    if not b2b_rows:
        # Queue Finished but getOrderDetails has no businessorder rows.
        # Edge case (replication lag?) — write notes order_id only.
        if notes_order_id and not map_doc.ee_order_id:
            updates = {"ee_order_id": notes_order_id}
            frappe.db.set_value(
                "EasyEcom B2B Order Map", map_name, updates,
                update_modified=False,
            )
            frappe.db.commit()
            return updates
        return {}

    # Take the latest row (multi-shipment-split safe, mirrors PR #101).
    latest = max(b2b_rows, key=lambda r: r.get("last_update_date") or "")

    updates: dict[str, Any] = {}
    if not map_doc.ee_order_id:
        order_id = latest.get("order_id") or notes_order_id
        if order_id:
            updates["ee_order_id"] = str(order_id)
    if not map_doc.ee_suborder_id:
        items = latest.get("order_items") or []
        if items and items[0].get("suborder_id"):
            updates["ee_suborder_id"] = str(items[0]["suborder_id"])
    if not map_doc.ee_invoice_id and latest.get("invoice_id"):
        updates["ee_invoice_id"] = str(latest["invoice_id"])

    if updates:
        frappe.db.set_value(
            "EasyEcom B2B Order Map", map_name, updates,
            update_modified=False,
        )
        frappe.db.commit()
    return updates


def _mark_map_drift(
    *,
    map_name: str,
    queue_status_data: dict,
    error_csv_url: str | None,
) -> None:
    """On status_id=4 (Error), transition Map status → Drift and
    persist the error CSV URL on last_error so the FDE can pull
    EE's detailed rejection report."""
    error_msg = queue_status_data.get("message") or "EE queue rejected the order"
    last_error_parts = [
        f"EE queue Finished with Error: {error_msg}",
    ]
    if error_csv_url:
        last_error_parts.append(f"Error report CSV: {error_csv_url}")
    if queue_status_data.get("upload_file"):
        last_error_parts.append(
            f"Original payload CSV: {queue_status_data['upload_file']}"
        )

    frappe.db.set_value(
        "EasyEcom B2B Order Map",
        map_name,
        {
            "status": "Drift",
            "last_error": " | ".join(last_error_parts)[:5000],
        },
        update_modified=False,
    )
    frappe.db.commit()
