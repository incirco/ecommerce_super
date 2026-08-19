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
    order_type_keys: str | list[str] | None = None,
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
        order_type_keys: EE order_type_key values to include. Defaults
            to ["businessorder"]. Pass ["businessorder",
            "stocktransferorder"] to include STNs (internal warehouse
            transfers) which land as SIs with is_internal_customer=1
            + Non-GST template.

    Returns:
        Summary dict with counts + per-outcome details.
    """
    dry_run = _as_bool(dry_run)
    skip_customer_discovery = _as_bool(skip_customer_discovery)
    # HTTP form values arrive as JSON strings for list params
    if isinstance(order_type_keys, str):
        order_type_keys = frappe.parse_json(order_type_keys)
    order_type_keys = order_type_keys or ["businessorder"]

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
        order_type_keys=order_type_keys,
    )
    result["invoices_pulled"] = len(invoices)

    # Apply resume filter
    if resume_from_invoice_id:
        invoices = _apply_resume_filter(invoices, resume_from_invoice_id)
        result["invoices_after_resume_filter"] = len(invoices)

    # Import for the variance-tolerant branch below
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        InvoiceMirrorVariance,
    )

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
        except InvoiceMirrorVariance as exc:
            # Variance is a warning, not a failure — per mirror's own
            # design ("Sales Invoice was created (in Draft) but flagged
            # for FDE review"). Keep the SI, count it, log the variance.
            _bump_counter(result, "created_with_variance")
            result.setdefault("variance_details", []).append({
                "invoice_id": ee_row.get("invoice_id"),
                "invoice_number": ee_row.get("invoice_number"),
                "variance": str(exc)[:400],
            })
            if not dry_run:
                frappe.db.commit()
            continue
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


def _pick_item_tax_template(company_abbr: str, tax_rate: float) -> str | None:
    """Find Item Tax Template matching this GST rate for the company.
    Convention: 'GST {int_or_float_rate}% - {abbr}' (e.g. 'GST 18% - HQL').
    Returns None if no match — caller falls back to letting IC's auto-
    populate handle it, which will produce +tax_rate% variance visible
    in the mirror's variance check.
    """
    if not tax_rate:
        return None
    # Prefer integer-rate name (most templates ship this way)
    for candidate in (
        f"GST {int(tax_rate)}% - {company_abbr}",
        f"GST {tax_rate:g}% - {company_abbr}",
    ):
        if frappe.db.exists("Item Tax Template", candidate):
            return candidate
    return None


def _pick_sales_tax_template(
    company: str, company_abbr: str, customer: str,
    ee_row: dict | None = None,
) -> str | None:
    """Pick In-state vs Out-state Sales Taxes and Charges Template.
    First tries GSTIN state comparison; falls back to billing_state
    name comparison from ee_row (for URP customers without GSTIN).
    Convention: 'Output GST In-state - {abbr}' / 'Output GST Out-state - {abbr}'.
    """
    company_gstin = frappe.db.get_value("Company", company, "gstin") or ""
    if not company_gstin:
        return None

    customer_gstin = frappe.db.get_value("Customer", customer, "gstin") or ""
    if customer_gstin:
        is_intra_state = company_gstin[:2] == customer_gstin[:2]
    elif ee_row:
        # URP fallback: compare billing_state name to pickup_state (which
        # is the company's state per the EE payload for the shipping-
        # origin warehouse). Match = in-state (CGST+SGST), else Out-state (IGST).
        billing_state = (ee_row.get("billing_state") or "").strip().lower()
        pickup_state = (ee_row.get("pickup_state") or "").strip().lower()
        if not billing_state or not pickup_state:
            return None
        is_intra_state = billing_state == pickup_state
    else:
        return None

    template = (
        f"Output GST In-state - {company_abbr}" if is_intra_state
        else f"Output GST Out-state - {company_abbr}"
    )
    if frappe.db.exists("Sales Taxes and Charges Template", template):
        return template
    return None


def _default_warehouse(ma_doc: Any) -> str | None:
    """Resolve the ERPNext Warehouse mapped to the MA's default EE
    Location. Returns None if the mapping doesn't exist — caller
    decides whether to fall back or raise.

    Chain: MA.easyecom_account → EE Account.default_location_key
    → EE Location.mapped_warehouse.
    """
    if not ma_doc.easyecom_account:
        return None
    loc_docname = frappe.db.get_value(
        "EasyEcom Account", ma_doc.easyecom_account, "default_location_key"
    )
    if not loc_docname:
        return None
    return frappe.db.get_value(
        "EasyEcom Location", loc_docname, "mapped_warehouse"
    )


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
    order_type_keys: list[str] | None = None,
) -> list[dict]:
    """Poll getAllOrders with invoice_start_date/invoice_end_date +
    filter to order_type_keys (default: 'businessorder').

    EE caps getAllOrders windows at 7 days (same cap b2c polling
    enforces at polling.py:289). Chunk the requested invoice-date
    range into 7-day sub-windows and dedup across them.
    """
    from datetime import date, timedelta

    allowed = {k.lower() for k in (order_type_keys or ["businessorder"])}

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
                if (row.get("order_type_key") or "").lower() not in allowed:
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
        mirror_si_from_ee_response,
    )
    result = mirror_si_from_ee_response(map_doc=map_doc, ee_row=ee_row)
    # Wire the Map → SI back-reference (mirror returns si.name but
    # doesn't touch the Map; downstream recon relies on this link).
    si_name = (result or {}).get("sales_invoice")
    if si_name:
        map_doc.db_set("sales_invoice", si_name, update_modified=False)

    return "created_sis"


# --------------------------------------------------------------
# Customer + item resolution
# --------------------------------------------------------------


def _resolve_customer(ee_row: dict, ma_doc: Any) -> str | None:
    """Return a Customer name for the EE row's buyer.

    Resolution chain (first match wins):
      1. GSTIN lookup (India Compliance standard) — for buyers with a
         valid Indian GSTIN that matches an existing Customer.
      2. ecs_ee_customer_code lookup — for buyers we auto-created on a
         previous backfill run (idempotency).
      3. Auto-create a Customer using the EE row (URP domestic,
         Overseas, self-invoicing / STN internal). Placeholder name;
         ops renames later if desired.

    Only returns None if the EE row lacks a customer_code AND doesn't
    match anything on GSTIN (shouldn't happen for real EE data).
    Filters `disabled=0` on GSTIN lookup — disabled Customer would
    fail SO validation with PartyDisabled anyway.
    """
    buyer_gst = (ee_row.get("buyer_gst") or "").strip()
    company_gstin = (
        frappe.db.get_value("Company", ma_doc.company, "gstin") or ""
    ).strip()

    # 1. GSTIN lookup — skip when GSTIN is URP-like OR matches company
    # (self-invoicing needs the internal-customer path below).
    is_urp_like = (
        not buyer_gst or buyer_gst.upper() in {"URP", "NA", "N/A"}
    )
    is_self = bool(buyer_gst) and buyer_gst == company_gstin
    if not is_urp_like and not is_self:
        by_gst = frappe.db.get_value(
            "Customer",
            {"gstin": buyer_gst, "disabled": 0},
            "name",
        )
        if by_gst:
            return by_gst

    # 2. ecs_ee_customer_code lookup — idempotency for auto-created
    code = ee_row.get("customer_code")
    if code:
        by_code = frappe.db.get_value(
            "Customer",
            {"ecs_ee_customer_code": code, "disabled": 0},
            "name",
        )
        if by_code:
            return by_code

    # 3. Auto-create
    return _create_customer_from_ee(ee_row, ma_doc, company_gstin=company_gstin)


def _create_customer_from_ee(
    ee_row: dict, ma_doc: Any, *, company_gstin: str,
) -> str | None:
    """Auto-create a Customer from EE row data.

    Discriminates by billing_country + buyer_gst:
      - buyer_gst == company_gstin → internal (is_internal_customer=1),
        Non-GST style, represented_company=<same>
      - billing_country != India → Overseas
      - Indian + URP/blank buyer_gst → Unregistered
      - Indian + valid buyer_gst → Registered Regular (rare here —
        would normally match by GSTIN in resolver step 1)

    buyer_name is redacted by EE's getAllOrders for PII, so name
    is derived from customer_code + billing_city + billing_country.
    Ops can rename via the desk after backfill.
    """
    code = ee_row.get("customer_code")
    if not code:
        return None  # No stable dedup key — refuse to create

    billing_country = (
        ee_row.get("billing_country") or ee_row.get("country") or ""
    ).strip()
    billing_city = (
        ee_row.get("billing_city") or ee_row.get("city") or ""
    ).strip()
    buyer_gst = (ee_row.get("buyer_gst") or "").strip()

    is_self = bool(buyer_gst) and buyer_gst == company_gstin
    is_overseas = billing_country and billing_country.lower() != "india"

    if is_self:
        gst_category = "Registered Regular"  # will use Non-GST template downstream
        name_prefix = "EE Internal Transfer Customer"
        is_internal = 1
    elif is_overseas:
        gst_category = "Overseas"
        name_prefix = "EE Overseas B2B Customer"
        is_internal = 0
    elif not buyer_gst or buyer_gst.upper() in {"URP", "NA", "N/A"}:
        gst_category = "Unregistered"
        name_prefix = "EE URP B2B Customer"
        is_internal = 0
    else:
        # Valid Indian GSTIN but no existing Customer — create fresh
        gst_category = "Registered Regular"
        name_prefix = "EE B2B Customer"
        is_internal = 0

    parts = [name_prefix, str(code)]
    if billing_city:
        parts.append(f"({billing_city}")
        if billing_country:
            parts[-1] += f", {billing_country})"
        else:
            parts[-1] += ")"
    customer_name = " ".join(parts)

    cust = {
        "doctype": "Customer",
        "customer_name": customer_name,
        "customer_type": "Company",
        "customer_group": "Commercial",
        "territory": "India" if not is_overseas else "All Territories",
        "gst_category": gst_category,
        "disabled": 0,
        "ecs_ee_customer_code": code,
    }
    if is_internal:
        cust["is_internal_customer"] = 1
        cust["represented_company"] = ma_doc.company
        # ERPNext requires the "Allowed To Transact With" child table
        # populated when is_internal_customer=1, else SO validate throws
        # ValidationError. Add the current MA's company.
        cust["companies"] = [{"company": ma_doc.company}]
    # Only stamp gstin on Indian Registered Regular — Overseas can hold
    # a foreign non-GSTIN identifier without IC checksum failure
    if gst_category == "Registered Regular" and buyer_gst and not is_self:
        cust["gstin"] = buyer_gst

    # Overseas customers transacting in a foreign currency need a
    # Party Account (child: Customer.accounts) matching the invoice
    # currency, else ERPNext throws "Party Account ... currency (INR)
    # and document currency (XXX) should be same".
    if is_overseas:
        currency = (ee_row.get("invoice_currency_code") or "INR").upper()
        if currency != "INR":
            debtor = _ensure_foreign_debtor(ma_doc.company, currency)
            if debtor:
                cust["accounts"] = [
                    {"company": ma_doc.company, "account": debtor}
                ]
            # ERPNext's Party Account currency check: Debtor's currency
            # must match either customer.default_currency ("billing
            # currency") or Company.default_currency. Pin customer's
            # default to the invoice currency so the check passes.
            cust["default_currency"] = currency

    doc = frappe.get_doc(cust)
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.insert()
    return doc.name


def _ensure_foreign_debtor(company: str, currency: str) -> str | None:
    """Return an existing Receivable account for (company, currency),
    else create one alongside the company's default INR Debtor.

    Naming convention: '<parent_name> {CUR} - <abbr>' e.g.
    'Debtors CAD - HQL'. Auto-created under the same parent group as
    the default Debtor. Idempotent.
    """
    # Find any existing receivable account with matching currency
    existing = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "account_type": "Receivable",
            "account_currency": currency,
            "is_group": 0,
        },
        "name",
    )
    if existing:
        return existing

    # Auto-create — anchor to the company's default receivable account's parent
    default_debtor = frappe.db.get_value(
        "Company", company, "default_receivable_account"
    )
    if not default_debtor:
        default_debtor = frappe.db.get_value(
            "Account",
            {"company": company, "account_type": "Receivable", "is_group": 0},
            "name",
        )
    if not default_debtor:
        return None
    parent_account, root_name = frappe.db.get_value(
        "Account", default_debtor, ["parent_account", "account_name"]
    )
    abbr = frappe.get_cached_value("Company", company, "abbr")
    new_name = f"{root_name} {currency} - {abbr}"
    if frappe.db.exists("Account", new_name):
        return new_name
    acc = frappe.get_doc({
        "doctype": "Account",
        "account_name": f"{root_name} {currency}",
        "parent_account": parent_account,
        "company": company,
        "account_type": "Receivable",
        "account_currency": currency,
        "is_group": 0,
    })
    acc.flags.ignore_permissions = True
    acc.insert()
    return acc.name


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
        # EE's `selling_price` is the tax-INCLUSIVE line total (India
        # B2B convention). Store the tax-inclusive per-unit rate + the
        # per-line tax_rate; the SO builder decides whether to back out
        # tax (regular GST invoice — template re-adds it) or use raw
        # (STN / self-invoicing / Overseas exports — no GST added).
        rate = selling_price / qty if qty else 0
        item_dict = {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,  # tax-inclusive per-unit
        }
        tax_rate = float(r.get("tax_rate") or 0)
        if tax_rate > 0:
            item_dict["_ecs_tax_rate"] = tax_rate  # used by SO builder
        items.append(item_dict)
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

    # Detect the internal / non-GST branches:
    #  - STN (stocktransferorder): warehouse-to-warehouse internal move
    #  - Self-invoicing: buyer_gst == company_gstin
    # Both must skip GST templates entirely and use is_internal_customer
    # on the Customer side. India Compliance refuses to charge GST when
    # party = company, so a Non-GST setup is the only correct path.
    company_gstin = (
        frappe.db.get_value("Company", ma_doc.company, "gstin") or ""
    ).strip()
    buyer_gst = (ee_row.get("buyer_gst") or "").strip()
    is_stn = (ee_row.get("order_type_key") or "").lower() == "stocktransferorder"
    is_self = bool(buyer_gst) and buyer_gst == company_gstin
    is_non_gst = is_stn or is_self

    # For overseas customers (export), skip GST too — exports go under
    # LUT/Bond or IGST-refund; either way our SI's grand_total should
    # equal EE's foreign-currency native total (FX handled by ERPNext).
    cust_gst_cat = frappe.db.get_value("Customer", customer, "gst_category")
    is_overseas = cust_gst_cat == "Overseas"

    # Resolve GST templates per CLAUDE.md #206: set taxes_and_charges
    # + per-line item_tax_template, let ERPNext compute rows natively.
    # Skip for internal / overseas — see above.
    company_abbr = frappe.get_cached_value("Company", ma_doc.company, "abbr")
    tax_template = None
    if not is_non_gst and not is_overseas:
        tax_template = _pick_sales_tax_template(
            ma_doc.company, company_abbr, customer, ee_row=ee_row,
        )

    def _build_item_dict(it: dict) -> dict:
        # Rate arriving from _resolve_line_items is tax-INCLUSIVE.
        # For internal / overseas: keep as-is (no tax added downstream,
        #   so grand_total = rate × qty = EE.total exactly).
        # For regular GST: back out per-line tax so template can re-add.
        tax_rate = it.get("_ecs_tax_rate")
        if tax_rate and not is_non_gst and not is_overseas:
            rate = it["rate"] / (1 + tax_rate / 100.0)
        else:
            rate = it["rate"]
        d = {
            "item_code": it["item_code"],
            "qty": it["qty"],
            "rate": rate,
            "delivery_date": invoice_date,
        }
        if tax_rate and not is_non_gst and not is_overseas:
            itt = _pick_item_tax_template(company_abbr, tax_rate)
            if itt:
                d["item_tax_template"] = itt
        return d

    so_doc = {
        "doctype": "Sales Order",
        "customer": customer,
        "company": ma_doc.company,
        "transaction_date": order_date,
        "delivery_date": invoice_date,  # invoice date works as a proxy
        "currency": (ee_row.get("invoice_currency_code") or "INR").upper(),
        "items": [_build_item_dict(it) for it in items],
    }
    # conversion_rate: for INR (or company default), pin to 1; for
    # foreign, let ERPNext auto-fetch from Currency Exchange for the
    # transaction_date (we pre-loaded FX rows for July foreign dates).
    company_currency = frappe.db.get_value(
        "Company", ma_doc.company, "default_currency"
    ) or "INR"
    if so_doc["currency"] == company_currency:
        so_doc["conversion_rate"] = 1
    if tax_template:
        so_doc["taxes_and_charges"] = tax_template
    # Warehouse resolution — walk the fallback chain:
    #   1. MA.warehouse (explicit override on the MA)
    #   2. EE Location.mapped_warehouse (account-default routing)
    # SO validation makes set_warehouse mandatory when items are stock
    # items, so an unresolved warehouse fails the whole invoice. Raise
    # with actionable message rather than letting ERPNext throw a
    # generic MandatoryError. Company.default_warehouse is NOT a valid
    # column on ERPNext v16 Company doctype — don't try it.
    warehouse = (
        getattr(ma_doc, "warehouse", None)
        or _default_warehouse(ma_doc)
    )
    if not warehouse:
        raise ValueError(
            f"No warehouse for MA {ma_doc.name}: set MA.warehouse or "
            f"EE Location.mapped_warehouse (via the MA's EE Account)"
        )
    so_doc["set_warehouse"] = warehouse

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
