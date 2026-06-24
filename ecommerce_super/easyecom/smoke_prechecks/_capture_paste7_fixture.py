"""Capture a real EE /orders/V2/getOrderDetails response and save
as a JSON fixture for the §11 polling tests.

Throwaway runner — fixture is the artifact we keep.

Run: bench --site smoke-test.local execute \\
    ecommerce_super.easyecom.smoke_prechecks._capture_paste7_fixture.run
"""
from __future__ import annotations

import json
import os
from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET


_FIXTURE_PATH = (
    "/Users/nikhilrishi/frappe/ecommerce-super/apps/ecommerce_super"
    "/ecommerce_super/tests/fixtures/b2b_polling_real_response.json"
)


def run() -> dict[str, Any]:
    company = frappe.db.get_value(
        "EasyEcom Company Settings", {"enabled": 1}, "company"
    )
    location_key = frappe.db.get_value(
        "EasyEcom Location",
        {"workflow_state": "Live", "enabled": 1},
        "location_key",
    )
    client = EasyEcomClient(company=company, location_key=location_key)
    params = {
        "reference_code": "SAL-ORD-2026-00005",
        "include_ee_history": 1,
        "include_custom_fields": 1,
        "limit": 5,
    }
    resp = client.get(ORDER_DETAILS_GET, params=params)
    os.makedirs(os.path.dirname(_FIXTURE_PATH), exist_ok=True)
    with open(_FIXTURE_PATH, "w") as f:
        json.dump(resp, f, indent=2, default=str)
    return {
        "ok": True,
        "fixture_path": _FIXTURE_PATH,
        "response_keys": list(resp.keys()),
        "data_len": len(resp.get("data") or []),
        "first_order_keys": (
            list(resp["data"][0].keys())[:30] if resp.get("data") else None
        ),
    }
