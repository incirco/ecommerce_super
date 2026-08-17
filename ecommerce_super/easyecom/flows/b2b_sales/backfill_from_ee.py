"""Generic B2B backfill from EE getAllOrders API.

PURPOSE

  Retrospectively create Sales Orders + Sales Invoices in ERPNext for
  B2B invoices that already exist on EE's side (typically because EE
  was in production before the ERP integration went live, or the
  client is onboarding mid-year and needs to see historical B2B
  transactions).

  Parameterized by (marketplace_account, invoice_date_start,
  invoice_date_end) — works for any client, any month.

WHY OPTION B (SO + invoice_mirror), NOT DIRECT SI

  Building SIs by hand for B2B means recreating all the tax split,
  addressing, GSTIN, multi-currency handling that ERPNext's
  make_sales_invoice() already does natively (source SO → SI copy).
  Instead we:

    1. Create a source SO that carries the correct customer, items,
       taxes, currency, addresses
    2. Submit the SO (skipping the EE-push hook via a flag — the
       invoice already exists on EE's side)
    3. Create EasyEcom B2B Order Map linking SO ↔ EE invoice_id
    4. Call invoice_mirror._mirror_invoice(map, ee_row) which uses
       ERPNext-native make_sales_invoice() to produce the SI

  This matches the LIVE flow exactly, minus the SO push — so the
  produced SIs are indistinguishable from live-flow SIs downstream
  (recon, GSTR-1, GL entries, everything).

PREREQUISITE — CUSTOMER MASTER

  A B2B invoice's `buyer_gst` must match an existing Customer's GSTIN
  before backfill can proceed. This module calls
  customer_pull.pull_customers() first (unless disabled) to ensure
  the customer master is fresh from EE. Invoices with unresolvable
  customers are counted separately and skipped — ops runs customer
  discovery manually or corrects the GSTIN on the ERP customer, then
  re-runs backfill (idempotent, safe).

IDEMPOTENCY

  Per invoice, checks if a Sales Invoice already exists with
  ecs_easyecom_invoice_id = <this_invoice_id>. If so, skip. Safe to
  re-run for the same window multiple times without duplication.

FAILURE ISOLATION

  Per-invoice try/except with savepoint. One bad invoice never fails
  the batch. Errors logged with the ee_invoice_id for later
  correction + resume via resume_from_invoice_id parameter.

INVOCATION

  Whitelisted for HTTP invocation. Recommended call pattern:

    dry-run first:
      POST /api/method/ecommerce_super.easyecom.flows.b2b_sales.
                       backfill_from_ee.backfill_b2b_from_ee_api
      body: {
        "marketplace_account": "ECS-MA-Puresta-B2B",
        "invoice_date_start": "2026-07-01",
        "invoice_date_end": "2026-07-31",
        "dry_run": true
      }

    then live:
      Same params with "dry_run": false
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import getdate

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDERS_GET_ALL


# --------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------


@frappe.whitelist()
def backfill_b2b_from_ee_api(
    *,
    marketplace_account: str,
    invoice_date_start: str,
    invoice_date_end: str,
    dry_run: str | bool = True,
    skip_customer_discovery: str | bool = False,
    resume_from_invoice_id: str | None = None,
) -> dict:
    """Backfill B2B SOs + SIs from EE's getAllOrders for a date window.

    Args:
        marketplace_account: name of the B2B EasyEcom Marketplace Account
        invoice_date_start: YYYY-MM-DD (inclusive)
        invoice_date_end: YYYY-MM-DD (inclusive)
        dry_run: True → no writes, just report what would happen
        skip_customer_discovery: True → skip the customer_pull step
            (use only when you know customer master is already fresh)
        resume_from_invoice_id: EE invoice_id to start from (skip
            all rows up to and including this one). Enables resume
            after partial-batch failure.

    Returns:
        Summary dict with counts + per-outcome details.
    """
    dry_run = _as_bool(dry_run)
    skip_customer_discovery = _as_bool(skip_customer_discovery)

    ma_doc = frappe.get_cached_doc(
        "EasyEcom Marketplace Account", marketplace_account
    )

    result: dict[str, Any] = {
        "marketplace_account": marketplace_account,
        "invoice_date_start": invoice_date_start,
        "invoice_date_end": invoice_date_end,
        "dry_run": dry_run,
        "customer_discovery": None,
        "invoices_pulled": 0,
        "created_sos": 0,
        "created_sis": 0,
        "skipped_already_exists": 0,
        "skipped_no_customer": 0,
        "skipped_no_items": 0,
        "errors": 0,
        "error_details": [],
    }

    # Step 1: refresh customer master (unless skipped)
    if not skip_customer_discovery:
        result["customer_discovery"] = _refresh_customer_master(
            ma_doc, dry_run=dry_run
        )

    # Step 2: pull B2B invoices from EE for the window
    invoices = _fetch_b2b_invoices(
        ma_doc,
        invoice_date_start=invoice_date_start,
        invoice_date_end=invoice_date_end,
    )
    result["invoices_pulled"] = len(invoices)

    # Apply resume filter
    if resume_from_invoice_id:
        invoices = _apply_resume_filter(invoices, resume_from_invoice_id)
        result["invoices_after_resume_filter"] = len(invoices)

    # Step 3: process each invoice
    for ee_row in invoices:
        if not dry_run:
            frappe.db.savepoint("b2b_backfill_invoice")
        try:
            outcome = _process_one_invoice(
                ee_row=ee_row,
                ma_doc=ma_doc,
                dry_run=dry_run,
            )
            _bump_counter(result, outcome)
            if not dry_run:
                # Commit each successful invoice so a later failure
                # doesn't roll back earlier good work — and so a
                # gateway/worker timeout leaves everything up to now
                # already persisted (idempotency dedupes on re-run).
                frappe.db.commit()
        except Exception as exc:
            result["errors"] += 1
            result["error_details"].append({
                "invoice_id": ee_row.get("invoice_id"),
                "invoice_number": ee_row.get("invoice_number"),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            frappe.logger().warning(
                f"[b2b_backfill] invoice_id={ee_row.get('invoice_id')} "
                f"failed: {type(exc).__name__}: {exc}"
            )
            if not dry_run:
                try:
                    frappe.db.rollback(save_point="b2b_backfill_invoice")
                except Exception:
                    # Defensive — a savepoint may have been consumed
                    # by an intermediate commit inside the failing
                    # invoice's code path (e.g. SO submit auto-commits
                    # in some ERPNext paths). Fall through so the
                    # batch continues.
                    pass

    return result


# --------------------------------------------------------------
# Step 1 — customer master refresh
# --------------------------------------------------------------


def _location_key(ma_doc: Any) -> str:
    """Resolve the bare location_key string for the MA's EE Account.

    ORDERS_GET_ALL is a non-foundational endpoint — client.py:240-241
    only adds the Bearer header when self.location_key is set. Without
    it, EE returns 401. Foundational endpoints (customer pull, item
    pull) fall through to a different path that auto-resolves from the
    account, which is why they work with EasyEcomClient(company=X).

    Two-step deref matches polling.py:151-169:
      1. EasyEcom Account.default_location_key → EE Location docname
      2. EasyEcom Location.location_key → bare location_key string
    """
    if not ma_doc.easyecom_account:
        raise ValueError(
            f"Marketplace Account {ma_doc.name} has no easyecom_account"
        )
    loc_docname = frappe.db.get_value(
        "EasyEcom Account", ma_doc.easyecom_account, "default_location_key"
    )
    if not loc_docname:
        raise ValueError(
            f"EasyEcom Account {ma_doc.easyecom_account!r} has no "
            f"default_location_key configured"
        )
    key = frappe.db.get_value(
        "EasyEcom Location", loc_docname, "location_key"
    )
    if not key:
        raise ValueError(
            f"EasyEcom Location {loc_docname!r} has no location_key field"
        )
    return key


def _refresh_customer_master(ma_doc: Any, *, dry_run: bool) -> dict:
    """Pull latest B2B customers from EE via the standard discovery flow.

    Uses customer_pull.pull_customers which fetches
    /Wholesale/v2/UserManagement and creates/updates ERPNext Customer
    records. This is the same flow the daily 05:30 IST cron runs; we
    invoke it on demand so a fresh backfill starts with a fresh master.
    """
    if dry_run:
        return {"skipped_dry_run": True}

    from ecommerce_super.easyecom.flows.customer_pull import pull_customers
    try:
        client = EasyEcomClient(location_key=_location_key(ma_doc))
    except Exception as exc:
        return {"error": f"client init: {type(exc).__name__}: {exc}"}
    outcome = pull_customers(client=client)
    return {
        "created": getattr(outcome, "created", 0),
        "updated": getattr(outcome, "updated", 0),
        "skipped": getattr(outcome, "skipped", 0),
        "errors": getattr(outcome, "errors", 0),
    }


# --------------------------------------------------------------
# Step 2 — fetch B2B invoices from EE
# --------------------------------------------------------------


def _fetch_b2b_invoices(
    ma_doc: Any, *, invoice_date_start: str, invoice_date_end: str,
) -> list[dict]:
    """Poll getAllOrders with invoice_start_date/invoice_end_date +
    filter to B2B (order_type_key == 'businessorder').

    EE caps getAllOrders windows at 7 days (same cap b2c polling
    enforces at polling.py:289). Chunk the requested invoice-date
    range into 7-day sub-windows and dedup across them.
    """
    from datetime import date, timedelta

    client = EasyEcomClient(location_key=_location_key(ma_doc))

    invoices: list[dict] = []
    seen_ids: set[str] = set()

    start = date.fromisoformat(invoice_date_start)
    end = date.fromisoformat(invoice_date_end)
    window_days = 7

    cursor = start
    while cursor <= end:
        w_end = min(cursor + timedelta(days=window_days - 1), end)
        for page in client.paginated(
            ORDERS_GET_ALL,
            params={
                "invoice_start_date": f"{cursor.isoformat()}T00:00:00",
                "invoice_end_date": f"{w_end.isoformat()}T23:59:59",
                "get_batch_codes": 0,
            },
            max_pages=500,
        ):
            rows = _extract_rows(page)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                invoice_id = str(row.get("invoice_id") or "")
                if not invoice_id or invoice_id in seen_ids:
                    continue
                if (row.get("order_type_key") or "").lower() != "businessorder":
                    continue
                if not (row.get("invoice_number") or "").strip():
                    continue
                seen_ids.add(invoice_id)
                invoices.append(row)
        cursor = w_end + timedelta(days=1)

    return invoices


def _extract_rows(resp: dict) -> list:
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


def _apply_resume_filter(invoices: list[dict], resume_from: str) -> list[dict]:
    """Skip everything up to and including the resume_from invoice_id."""
    resume_from = str(resume_from)
    idx = next(
        (i for i, r in enumerate(invoices)
         if str(r.get("invoice_id")) == resume_from),
        None,
    )
    if idx is None:
        return invoices
    return invoices[idx + 1:]


# --------------------------------------------------------------
# Step 3 — per-invoice processing
# --------------------------------------------------------------


def _process_one_invoice(
    *, ee_row: dict, ma_doc: Any, dry_run: bool,
) -> str:
    """Backfill one B2B invoice. Returns outcome key.

    Wrapped by the caller in a savepoint for rollback on failure.
    """
    invoice_id = str(ee_row.get("invoice_id"))

    # Idempotency — already backfilled?
    if _si_exists_for_ee_invoice_id(invoice_id):
        return "skipped_already_exists"

    # Resolve Customer
    customer = _resolve_customer(ee_row, ma_doc)
    if not customer:
        return "skipped_no_customer"

    # Resolve line items
    items = _resolve_line_items(ee_row)
    if not items:
        return "skipped_no_items"

    if dry_run:
        return "created_sis"  # would-be outcome — surfaces to counts

    # Create + submit Sales Order (with push suppressed)
    with _push_suppressed():
        so = _create_and_submit_so(
            ee_row=ee_row,
            customer=customer,
            items=items,
            ma_doc=ma_doc,
        )

    # Create the B2B Order Map linking SO ↔ ee_invoice_id
    map_doc = _create_b2b_order_map(
        so=so, ee_row=ee_row, ma_doc=ma_doc,
    )

    # Delegate to the live-flow mirror to produce the SI
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        _mirror_invoice,
    )
    _mirror_invoice(map_doc=map_doc, ee_row=ee_row)

    return "created_sis"


# --------------------------------------------------------------
# Customer + item resolution
# --------------------------------------------------------------


def _resolve_customer(ee_row: dict, ma_doc: Any) -> str | None:
    """Look up an ERPNext Customer whose GSTIN matches the EE row's
    buyer_gst. URP / blank / NA buyers → no match (B2B needs GSTIN).
    """
    buyer_gst = (ee_row.get("buyer_gst") or "").strip()
    if not buyer_gst or buyer_gst.upper() in {"URP", "NA", "N/A"}:
        return None
    # Look via the Customer.gstin field (India Compliance-standard)
    customer = frappe.db.get_value(
        "Customer",
        {"gstin": buyer_gst},
        "name",
    )
    return customer


def _resolve_line_items(ee_row: dict) -> list[dict]:
    """EE suborder/order_items → ERPNext SO Item dicts. Uses
    EasyEcom Item Map for the SKU → Item name lookup."""
    rows = (
        ee_row.get("suborders")
        or ee_row.get("order_items")
        or ee_row.get("orderItems")
        or []
    )
    items: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sku = (r.get("sku") or r.get("SKU") or "").strip()
        if not sku:
            continue
        item_code = frappe.db.get_value(
            "EasyEcom Item Map", {"ee_sku": sku}, "erpnext_name",
        )
        if not item_code:
            # SKU unmapped — skip row. Bulk-mapping errors surface
            # via the caller returning "skipped_no_items" if all
            # rows are unmapped.
            continue
        qty = float(r.get("item_quantity") or r.get("quantity") or 0)
        if qty <= 0:
            continue
        selling_price = float(r.get("selling_price") or 0)
        rate = selling_price / qty if qty else 0
        items.append({
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
        })
    return items


# --------------------------------------------------------------
# SO creation
# --------------------------------------------------------------


def _create_and_submit_so(
    *, ee_row: dict, customer: str, items: list[dict], ma_doc: Any,
) -> Any:
    """Sales Order dict → insert → submit. Push hook is suppressed by
    caller via _push_suppressed context manager."""
    order_date = (ee_row.get("order_date") or "")[:10] or (
        ee_row.get("invoice_date") or ""
    )[:10]
    invoice_date = (ee_row.get("invoice_date") or "")[:10]

    so_doc = {
        "doctype": "Sales Order",
        "customer": customer,
        "company": ma_doc.company,
        "transaction_date": order_date,
        "delivery_date": invoice_date,  # invoice date works as a proxy
        "currency": (ee_row.get("invoice_currency_code") or "INR").upper(),
        "conversion_rate": 1,  # foreign currency handled by later PR
        "items": [
            {
                "item_code": it["item_code"],
                "qty": it["qty"],
                "rate": it["rate"],
                "delivery_date": invoice_date,
            }
            for it in items
        ],
    }
    if getattr(ma_doc, "warehouse", None):
        so_doc["set_warehouse"] = ma_doc.warehouse

    so = frappe.get_doc(so_doc)
    so.flags.ignore_permissions = True
    so.insert()
    so.submit()
    return so


def _create_b2b_order_map(
    *, so: Any, ee_row: dict, ma_doc: Any,
) -> Any:
    """Create a B2B Order Map — enough for invoice_mirror to find
    everything it needs. Status set to 'Invoice Generated' since we
    know the SI is about to be minted."""
    map_doc = frappe.get_doc({
        "doctype": "EasyEcom B2B Order Map",
        "sales_order": so.name,
        "easyecom_account": ma_doc.easyecom_account,
        "module": "New B2B",  # invoice_mirror expects this for backfill flow
        "status": "Invoice Generated",
        "ee_invoice_id": str(ee_row.get("invoice_id") or ""),
        "ee_invoice_number": ee_row.get("invoice_number") or "",
        "ee_order_id": str(ee_row.get("order_id") or ""),
        "pushed_at": frappe.utils.now_datetime(),
    })
    map_doc.flags.ignore_permissions = True
    map_doc.insert()
    return map_doc


# --------------------------------------------------------------
# Helpers
# --------------------------------------------------------------


def _si_exists_for_ee_invoice_id(invoice_id: str) -> bool:
    return bool(frappe.db.exists(
        "Sales Invoice", {"ecs_easyecom_invoice_id": invoice_id}
    ))


# NOTE — no _location_key() helper. EasyEcomClient(company=...)
# resolves the location + credentials internally (see client.py:245-254).
# Matches the pattern used by customer_pull / item_pull / other flows.
# Adding another indirection here would just duplicate what the client
# already does correctly.


def _as_bool(v: Any) -> bool:
    """Whitelisted method receives str "true"/"false" via HTTP form."""
    if isinstance(v, bool):
        return v
    return str(v).lower() in {"true", "1", "yes"}


def _bump_counter(result: dict, outcome: str) -> None:
    if outcome == "created_sis":
        result["created_sis"] += 1
        result["created_sos"] += 1
    else:
        result[outcome] = result.get(outcome, 0) + 1


@contextmanager
def _push_suppressed():
    """Set flag → SO.on_submit push hook returns early → no push queued."""
    flags = getattr(frappe.local, "flags", None)
    if flags is None:
        yield
        return
    prev = flags.get("easyecom_b2b_backfill_in_flight")
    flags.easyecom_b2b_backfill_in_flight = True
    try:
        yield
    finally:
        if prev is None:
            flags.easyecom_b2b_backfill_in_flight = False
        else:
            flags.easyecom_b2b_backfill_in_flight = prev
