"""§11 Stage 3 — Get All Orders endpoint shape probe.

Read-only probe against Harmony's /orders/V2/getAllOrders to confirm:
  - Exact request param shape (created_after? limit? filter format?)
  - Response shape (data field names, pagination cursor format)
  - Rate limit semantics if hit
  - Per-row schema relevant to §11 polling reconciliation
    (orderNumber? reference_code? order_status?
     ee_order_id? ee_invoice_id?)

Do NOT mutate any state, no cursor write, no GRN-pull-style watermark
advance. Pure HTTP GET probe + diagnostic dump.

Use this BEFORE coding polling.py reconciliation logic to ensure the
implementation matches Harmony's actual contract — not the packet's
pre-build assumption.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import frappe


def probe(max_pages: int = 3, limit_per_page: int = 10) -> dict:
    """Read-only probe of /orders/V2/getAllOrders against the enabled
    Account's first Live + enabled Location.

    Returns a structured trace with:
      - request params actually sent
      - HTTP response code
      - sample rows (first 3) + their full key shape
      - field names relevant to §11 polling
      - pagination cursor + next_url shape if present
    """
    from ecommerce_super.easyecom.client.client import EasyEcomClient
    from ecommerce_super.easyecom.client.endpoints import ORDERS_GET_ALL

    account_name = frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1}, "name"
    )
    if not account_name:
        return {"error": "no enabled EasyEcom Account"}

    loc_key = frappe.db.get_value(
        "EasyEcom Location",
        {"workflow_state": "Live", "enabled": 1},
        "location_key",
    )
    if not loc_key:
        return {"error": "no Live + enabled EE Location"}

    client = EasyEcomClient(location_key=str(loc_key))

    # EE enforces a 7-day max window on getAllOrders (discovered
    # 2026-06-14 — body code 400 "Date range greater than 7 days not
    # allowed" on a 90-day window). Use 6 days to leave a safety
    # margin. §9 GRN pull's V2 endpoint shares the limit=10 cap;
    # confirm whether this endpoint shares it too in the response.
    since = (datetime.utcnow() - timedelta(days=6)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    params: dict = {
        "limit": limit_per_page,
        "created_after": since,
    }

    out: dict = {
        "endpoint": ORDERS_GET_ALL,
        "request_params": dict(params),
        "account": account_name,
        "location_key": loc_key,
        "pages_walked": 0,
        "total_rows_seen": 0,
        "sample_rows": [],
        "key_shape": set(),
        "pagination_cursor_keys_present": set(),
        "b2b_relevant_fields": {},
        "errors": [],
    }

    next_url: str | None = None
    pages = 0
    while pages < max_pages:
        try:
            if next_url:
                page = client.get(next_url, params=None)
            else:
                page = client.get(ORDERS_GET_ALL, params=params)
        except Exception as exc:
            # Capture response_body if the exception carries one
            # (EasyEcomValidationError + family wrap the body).
            body = getattr(exc, "response_body", None)
            status_code = getattr(exc, "status_code", None)
            out["errors"].append(
                {
                    "page": pages,
                    "exception_class": type(exc).__name__,
                    "message": str(exc)[:500],
                    "status_code": status_code,
                    "response_body": (
                        body if isinstance(body, dict)
                        else (str(body)[:1000] if body else None)
                    ),
                }
            )
            break
        pages += 1
        out["pages_walked"] = pages

        # Pagination probes — what cursor key does EE use?
        for cursor_key in ("next", "nextUrl", "next_url", "cursor", "next_page"):
            if isinstance(page, dict) and cursor_key in page:
                out["pagination_cursor_keys_present"].add(cursor_key)

        rows = (page or {}).get("data") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            out["errors"].append(
                {
                    "page": pages,
                    "detail": (
                        f"Response 'data' is not a list; top-level keys: "
                        f"{list(page.keys()) if isinstance(page, dict) else type(page).__name__}"
                    ),
                }
            )
            break

        for row in rows:
            out["total_rows_seen"] += 1
            if isinstance(row, dict):
                for k in row.keys():
                    out["key_shape"].add(k)
            if len(out["sample_rows"]) < 3 and isinstance(row, dict):
                out["sample_rows"].append(_truncate_row(row))

        # Find next cursor for the walk.
        next_url = None
        for k in (
            "next",
            "nextUrl",
            "next_url",
            "cursor",
            "next_page",
        ):
            v = (page or {}).get(k) if isinstance(page, dict) else None
            if v:
                next_url = v
                break
        if not next_url:
            break

    # Render sets as sorted lists for JSON.
    out["key_shape"] = sorted(out["key_shape"])
    out["pagination_cursor_keys_present"] = sorted(
        out["pagination_cursor_keys_present"]
    )

    # Identify §11-polling-relevant fields. Each polling reconciliation
    # case needs specific field-shape support:
    #   - New B2B identifier correlation: match by reference_code, read
    #     orderId / suborderId / invoiceId
    #   - EE-side cancellation detection: read order_status / status
    #   - EE-side invoice generation: read invoice_id or similar
    keys = set(out["key_shape"])
    out["b2b_relevant_fields"] = {
        "reference_code_match_candidates": sorted(
            k for k in keys
            if "reference" in k.lower()
            or "order_number" in k.lower()
            or k.lower() == "ordernumber"
        ),
        "order_id_candidates": sorted(
            k for k in keys
            if "order_id" in k.lower() or k.lower() == "orderid"
        ),
        "suborder_id_candidates": sorted(
            k for k in keys
            if "suborder" in k.lower() or "sub_order" in k.lower()
        ),
        "invoice_id_candidates": sorted(
            k for k in keys
            if "invoice" in k.lower()
        ),
        "status_candidates": sorted(
            k for k in keys
            if "status" in k.lower() or "state" in k.lower()
        ),
        "cancellation_candidates": sorted(
            k for k in keys
            if "cancel" in k.lower()
        ),
    }

    return out


def _truncate_row(row: dict) -> dict:
    """Trim long string values for compact display while keeping the
    full key set visible."""
    trimmed: dict = {}
    for k, v in row.items():
        if isinstance(v, str) and len(v) > 200:
            trimmed[k] = v[:200] + "…"
        elif isinstance(v, (list, dict)) and len(str(v)) > 500:
            trimmed[k] = f"<{type(v).__name__}, len(str)={len(str(v))}>"
        else:
            trimmed[k] = v
    return trimmed
