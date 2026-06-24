"""§11 Phase 1 live-smoke preconditions probe.

Run: bench --site smoke-test.local execute \
    ecommerce_super.easyecom.smoke_prechecks._section_11_preconditions.run
"""
from __future__ import annotations

import importlib
import json
from typing import Any

import frappe


def run() -> dict[str, Any]:
    out: dict[str, Any] = {}

    # 1. EE target
    acct = frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1},
        ["name", "api_endpoint", "ecs_b2b_module", "default_location_key"],
        as_dict=True,
    )
    out["account"] = dict(acct) if acct else None

    # 2. JWT obtainable
    try:
        from ecommerce_super.easyecom.client.client import EasyEcomClient
        loc = frappe.db.get_value(
            "EasyEcom Location",
            {"workflow_state": "Live", "enabled": 1},
            ["name", "location_key", "mapped_warehouse"],
            as_dict=True,
        )
        out["location"] = dict(loc) if loc else None
        company = frappe.db.get_value(
            "EasyEcom Company Settings", {"enabled": 1}, "company"
        )
        out["company"] = company
        if loc and company:
            client = EasyEcomClient(company=company, location_key=loc.location_key)
            jwt = client.refresh_jwt()
            out["jwt_acquired"] = bool(jwt)
            out["jwt_length"] = len(jwt) if jwt else 0
        else:
            out["jwt_acquired"] = False
            out["jwt_reason"] = "no loc or no company"
    except Exception as exc:
        out["jwt_acquired"] = False
        out["jwt_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    # 3. §8d mapped Item with HSN + non-zero rate
    item_row = frappe.db.sql(
        """
        SELECT m.erpnext_name AS item_code, m.ee_sku, m.ee_product_id,
               i.gst_hsn_code, i.has_batch_no, i.has_serial_no,
               i.standard_rate
        FROM `tabEasyEcom Item Map` m
        JOIN tabItem i ON i.item_code = m.erpnext_name
        WHERE m.status IN ('Mapped', 'Created-Flagged')
          AND i.gst_hsn_code IS NOT NULL AND i.gst_hsn_code != ''
          AND IFNULL(i.has_batch_no, 0) = 0
          AND IFNULL(i.has_serial_no, 0) = 0
          AND i.disabled = 0
        LIMIT 1
        """,
        as_dict=True,
    )
    out["item"] = dict(item_row[0]) if item_row else None

    # 4. §8a EE-mapped Warehouse
    wh = frappe.db.get_value(
        "Warehouse",
        {"disabled": 0, "ecs_ee_location_label": ["!=", ""]},
        ["name", "company", "ecs_ee_location_label"],
        as_dict=True,
    )
    out["warehouse"] = dict(wh) if wh else None

    # 5. Company with state
    if company:
        co_row = frappe.db.get_value(
            "Company", company,
            ["country", "default_currency", "gstin", "gst_category"],
            as_dict=True,
        )
        out["company_detail"] = dict(co_row) if co_row else None

    # 6. §11 handler module paths
    handlers = {}
    for path, attr in [
        ("ecommerce_super.easyecom.flows.b2b_sales.push", "push_b2b_order_async"),
        ("ecommerce_super.easyecom.flows.b2b_sales.push", "on_submit_push"),
        ("ecommerce_super.easyecom.flows.b2b_sales.cancel",
         "cancel_b2b_order_from_erpnext"),
        ("ecommerce_super.easyecom.flows.b2b_sales.polling",
         "derive_local_status_from_ee_rows"),
    ]:
        try:
            mod = importlib.import_module(path)
            handlers[f"{path}.{attr}"] = hasattr(mod, attr)
        except Exception as exc:
            handlers[f"{path}.{attr}"] = f"IMPORT FAIL: {type(exc).__name__}: {exc}"
    out["handlers"] = handlers

    return out
