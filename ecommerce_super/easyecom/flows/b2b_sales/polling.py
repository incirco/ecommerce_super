"""§11 Stage 3 — B2B Order Map polling reconciliation.

Design pivot from packet (approved 2026-06-14):
  - Packet assumed getAllOrders + cursor watermark sweep.
  - Endpoint probe surfaced EE's 7-day cap on `created_after`; a Map
    older than 7 days would be permanently abandoned.
  - getOrderDetails accepts `reference_code` with NO date constraint,
    deterministic per-Map lookup keyed on identifiers we already own.
  - Phase 1 polling is therefore: per-Map probe via
    /orders/V2/getOrderDetails?reference_code=<SO.name>. getAllOrders
    is DROPPED from Phase 1 entirely (zero value when ERPNext
    originates every B2B SO).

Cadence model:
  - Scheduler tick is FIXED at 5 minutes (hooks.py cron */5).
  - Per-Account `ecs_polling_cadence_minutes` Custom Field (default
    15) gates which Maps qualify per tick: a Map is polled iff
      last_polled_at IS NULL OR last_polled_at <= NOW() - cadence
  - Null `last_polled_at` means "never polled" — qualifies on the
    first tick after Map creation (~5 min delay to first poll).

Status derivation (LOCKED — see derive_local_status_from_ee_rows
docstring for the full rule table). Handles EE's two multi-row
semantics:
  - State-change history (new row per state change, same order_id).
  - Shipment splits (separate invoice rows per fulfillment chunk,
    same reference_code).

Discrepancy kinds raised:
  - "B2B Map orphaned at EE"
  - "B2B order cancelled by EE — polling-detected"
  - "B2B order partial cancellation detected"
  - "B2B unknown order_status_id"
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomError,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_location_for_warehouse,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id


PENDING_STATUSES: frozenset[str] = frozenset(
    {"Pushed", "Queued", "Invoice Pending"}
)

# Locked enum per EE docs.
KNOWN_ACTIVE_STATUS_IDS: frozenset[int] = frozenset(
    {1, 2, 3, 4, 5, 6, 7, 30}
)
CANCELLED_STATUS_ID: int = 9


# ============================================================
# Scheduler entry point.
# ============================================================


def reconcile_all_pending_b2b_orders() -> dict:
    """Cron tick handler (every 5 min). Iterates each enabled EE
    Account; for each, finds qualifying Maps (cooldown elapsed) and
    polls each.

    Returns a structured summary for the bench log / scheduler
    visibility.
    """
    summary: dict[str, Any] = {
        "accounts_processed": 0,
        "maps_polled": 0,
        "transitions": 0,
        "discrepancies_raised": 0,
        "errors": [],
    }

    accounts = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1},
        fields=[
            "name",
            "ecs_polling_cadence_minutes",
        ],
    )
    for acc in accounts:
        cadence = int(acc.get("ecs_polling_cadence_minutes") or 15)
        eligible = _find_eligible_maps(
            easyecom_account=acc["name"], cadence_minutes=cadence
        )
        for map_name in eligible:
            try:
                outcome = reconcile_one_map(map_name)
                summary["maps_polled"] += 1
                if outcome.get("transitioned"):
                    summary["transitions"] += 1
                if outcome.get("discrepancy_raised"):
                    summary["discrepancies_raised"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(
                    {
                        "map": map_name,
                        "exception": type(exc).__name__,
                        "message": str(exc)[:300],
                    }
                )
                frappe.log_error(
                    title=f"§11 polling failed for {map_name}",
                    message=f"{type(exc).__name__}: {exc}",
                )
        summary["accounts_processed"] += 1

    return summary


def _find_eligible_maps(
    *, easyecom_account: str, cadence_minutes: int
) -> list[str]:
    """Return Map names in pending statuses whose last_polled_at is
    NULL OR <= NOW() - cadence_minutes."""
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabEasyEcom B2B Order Map`
        WHERE easyecom_account = %(account)s
          AND status IN %(statuses)s
          AND (
            last_polled_at IS NULL
            OR last_polled_at <= DATE_SUB(NOW(), INTERVAL %(cadence)s MINUTE)
          )
        """,
        {
            "account": easyecom_account,
            "statuses": tuple(PENDING_STATUSES),
            "cadence": int(cadence_minutes),
        },
        as_dict=True,
    )
    return [r["name"] for r in rows]


# ============================================================
# Per-Map reconciliation.
# ============================================================


