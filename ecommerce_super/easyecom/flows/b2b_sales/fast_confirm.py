"""§11.3.5 — Fast-confirm queue check for New B2B push.

New B2B push returns "Successfully Queued" with no IDs in the body.
Without this module, the Map row sits with null OrderID/SuborderID/
InvoiceID until the */5 polling cron fires — up to 5 min latency
before the FDE can confirm the order really landed on EE.

Fast-confirm closes that gap. EE typically finishes queue jobs in
2-5 seconds. We poll `/getQueueStatus` up to 6 times at 5s intervals
(30s total ceiling). On status_id=3 (Finished), we backfill IDs
from the polling response IMMEDIATELY. On status_id=4 (Error), we
mark the Map → Failed (gh#254 — a distinct terminal status, no longer
the ambiguous "Drift"), parse EE's error-report CSV for the failing
SKUs + reason, write that onto last_error, mirror it to the Sales
Order timeline, and raise a "B2B Push Failed" Integration Discrepancy
so the rejection is visible + actionable (not buried in an audit
field). On timeout (still NEW/processing after 30s), we fall through
silently; the existing */5 polling cron + PR #101 backfill catch up
later.

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
    row IDs on Finished. Mark Map → Failed on Error (gh#254).

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
            _mark_map_failed(
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


def _mark_map_failed(
    *,
    map_name: str,
    queue_status_data: dict,
    error_csv_url: str | None,
) -> None:
    """gh#254 — on status_id=4 (Error), transition Map status → **Failed**
    (a distinct terminal status, not the ambiguous "Drift" that looked
    identical to an in-progress order), and make the rejection visible:

      1. Parse EE's error-report CSV for the failing SKUs + reason and
         put a human-readable summary on `last_error` (so the FDE sees
         *which* SKUs failed and *why* without downloading the CSV).
      2. Mirror that reason to the Sales Order timeline and raise a
         "B2B Push Failed" Integration Discrepancy so the FDE/user sees
         it where they look.

    The status + last_error write is the single deterministic side
    effect. The SO-facing surfacing (step 2) is isolated in
    `_surface_rejection_on_so` and fully guarded, so a failure there can
    never leave the Map without its Failed status.
    """
    error_msg = queue_status_data.get("message") or "EE queue rejected the order"

    # Best-effort: fetch + parse EE's per-line error CSV into a readable
    # "reason: SKU, SKU" summary. Never raises.
    parsed_summary = _parse_ee_error_csv(error_csv_url) if error_csv_url else None

    last_error_parts = [f"EE queue Finished with Error: {error_msg}"]
    if parsed_summary:
        last_error_parts.append(f"Failing lines — {parsed_summary}")
    if error_csv_url:
        last_error_parts.append(f"Error report CSV: {error_csv_url}")
    if queue_status_data.get("upload_file"):
        last_error_parts.append(
            f"Original payload CSV: {queue_status_data['upload_file']}"
        )
    last_error = " | ".join(last_error_parts)[:5000]

    frappe.db.set_value(
        "EasyEcom B2B Order Map",
        map_name,
        {
            "status": "Failed",
            "last_error": last_error,
        },
        update_modified=False,
    )
    frappe.db.commit()

    # Surface to the user — SO timeline comment + FDE Discrepancy.
    # Fully guarded: the Map is already Failed regardless of what happens
    # here, so a surfacing failure degrades gracefully to the audit field.
    try:
        _surface_rejection_on_so(
            map_name=map_name,
            error_msg=error_msg,
            parsed_summary=parsed_summary,
            error_csv_url=error_csv_url,
        )
    except Exception:
        frappe.log_error(
            title=f"gh#254: could not surface EE rejection for {map_name}",
            message=frappe.get_traceback(),
        )


def _parse_ee_error_csv(url: str) -> str | None:
    """Best-effort: GET EE's error-report CSV and summarise the failing
    lines as "<reason>: <sku>, <sku> | <reason>: <sku>". Returns None on
    any failure (missing lib, network error, unexpected shape) — the
    caller falls back to just linking the raw CSV. Never raises.

    EE's CSV columns (observed): OrderItemId, Sku, Quantity, Price, Message.
    """
    try:
        import csv as _csv
        import io as _io

        import requests  # bundled with Frappe

        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        reader = _csv.DictReader(_io.StringIO(resp.text))
        by_reason: dict[str, list[str]] = {}
        for row in reader:
            # Tolerate header-case / whitespace variance.
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            reason = norm.get("message") or "Unspecified error"
            sku = norm.get("sku") or norm.get("orderitemid") or "?"
            by_reason.setdefault(reason, [])
            if sku not in by_reason[reason]:
                by_reason[reason].append(sku)
        if not by_reason:
            return None
        # Cap to keep last_error readable; note if truncated.
        parts = []
        for reason, skus in by_reason.items():
            shown = skus[:20]
            suffix = f" (+{len(skus) - 20} more)" if len(skus) > 20 else ""
            parts.append(f"{reason}: {', '.join(shown)}{suffix}")
        return " | ".join(parts)[:2000]
    except Exception:
        return None


def _surface_rejection_on_so(
    *,
    map_name: str,
    error_msg: str,
    parsed_summary: str | None,
    error_csv_url: str | None,
) -> None:
    """Make the rejection visible where the user will actually see it:
    a comment on the Sales Order timeline + a "B2B Push Failed" Integration
    Discrepancy (which flows into the existing B2B push-failures worklist).
    Idempotent-ish: skips re-adding if an identical rejection comment
    already exists on the SO."""
    map_row = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        map_name,
        ["sales_order", "company", "easyecom_account"],
        as_dict=True,
    )
    if not map_row or not map_row.get("sales_order"):
        return
    so_name = map_row["sales_order"]

    reason_line = (
        f"Failing lines — {parsed_summary}" if parsed_summary
        else (f"See EE error report: {error_csv_url}" if error_csv_url else error_msg)
    )
    comment_text = (
        f"<b>EasyEcom rejected this order.</b> The SO was submitted and pushed, "
        f"but EasyEcom's queue did not create the order.<br>"
        f"Reason: {frappe.utils.escape_html(error_msg)}<br>"
        f"{frappe.utils.escape_html(reason_line)}<br>"
        f"<i>Fix the flagged SKU(s) in EasyEcom, then re-submit / re-push. "
        f"See B2B Order Map {map_name} (status=Failed) for full details.</i>"
    )

    # De-dupe: don't spam the timeline if polling/retry re-enters.
    already = frappe.db.exists(
        "Comment",
        {
            "reference_doctype": "Sales Order",
            "reference_name": so_name,
            "comment_type": "Comment",
            "content": ["like", "%EasyEcom rejected this order.%"],
        },
    )
    if not already:
        so_doc = frappe.get_doc("Sales Order", so_name)
        so_doc.add_comment("Comment", text=comment_text)

    # FDE-actionable Discrepancy — reuse the existing "B2B Push Failed"
    # kind so it lands in the current B2B push-failures worklist card.
    from ecommerce_super.easyecom.flows.grn_pull import _raise_discrepancy

    _raise_discrepancy(
        kind="B2B Push Failed",
        reference_doctype="Sales Order",
        reference_name=so_name,
        company=map_row.get("company"),
        reason=(
            f"§11 New B2B: EasyEcom's async queue REJECTED SO {so_name} "
            f"(Map {map_name}, account {map_row.get('easyecom_account')}). "
            f"EE message: {error_msg}. "
            + (f"{reason_line}. " if parsed_summary else "")
            + "Order was not created in EasyEcom. Fix the SKU(s) in EE and re-push."
        ),
    )
    frappe.db.commit()
