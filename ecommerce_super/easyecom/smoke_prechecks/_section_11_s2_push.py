"""S2 — happy-path §11 SO push against Harmony.

Run: bench --site smoke-test.local execute \
    ecommerce_super.easyecom.smoke_prechecks._section_11_s2_push.run
"""
from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import today


def run() -> dict[str, Any]:
    out: dict[str, Any] = {"steps": {}}

    # --- 0. Resolve fixtures ---
    customer = "ECS-S11-LIVESMOKE-CUST"
    company = "Smoke Test Co"
    src_wh = "Mumbai WH - STC"
    # §11.2 gate strictly requires Item Map status=Mapped (not
    # Created-Flagged). HPC-APC-001 is Mapped on Harmony from prior
    # §11 runs.
    item_code = "HPC-APC-001"
    hsn = frappe.db.get_value("Item", item_code, "gst_hsn_code")

    suffix = str(int(time.time()))[-6:]

    # --- 1. Create + submit SO ---
    so = frappe.new_doc("Sales Order")
    so.update({
        "customer": customer,
        "company": company,
        "set_warehouse": src_wh,
        "transaction_date": today(),
        "delivery_date": today(),
        "currency": "INR",
        # §11.2 requires Customer.gstin; we tagged tax_id earlier
    })
    so.append("items", {
        "item_code": item_code, "qty": 1, "rate": 1000,
        "warehouse": src_wh, "delivery_date": today(),
        "gst_hsn_code": hsn,
    })
    so.flags.ignore_permissions = True
    try:
        so.insert()
        so.submit()
        out["steps"]["1_so_submitted"] = {
            "name": so.name, "docstatus": so.docstatus, "status": so.status,
        }
    except Exception as exc:
        out["steps"]["1_so_submitted"] = {
            "ok": False, "exception": type(exc).__name__,
            "message": str(exc)[:1500],
        }
        return out

    # --- 2. Run the §11 push worker explicitly ---
    from ecommerce_super.easyecom.flows.b2b_sales.push import (
        push_b2b_order_async,
    )
    try:
        push_out = push_b2b_order_async(sales_order=so.name)
        out["steps"]["2_push_outcome"] = push_out
    except Exception as exc:
        out["steps"]["2_push_outcome"] = {
            "ok": False, "exception": type(exc).__name__,
            "message": str(exc)[:1500],
        }

    # --- 3. Capture latest createOrder API Call ---
    ac = frappe.db.sql(
        """
        SELECT name, endpoint, http_method, request_url,
               request_payload, response_status_code,
               response_payload, status, modified
        FROM `tabEasyEcom API Call`
        WHERE endpoint LIKE '%createOrder'
        ORDER BY creation DESC
        LIMIT 1
        """,
        as_dict=True,
    )
    if ac:
        row = dict(ac[0])
        # Trim large payloads for inline output; raw stays in DB
        for k in ("request_payload", "response_payload"):
            v = row.get(k) or ""
            row[k] = v[:3000]
        out["steps"]["3_api_call"] = row
    else:
        out["steps"]["3_api_call"] = {"detail": "no createOrder API Call found"}

    # --- 4. Read Map row + back-stamp on SO ---
    bom = frappe.db.sql(
        """
        SELECT name, sales_order, status, ee_order_id,
               ee_suborder_id, ee_invoice_id, module, easyecom_account
        FROM `tabEasyEcom B2B Order Map`
        WHERE sales_order = %s
        """,
        (so.name,), as_dict=True,
    )
    out["steps"]["4_b2b_order_map"] = dict(bom[0]) if bom else None

    so_back = frappe.db.get_value(
        "Sales Order", so.name,
        ["ecs_easyecom_so_id" if frappe.get_meta("Sales Order").get_field(
            "ecs_easyecom_so_id") else "name"],
        as_dict=True,
    )
    out["steps"]["5_so_backstamp"] = dict(so_back) if so_back else None
    return out
