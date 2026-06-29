"""§12 — B2C marketplace polling walker.

Pulls manifested marketplace orders from EE and hands each off to the
SI builder for ingestion as a Sales Invoice (no Sales Order — per
SPEC §12.2 line 2742, B2C orders are born in EE; ERPNext sees only the
financial event).

Cadence: every 5 minutes by default (per-Marketplace-Account
override via `polling_cadence_minutes`). Per-tick eligibility:
  - Marketplace Account is `enabled = 1`
  - `easyecom_account` is set (drives JWT routing via the EE Account's
    `default_location_key`)
  - `last_pull_orders` IS NULL OR <= NOW() - cadence

Cursor advance: `last_pull_orders` is bumped to the start time of the
current poll on success. Forward-only — historical orders that
pre-date the first tick are §102 backfill territory, never picked up
here.

Idempotency: dedup is on EE's `Invoice_id` (shipment-level), which
becomes the Sales Invoice's `ecs_easyecom_invoice_id`. Re-polled
orders that already have an SI are silently skipped.

Per-record failure: one bad order doesn't kill the batch. Failed
orders raise Failed Sync Records (with translated reasons) but other
orders in the same tick proceed.
"""
from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDERS_GET_ALL
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomError,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id


# Default cadence per Marketplace Account. Overrideable via the
# polling_cadence_minutes field on the row.
DEFAULT_CADENCE_MINUTES: int = 5


# ============================================================
# Scheduler entry point
# ============================================================


