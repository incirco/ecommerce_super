"""Paste-7 probe — read getOrderDetails for B2B orders to capture
the response shape.

Run: bench --site smoke-test.local execute \\
    ecommerce_super.easyecom.smoke_prechecks._paste7_probe.run
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import add_days, today

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET


def run() -> dict[str, Any]:
    company = frappe.db.get_value(
        "EasyEcom Company Settings", {"enabled": 1}, "company"
    )
    # Need a location_key so the client can refresh the JWT (the
    # smoke-test.local Harmony bearer expires and 401s are seen on
    # /orders/V2/getOrderDetails).
    location_key = frappe.db.get_value(
        "EasyEcom Location",
        {"workflow_state": "Live", "enabled": 1},
        "location_key",
    )
    client = EasyEcomClient(company=company, location_key=location_key)
    # Force a fresh JWT to clear any cached-but-expired bearer.
    client.refresh_jwt()
    # EE requires reference_code OR order_id OR invoice_id (date
    # filters alone yield 400). Use a known existing order.
    params = {
        "reference_code": "SAL-ORD-2026-00005",
        "include_ee_history": 1,
        "include_custom_fields": 1,
        "limit": 5,
    }
    try:
        resp = client.get(ORDER_DETAILS_GET, params=params)
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "params": params,
        }
    return {
        "ok": True,
        "params": params,
        "response_keys": list(resp.keys()) if isinstance(resp, dict) else None,
        "data_len": len(resp.get("data") or []) if isinstance(resp, dict) else None,
        "first_order": (
            resp.get("data", [None])[0]
            if isinstance(resp, dict) and resp.get("data") else None
        ),
    }