def reconcile_one_map(map_name: str) -> dict:
    """Poll one B2B Order Map via getOrderDetails and reconcile its
    state. Returns a structured outcome dict.

    Always stamps `last_polled_at` (success or failure) so a misbehaving
    Map doesn't get re-polled on every single tick.
    """
    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    sales_order = map_doc.sales_order
    ee_account_name = map_doc.easyecom_account

    # Resolve location_key from the SO's set_warehouse — same gate as
    # the push side. If the warehouse is no longer EE-mapped (rare
    # but possible during reconfiguration), skip + log.
    so_warehouse = frappe.db.get_value(
        "Sales Order", sales_order, "set_warehouse"
    )
    ee_location = (
        get_ee_location_for_warehouse(so_warehouse) if so_warehouse else None
    )
    location_key = (
        str(getattr(ee_location, "location_key", "")) if ee_location else ""
    )
    if not location_key:
        _stamp_last_polled(map_name)
        return {
            "ok": False,
            "transitioned": False,
            "detail": (
                f"Skip: warehouse {so_warehouse!r} no longer maps to a "
                "Live EE Location."
            ),
        }

    correlation_id = new_correlation_id()
    client = EasyEcomClient(location_key=location_key)
    try:
        response = client.get(
            ORDER_DETAILS_GET,
            params={"reference_code": sales_order},
            correlation_id=correlation_id,
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        _stamp_last_polled(map_name)
        return {
            "ok": False,
            "transitioned": False,
            "exception": type(exc).__name__,
            "message": str(exc)[:500],
        }

    rows = _extract_rows(response)
    decision, payload = derive_local_status_from_ee_rows(map_doc, rows)
    outcome = _apply_decision(
        map_doc=map_doc,
        decision=decision,
        payload=payload,
        rows=rows,
        ee_account_name=ee_account_name,
        correlation_id=correlation_id,
    )
    _stamp_last_polled(map_name)
    return outcome


def _extract_rows(response: Any) -> list[dict]:
    """getOrderDetails returns `data: [...]` (list of order rows).
    Defensive — accept both list and the {orders: [...]} variant
    just in case EE's response shape diverges between endpoints."""
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        orders = data.get("orders")
        if isinstance(orders, list):
            return [r for r in orders if isinstance(r, dict)]
    return []


# ============================================================
# Locked status derivation function.
# ============================================================


def derive_local_status_from_ee_rows(
    local_map: Any, rows: list[dict]
) -> tuple[str, Any]:
    """Conservative Phase 1 status derivation.

    Handles EE's multi-row response semantic (state-change history
    + shipment splits) and per-suborder partial cancellation via
    cancelled_quantity (FAQ #23).

    Returns one of:
      ("orphan", None)
      ("transition_to", "Cancelled")
      ("transition_to", "Invoice Pending")
      ("partial_cancel", {"total_item_qty", "cancelled_qty",
                          "cancelled_pct"})
      ("no_change", None)
      ("unknown", {"status_id", "latest_row_invoice_id"})
    """
    # Defensive filter: B2B rows only. EE's getOrderDetails returns
    # all order types matching the reference_code; we only act on
    # rows where order_type_key == "businessorder".
    b2b_rows = [
        r for r in rows if r.get("order_type_key") == "businessorder"
    ]
    if not b2b_rows:
        return ("orphan", None)

    # Aggregate item-level quantities across ALL rows (handles
    # shipment splits — same reference_code, multiple invoice rows
    # per fulfillment chunk).
    #
    # GROUNDING CORRECTION (paste 7 live finding on Harmony): EE's
    # getOrderDetails response returns the per-line array under the
    # key `order_items`, NOT `suborders` as the Phase 1 packet
    # assumed. The field rename preserves the same shape
    # (item_quantity / cancelled_quantity per row) — only the parent
    # key changed.
    total_item_qty = 0
    total_cancelled_qty = 0
    for r in b2b_rows:
        for sub in (r.get("order_items") or []):
            iq = sub.get("item_quantity") or 0
            cq = sub.get("cancelled_quantity") or 0
            total_item_qty += iq
            total_cancelled_qty += cq

    # Phase 1 full cancellation: ALL rows show order_status_id=9 AND
    # all quantity cancelled. The qty gate guards against a half-
    # cancelled shipment-split case where the row-level status flips
    # to 9 but an order_item still has uncancelled qty.
    rows_all_cancelled = all(
        r.get("order_status_id") == CANCELLED_STATUS_ID
        for r in b2b_rows
    )
    qty_all_cancelled = (
        total_item_qty > 0 and total_cancelled_qty == total_item_qty
    )

    if rows_all_cancelled and qty_all_cancelled:
        return ("transition_to", "Cancelled")

    # Partial cancellation: some qty cancelled but not all. Phase 1
    # surfaces this as a Discrepancy (credit notes, qty adjustments
    # are Phase 2 territory) — no local transition.
    if 0 < total_cancelled_qty < total_item_qty:
        return (
            "partial_cancel",
            {
                "total_item_qty": total_item_qty,
                "cancelled_qty": total_cancelled_qty,
                "cancelled_pct": round(
                    100 * total_cancelled_qty / total_item_qty, 1
                ),
            },
        )

    # Phase 1 invoice generation: ANY row has invoice_number populated.
    # EE adds a new row when an invoice is generated for a shipment
    # chunk, so the presence of invoice_number on at least one row is
    # the trigger.
    if any(r.get("invoice_number") for r in b2b_rows):
        return ("transition_to", "Invoice Pending")

    # Otherwise: defer to latest row for residual signals.
    latest = max(b2b_rows, key=lambda r: r.get("last_update_date") or "")
    status_id = latest.get("order_status_id")

    if status_id in KNOWN_ACTIVE_STATUS_IDS:
        return ("no_change", None)

    return (
        "unknown",
        {
            "status_id": status_id,
            "latest_row_invoice_id": latest.get("invoice_id"),
        },
    )


# ============================================================
# Decision dispatch — applies the derivation outcome.
# ============================================================


def _apply_decision(
    *,
    map_doc: Any,
    decision: str,
    payload: Any,
    rows: list[dict],
    ee_account_name: str,
    correlation_id: str,
) -> dict:
    """Apply the derivation function's decision: transition the Map
    state, raise a Discrepancy, or both."""
    from ecommerce_super.easyecom.flows.grn_pull import _raise_discrepancy

    company = frappe.db.get_value(
        "Sales Order", map_doc.sales_order, "company"
    )

    if decision == "orphan":
        _raise_discrepancy(
            kind="B2B Map orphaned at EE",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE has no rows for reference_code="
                f"{map_doc.sales_order!r} (EE Account {ee_account_name}, "
                f"correlation_id={correlation_id}). Map exists locally "
                "in status={local_status} but EE does not recognize the "
                "reference_code — likely push failure or EE-side data "
                "loss. FDE action required.".format(
                    local_status=map_doc.status,
                )
            ),
        )
        return {
            "ok": True,
            "transitioned": False,
            "discrepancy_raised": True,
            "decision": "orphan",
        }

    if decision == "transition_to" and payload == "Cancelled":
        frappe.db.set_value(
            "EasyEcom B2B Order Map",
            map_doc.name,
            {
                "status": "Cancelled",
                "cancelled_at": now_datetime(),
            },
            update_modified=False,
        )
        _raise_discrepancy(
            kind="B2B order cancelled by EE — polling-detected",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE reports order_status_id=9 (Cancelled) "
                f"with all quantities cancelled for reference_code="
                f"{map_doc.sales_order!r} (EE Account {ee_account_name}, "
                f"correlation_id={correlation_id}). Local Map transitioned "
                "to Cancelled. The Cancel Order webhook receiver lands in "
                "Phase 2; until then, polling is the recovery path."
            ),
        )
        frappe.db.commit()
        return {
            "ok": True,
            "transitioned": True,
            "discrepancy_raised": True,
            "decision": "cancelled",
        }

    if decision == "transition_to" and payload == "Invoice Pending":
        frappe.db.set_value(
            "EasyEcom B2B Order Map",
            map_doc.name,
            {"status": "Invoice Pending"},
            update_modified=False,
        )
        frappe.db.commit()
        return {
            "ok": True,
            "transitioned": True,
            "discrepancy_raised": False,
            "decision": "invoice_pending",
        }

    if decision == "partial_cancel":
        _raise_discrepancy(
            kind="B2B order partial cancellation detected",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE reports partial cancellation on "
                f"reference_code={map_doc.sales_order!r} "
                f"(EE Account {ee_account_name}, correlation_id="
                f"{correlation_id}). total_item_qty="
                f"{payload['total_item_qty']}, cancelled_qty="
                f"{payload['cancelled_qty']} ({payload['cancelled_pct']}%). "
                "No local Map transition — credit notes / qty "
                "adjustments are Phase 2 territory. FDE action required."
            ),
        )
        frappe.db.commit()
        return {
            "ok": True,
            "transitioned": False,
            "discrepancy_raised": True,
            "decision": "partial_cancel",
            "qty_payload": payload,
        }

    if decision == "unknown":
        # Pickle the entire payload to the reason for forensic visibility.
        try:
            payload_json = json.dumps(rows, default=str)[:4000]
        except Exception:
            payload_json = "(payload not JSON-serialisable)"
        _raise_discrepancy(
            kind="B2B unknown order_status_id",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE returned an order_status_id "
                f"{payload['status_id']!r} that's outside the known "
                f"enum {{1,2,3,4,5,6,7,9,30}} for reference_code="
                f"{map_doc.sales_order!r} (EE Account {ee_account_name}, "
                f"correlation_id={correlation_id}). latest_row "
                f"invoice_id={payload['latest_row_invoice_id']}. "
                f"No local transition — FDE action required to "
                f"interpret the unknown status. Full payload: "
                f"{payload_json}"
            ),
        )
        return {
            "ok": True,
            "transitioned": False,
            "discrepancy_raised": True,
            "decision": "unknown",
        }

    # decision == "no_change" — quiet path, just refresh last_polled_at.
    return {
        "ok": True,
        "transitioned": False,
        "discrepancy_raised": False,
        "decision": "no_change",
    }


def _stamp_last_polled(map_name: str) -> None:
    """Touch last_polled_at — fires on every poll, success or fail.
    Without this, a misbehaving Map (HTTP errors, orphan, etc.) would
    re-poll on every single 5-min tick, hammering EE quota."""
    try:
        frappe.db.set_value(
            "EasyEcom B2B Order Map",
            map_name,
            "last_polled_at",
            now_datetime(),
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        # Don't let last_polled_at failures block reconciliation.
        pass
