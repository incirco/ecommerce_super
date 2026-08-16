"""§12 — B2C pending-manifest sweeper.

Transitions Draft B2C Sales Invoices to their terminal state based on
periodic re-check against EE:

  Draft SI (invoice raised, manifest_date NULL)
    │
    ├─ EE now shows manifest_date populated + order_status Shipped/Returned
    │      → submit the SI (fires GL + stock ledger)
    │      → update Shipment.manifest_date
    │      → if Returned: additionally create paired Credit Note
    │
    ├─ EE now shows order_status = Cancelled (cancelled putaway)
    │      → branch by SI.gst_category:
    │           Unregistered (URP / B2C) → submit + immediately cancel
    │             (docstatus 0 → 1 → 2). GSTR-1 excludes docstatus=2.
    │             Matches CA-approved treatment when buyer never
    │             received the invoice PDF and has no ITC claim.
    │           Registered Regular → submit + create Credit Note pair.
    │             Both docs remain in GSTR-1 (B2B invoice + credit note),
    │             buyer can reverse their ITC. Full GST compliance.
    │
    ├─ EE still shows Confirmed / Manifest Scanned (transient state)
    │      → no-op, re-check next cycle
    │
    └─ EE returns nothing / error
           → log warning, retry next cycle

DESIGN CHOICES

  1. Sweeper reads pending drafts from EasyEcom Order Map Shipment (child
     of Marketplace Order Map — PR #272). Shipment.manifest_date IS NULL
     is the canonical "still pending" signal. The parent Map plus its SI
     back-reference lets us reach the SI directly without scanning
     tabSales Invoice.

  2. Per-invoice EE re-fetch uses getAllOrders with invoice_id filter —
     one API call per Draft SI. At Puresta's scale (30-90 pending drafts
     at any time) this is a few dozen calls per cron tick. If the volume
     grows we can batch by (marketplace_account, invoice_date window).

  3. Idempotency: rely on docstatus + Shipment.manifest_date as the
     state machine. Sweeper is safe to run every N minutes without
     duplicate side-effects — a Submitted SI is skipped; a Cancelled SI
     is skipped; a Draft SI whose EE state hasn't changed is a no-op.

  4. Failure isolation: exceptions on one SI never fail the sweep. Each
     branch wrapped in try/except, log-and-continue.

  5. Draft-first policy (SPEC amendment PR #272-adjacent): SIs are born
     as Drafts by the b2c invoice_builder. This sweeper is the ONLY
     path that submits a B2C SI in the standard flow. Backfill has its
     own path (see backfill_mode in invoice_builder).
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDERS_GET_ALL
from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
    _build_credit_note_for_reversed_order,
)


# Statuses that mean "physically manifested = OK to submit as regular sale"
_MANIFESTED_STATUSES = {"Shipped", "Confirmed", "Manifest Scanned"}
_RETURNED_STATUSES = {"Returned"}
_CANCELLED_STATUSES = {"Cancelled"}


# ============================================================
# Scheduler entry point
# ============================================================


def sweep_pending_manifest_sis(
    *,
    marketplace_account: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Scan all pending-manifest Draft SIs, transition each based on the
    current EE state. Called from hourly scheduler_events.

    Args:
        marketplace_account: optional filter to sweep one MA only (used
            by ad-hoc triggers from the desk). None = sweep all.
        dry_run: log what would happen without actually mutating docs.
            Useful for manual verification before flipping the cron on.

    Returns:
        {'checked': N, 'submitted': N, 'cancelled': N, 'credit_note': N,
         'still_pending': N, 'errors': N}
    """
    pending_rows = _find_pending_shipments(marketplace_account)
    result = {
        "checked": len(pending_rows),
        "submitted": 0,
        "cancelled": 0,
        "credit_note": 0,
        "still_pending": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    for row in pending_rows:
        try:
            outcome = _process_one_pending(row, dry_run=dry_run)
            result[outcome] = result.get(outcome, 0) + 1
        except Exception as exc:
            result["errors"] += 1
            frappe.logger().warning(
                f"[pending-manifest-sweeper] SI {row.get('sales_invoice')} "
                f"failed: {type(exc).__name__}: {exc}"
            )
    return result


# ============================================================
# Query — find candidates
# ============================================================


def _find_pending_shipments(marketplace_account: str | None) -> list[dict]:
    """Draft SIs whose Marketplace Order Map Shipment has NULL manifest_date.
    Joined query — no per-row iteration needed to filter.
    """
    filters = ["si.docstatus = 0", "sh.manifest_date IS NULL"]
    params: dict[str, Any] = {}
    if marketplace_account:
        filters.append("mom.marketplace_account = %(ma)s")
        params["ma"] = marketplace_account

    where_clause = " AND ".join(filters)
    rows = frappe.db.sql(
        f"""
        SELECT
            si.name                             AS sales_invoice,
            si.gst_category                     AS gst_category,
            si.company                          AS company,
            mom.name                            AS mp_order_map,
            mom.marketplace_account             AS marketplace_account,
            mom.marketplace_order_id            AS marketplace_order_id,
            sh.name                             AS shipment_row,
            sh.invoice_id                       AS ee_invoice_id,
            sh.invoice_number                   AS invoice_number
        FROM `tabSales Invoice` si
        JOIN `tabEasyEcom Order Map Shipment` sh
             ON sh.sales_invoice = si.name
        JOIN `tabEasyEcom Marketplace Order Map` mom
             ON mom.name = sh.parent
        WHERE {where_clause}
        """,
        params,
        as_dict=True,
    )
    return rows


# ============================================================
# Per-row processing
# ============================================================


def _process_one_pending(row: dict, *, dry_run: bool) -> str:
    """Fetch the current EE state for this SI's invoice_id, branch to
    the right transition. Returns one of the result-dict keys.
    """
    ee_row = _fetch_ee_order(row)
    if not ee_row:
        return "still_pending"

    status = (ee_row.get("order_status") or "").strip()
    manifest_date = (ee_row.get("manifest_date") or "").strip()

    # -- Cancelled putaway path --
    if status in _CANCELLED_STATUSES:
        return _handle_cancelled(row, ee_row, dry_run=dry_run)

    # -- Manifested path --
    if manifest_date and status in (_MANIFESTED_STATUSES | _RETURNED_STATUSES):
        return _handle_manifested(row, ee_row, status, manifest_date, dry_run=dry_run)

    # -- Still transient (Confirmed / Manifest Scanned without manifest_date) --
    return "still_pending"


def _handle_manifested(
    row: dict,
    ee_row: dict,
    status: str,
    manifest_date: str,
    *,
    dry_run: bool,
) -> str:
    """Manifest arrived. Submit the SI. If Returned, also create paired CN."""
    si_name = row["sales_invoice"]
    if dry_run:
        frappe.logger().info(
            f"[pending-manifest-sweeper] DRY-RUN would submit {si_name} "
            f"(status={status}, manifest_date={manifest_date})"
        )
        return "submitted"

    si = frappe.get_doc("Sales Invoice", si_name)
    # Update Shipment child's manifest_date on the Map before submit —
    # keeps the recon-visible view consistent even if submit fails.
    _update_shipment_manifest_date(row["shipment_row"], manifest_date)

    si.submit()
    frappe.db.commit()

    if status in _RETURNED_STATUSES:
        # Post-manifest return — dispatched then came back. Pair with CN.
        _create_paired_credit_note(si, ee_row)
        return "credit_note"
    return "submitted"


def _handle_cancelled(row: dict, ee_row: dict, *, dry_run: bool) -> str:
    """Cancelled putaway (or post-manifest cancel). Branch by GST reg:
      Unregistered → submit + cancel (docstatus 0 → 1 → 2)
      Registered Regular → submit + create Credit Note

    Both branches ensure every EE invoice_number has a persistent
    ERPNext record (either cancelled SI, or SI + CN pair). No invoice
    number goes silently missing from ERP.
    """
    si_name = row["sales_invoice"]
    gst_category = (row.get("gst_category") or "").strip()

    if dry_run:
        frappe.logger().info(
            f"[pending-manifest-sweeper] DRY-RUN would handle Cancelled "
            f"{si_name} (gst_category={gst_category})"
        )
        return "cancelled" if gst_category != "Registered Regular" else "credit_note"

    si = frappe.get_doc("Sales Invoice", si_name)

    if gst_category == "Registered Regular":
        # Buyer has GSTIN → they may have claimed ITC. Formal CN required.
        si.submit()
        frappe.db.commit()
        _create_paired_credit_note(si, ee_row)
        return "credit_note"

    # URP / B2C — no GSTIN, no ITC claim. Submit then cancel; GSTR-1
    # will automatically exclude the cancelled SI (India Compliance
    # queries docstatus=1 only).
    si.submit()
    frappe.db.commit()
    si.cancel()
    frappe.db.commit()
    return "cancelled"


# ============================================================
# EE re-fetch
# ============================================================


def _fetch_ee_order(row: dict) -> dict | None:
    """Fetch current state of one invoice via EE getAllOrders using
    invoice_id filter. Returns None on any error (sweeper retries
    next cycle).
    """
    ma_name = row.get("marketplace_account")
    ee_invoice_id = row.get("ee_invoice_id")
    if not (ma_name and ee_invoice_id):
        return None
    try:
        ma = frappe.get_cached_doc("EasyEcom Marketplace Account", ma_name)
        client = EasyEcomClient(location_key=_location_key_for(ma))
        resp = client.get(ORDERS_GET_ALL, params={
            "invoice_id": int(ee_invoice_id),
            "get_batch_codes": 0,
        })
        orders = _extract_orders(resp)
        return orders[0] if orders else None
    except Exception as exc:
        frappe.logger().warning(
            f"[pending-manifest-sweeper] EE fetch failed for "
            f"invoice_id={ee_invoice_id} on MA {ma_name}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None


def _location_key_for(marketplace_account: Any) -> str:
    """Marketplace Account → EE location_key via easyecom_account.
    Cached — the walk chain is stable per site."""
    ee_account_name = marketplace_account.easyecom_account
    ee_account = frappe.get_cached_doc("EasyEcom Account", ee_account_name)
    return ee_account.default_location_key


def _extract_orders(resp: dict) -> list[dict]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("orders", "rows", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


# ============================================================
# Shipment-child update helpers
# ============================================================


def _update_shipment_manifest_date(shipment_name: str, manifest_date: str) -> None:
    """Persist the manifest_date onto the Shipment child row. Uses db_set
    to avoid re-triggering parent Map validation on a field update."""
    frappe.db.set_value(
        "EasyEcom Order Map Shipment",
        shipment_name,
        "manifest_date",
        manifest_date[:19],
    )


def _create_paired_credit_note(si: Any, ee_row: dict) -> None:
    """Delegate to the existing CN builder in invoice_builder.py. That
    function already handles negative-qty items + tax mirroring +
    ecs_marketplace_order_id back-ref + posting_date derivation."""
    try:
        _build_credit_note_for_reversed_order(
            original_si=si,
            order_row=ee_row,
        )
    except Exception as exc:
        # CN creation failure is non-fatal — sweep retries next cycle.
        frappe.logger().warning(
            f"[pending-manifest-sweeper] Credit Note creation failed for "
            f"SI {si.name}: {type(exc).__name__}: {exc}"
        )