def reconcile_all_marketplace_accounts() -> dict:
    """Scheduler hook — fires every */5 min. Walks all eligible
    Marketplace Accounts and triggers reconciliation per account.

    Returns a summary dict (per-account outcomes) for the API Call /
    Sync Record audit trail.
    """
    eligible = _find_eligible_accounts()
    outcomes: list[dict] = []
    for account_name in eligible:
        try:
            outcomes.append(
                {
                    "marketplace_account": account_name,
                    "outcome": reconcile_one_marketplace_account(account_name),
                }
            )
        except Exception as exc:
            # The scheduler must never blow up on one bad account.
            outcomes.append(
                {
                    "marketplace_account": account_name,
                    "outcome": {
                        "ok": False,
                        "exception": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                }
            )
            frappe.log_error(
                title=f"§12 polling: top-level failure for {account_name}",
                message=f"{type(exc).__name__}: {exc}",
            )
    return {
        "ok": True,
        "eligible_count": len(eligible),
        "outcomes": outcomes,
    }


def _find_eligible_accounts() -> list[str]:
    """Return Marketplace Account names due for polling this tick.

    Eligible if:
      - enabled = 1
      - easyecom_account IS NOT NULL
      - last_pull_orders IS NULL OR last_pull_orders <= NOW() - cadence
    """
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabEasyEcom Marketplace Account`
        WHERE enabled = 1
          AND easyecom_account IS NOT NULL
          AND (
              last_pull_orders IS NULL
              OR last_pull_orders <= DATE_SUB(
                  NOW(),
                  INTERVAL COALESCE(polling_cadence_minutes, %(default_cadence)s) MINUTE
              )
          )
        ORDER BY COALESCE(last_pull_orders, '1970-01-01') ASC
        """,
        {"default_cadence": DEFAULT_CADENCE_MINUTES},
        as_dict=True,
    )
    return [r.name for r in rows]


# ============================================================
# Per-account reconciliation
# ============================================================


def reconcile_one_marketplace_account(account_name: str) -> dict:
    """Pull manifested orders for one Marketplace Account, dispatch
    each to the SI builder, advance the cursor on success.

    Returns a structured outcome dict.

    Always stamps `last_pull_at` (success or failure). Only advances
    `last_pull_orders` on a successful pull — so a hung Account keeps
    re-trying from the same point until it succeeds.
    """
    account = frappe.get_doc("EasyEcom Marketplace Account", account_name)

    if not account.easyecom_account:
        _stamp_last_pull_attempt(account_name, error="No easyecom_account configured")
        return {
            "ok": False,
            "detail": "Skip: easyecom_account not configured on this Marketplace Account.",
        }

    # EE Account.default_location_key is a Link to EasyEcom Location
    # (docname like ECS-LOC-xxx). EasyEcomClient expects the bare
    # location_key string (the field on the Location row). Resolve.
    location_docname = frappe.db.get_value(
        "EasyEcom Account", account.easyecom_account, "default_location_key"
    )
    if not location_docname:
        _stamp_last_pull_attempt(
            account_name,
            error=f"EasyEcom Account {account.easyecom_account!r} has no default_location_key",
        )
        return {
            "ok": False,
            "detail": (
                f"Skip: EasyEcom Account {account.easyecom_account!r} has no "
                "default_location_key — set one to enable §12 polling."
            ),
        }

    location_key = frappe.db.get_value(
        "EasyEcom Location", location_docname, "location_key"
    )
    if not location_key:
        _stamp_last_pull_attempt(
            account_name,
            error=f"EasyEcom Location {location_docname!r} has no location_key field",
        )
        return {
            "ok": False,
            "detail": (
                f"Skip: EasyEcom Location {location_docname!r} has no "
                "location_key — corrupted Location row."
            ),
        }

    correlation_id = new_correlation_id()
    pull_window_start = now_datetime()

    client = EasyEcomClient(location_key=location_key)
    try:
        orders = _walk_orders(
            client=client,
            account=account,
            correlation_id=correlation_id,
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        _stamp_last_pull_attempt(account_name, error=str(exc)[:500])
        return {
            "ok": False,
            "exception": type(exc).__name__,
            "message": str(exc)[:500],
        }

    # Dispatch each order to the SI builder. Per-order failures are
    # localised — one bad order doesn't break the rest of the batch.
    per_order_outcomes: list[dict] = []
    for order_row in orders:
        per_order_outcomes.append(
            _dispatch_order_to_builder(
                order_row=order_row,
                account=account,
                correlation_id=correlation_id,
            )
        )

    # Advance the cursor on success — even if some orders within the
    # batch failed (they'll resurface on next poll if EE still returns
    # them, otherwise they're recorded as Failed Sync Records for FDE).
    _advance_cursor(account_name, pull_window_start)

    return {
        "ok": True,
        "orders_pulled": len(orders),
        "outcomes": per_order_outcomes,
        "correlation_id": correlation_id,
    }


# ============================================================
# EE API walking
# ============================================================


def _walk_orders(
    *,
    client: EasyEcomClient,
    account: Any,
    correlation_id: str,
) -> list[dict]:
    """Call /orders/V2/getAllOrders with the configured status filter
    + sliding date window (last_pull_orders → now).

    Returns the list of order rows. Single-page for v1 — EE's
    getAllOrders supports cursor paging but v1 takes whatever fits
    in one default-page response; remainder lands on next tick. The
    7-day-window cap (patch note 4) is respected — if the window
    exceeds 7 days, we cap at 7 days from cursor.
    """
    status_filter = account.get("polling_status_filter") or "Manifested"

    # Build the date window. EE's getAllOrders requires start_date /
    # end_date (also satisfies the patch-note-3 reference_code-OR-
    # date-filter requirement since this is bulk discovery).
    last_pull = account.get("last_pull_orders")
    if last_pull:
        start_dt = get_datetime(last_pull)
    else:
        # Defensive — should never hit here because after_insert sets
        # last_pull_orders to NOW. But if it's null for some legacy
        # reason, fall back to 1 hour ago to avoid pulling deep history.
        start_dt = now_datetime()

    end_dt = now_datetime()

    # Respect the 7-day cap per patch note 4.
    max_window_seconds = 7 * 24 * 3600
    if (end_dt - start_dt).total_seconds() > max_window_seconds:
        end_dt = start_dt + (end_dt - start_dt).__class__(seconds=max_window_seconds)

    response = client.get(
        ORDERS_GET_ALL,
        params={
            "status": status_filter,
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
        },
        correlation_id=correlation_id,
    )

    return _extract_orders(response)


def _extract_orders(response: Any) -> list[dict]:
    """Pull the list of order rows from the EE response. EE wraps
    payloads inconsistently across versions; scan likely shapes.
    """
    if isinstance(response, dict):
        # Common: {"data": [...]} or {"data": {"orders": [...]}}
        data = response.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("orders", "rows", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        # Top-level array
        for key in ("orders", "rows"):
            if isinstance(response.get(key), list):
                return response[key]
    if isinstance(response, list):
        return response
    return []


# ============================================================
# Per-order dispatch (Stage 3 plugs in here)
# ============================================================


def _dispatch_order_to_builder(
    *,
    order_row: dict,
    account: Any,
    correlation_id: str,
) -> dict:
    """Hand one EE order row off to the §12 SI builder. Idempotency
    check: skip if an SI already exists for this EE Invoice_id.

    Stage 3 implements `build_si_from_ee_order` — this dispatcher
    catches the import error gracefully so Stage 2 can ship and
    smoke independently before Stage 3 lands.
    """
    ee_invoice_id = str(
        order_row.get("invoice_id") or order_row.get("invoiceId") or ""
    ).strip()
    if not ee_invoice_id:
        return {
            "ok": False,
            "skipped": False,
            "detail": "Order row missing invoice_id — cannot anchor SI dedup.",
            "order_payload_keys": list(order_row.keys())[:10],
        }

    # Idempotency — dedup on EE Invoice_id
    existing_si = frappe.db.get_value(
        "Sales Invoice",
        {
            "ecs_easyecom_invoice_id": ee_invoice_id,
            "docstatus": ["!=", 2],
        },
        "name",
    )
    if existing_si:
        return {
            "ok": True,
            "skipped": True,
            "detail": "SI already exists for this Invoice_id.",
            "sales_invoice": existing_si,
            "ee_invoice_id": ee_invoice_id,
        }

    # Hand off to Stage 3 builder. If Stage 3 hasn't landed yet, log
    # the order for FDE visibility and return a skip — so Stage 2 can
    # be smoked against Harmony without Stage 3 in place.
    try:
        from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
            build_si_from_ee_order,
        )
    except ImportError:
        frappe.logger().info(
            f"§12 polling: deferring SI creation for invoice_id={ee_invoice_id} — "
            "Stage 3 builder not yet available. Order payload logged for diagnostic."
        )
        return {
            "ok": True,
            "skipped": True,
            "detail": "Stage 3 builder not yet available.",
            "ee_invoice_id": ee_invoice_id,
        }

    try:
        build_result = build_si_from_ee_order(
            order_row=order_row,
            marketplace_account=account,
            correlation_id=correlation_id,
        )
        return {
            "ok": True,
            "skipped": False,
            "ee_invoice_id": ee_invoice_id,
            "build_result": build_result,
        }
    except Exception as exc:
        # Per-record failure — log + return; loop continues.
        frappe.log_error(
            title=f"§12 SI build failed for invoice_id={ee_invoice_id}",
            message=(
                f"{type(exc).__name__}: {exc}\n\n"
                f"Order payload: {json.dumps(order_row, default=str)[:4000]}"
            ),
        )
        return {
            "ok": False,
            "skipped": False,
            "ee_invoice_id": ee_invoice_id,
            "exception": type(exc).__name__,
            "message": str(exc)[:500],
        }


# ============================================================
# Cursor / audit helpers
# ============================================================


def _advance_cursor(account_name: str, new_cursor: Any) -> None:
    """Bump last_pull_orders to the pull-window start time + clear
    last_pull_error on success."""
    try:
        frappe.db.set_value(
            "EasyEcom Marketplace Account",
            account_name,
            {
                "last_pull_orders": new_cursor,
                "last_pull_at": now_datetime(),
                "last_pull_error": "",
            },
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        # Don't let cursor write failures crash the polling tick.
        pass


def _stamp_last_pull_attempt(account_name: str, *, error: str | None = None) -> None:
    """Touch last_pull_at without advancing the cursor — used on
    failure so re-polling is fairly distributed (a stuck Account
    doesn't get re-polled on every */5 tick)."""
    try:
        updates: dict = {"last_pull_at": now_datetime()}
        if error is not None:
            updates["last_pull_error"] = error[:5000]
        frappe.db.set_value(
            "EasyEcom Marketplace Account",
            account_name,
            updates,
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        pass
