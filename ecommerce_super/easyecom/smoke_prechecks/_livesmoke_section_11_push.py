"""§11 live smoke — create a fresh SO from the provisioner's
fixtures, push it to Harmony, capture the EE API Call + the B2B
Order Map outcome.

Throwaway — not part of any release flow. Runs against whatever EE
backend smoke-test.local is wired to (currently Harmony).
"""
from __future__ import annotations

import time
import frappe


def run() -> dict:
    out: dict = {"steps": {}}

    customer = "ECS-S11-LIVESMOKE-CUST"
    # Pin company → company-coherent warehouse → item that's mapped on
    # that company. Without pinning, the fixture-picker can choose
    # cross-company combinations that ERPNext rejects with
    # InvalidWarehouseCompany before any EE call.
    company = "Smoke Test Co"
    warehouse = frappe.db.get_value(
        "Warehouse",
        {
            "company": company,
            "disabled": 0,
            "ecs_ee_location_label": ["!=", ""],
        },
        "name",
    )
    # Pick a Mapped item that ALSO has a HSN code — India Compliance's
    # transaction validation refuses SOs whose lines lack HSN.
    item_code = frappe.db.sql(
        """
        SELECT m.erpnext_name
        FROM `tabEasyEcom Item Map` m
        JOIN tabItem i ON i.item_code = m.erpnext_name
        WHERE m.status = 'Mapped'
          AND i.disabled = 0
          AND i.gst_hsn_code IS NOT NULL
          AND i.gst_hsn_code != ''
        ORDER BY m.creation DESC
        LIMIT 1
        """
    )
    item_code = item_code[0][0] if item_code else None
    if not (item_code and warehouse and company):
        return {
            "error": (
                "missing fixture — item: %s warehouse: %s company: %s"
                % (item_code, warehouse, company)
            )
        }
    out["steps"]["0_fixtures"] = {
        "customer": customer,
        "item_code": item_code,
        "warehouse": warehouse,
        "company": company,
    }

    suffix = str(int(time.time()))[-6:]
    so = frappe.new_doc("Sales Order")
    so.update({
        "customer": customer,
        "company": company,
        "set_warehouse": warehouse,
        "currency": "INR",
        "transaction_date": frappe.utils.today(),
        "delivery_date": frappe.utils.today(),
    })
    item_hsn = frappe.db.get_value("Item", item_code, "gst_hsn_code") or ""
    so.append("items", {
        "item_code": item_code,
        "qty": 1,
        "rate": 100,
        "warehouse": warehouse,
        "delivery_date": frappe.utils.today(),
        "gst_hsn_code": item_hsn,
    })
    so.flags.ignore_permissions = True
    so.flags.ignore_links = True
    so.flags.ignore_mandatory = True
    try:
        so.insert()
        so.submit()
        out["steps"]["1_so_submitted"] = {
            "name": so.name, "status": so.status,
        }
    except Exception as exc:
        out["steps"]["1_so_submitted"] = {
            "ok": False,
            "exception": type(exc).__name__,
            "message": str(exc)[:1500],
        }
        return out

    from ecommerce_super.easyecom.flows.b2b_sales.push import (
        push_b2b_order_async,
    )
    try:
        outcome = push_b2b_order_async(sales_order=so.name)
        out["steps"]["2_push"] = {"ok": True, "outcome": outcome}
    except Exception as exc:
        out["steps"]["2_push"] = {
            "ok": False,
            "exception": type(exc).__name__,
            "message": str(exc)[:1500],
        }

    api_call = frappe.db.sql(
        """
        SELECT name, endpoint, request_payload,
               response_status_code, response_payload, status
        FROM `tabEasyEcom API Call`
        WHERE endpoint LIKE '%%Order%%'
        ORDER BY creation DESC
        LIMIT 1
        """,
        as_dict=True,
    )
    out["steps"]["3_api_call"] = (
        dict(api_call[0]) if api_call else {"detail": "no api call captured"}
    )
    map_row = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        {"sales_order": so.name},
        ["name", "status", "ee_order_id", "ee_invoice_id", "module"],
        as_dict=True,
    )
    out["steps"]["4_map_row"] = dict(map_row) if map_row else {
        "detail": "no map row"
    }
    return out
