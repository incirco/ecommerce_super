"""S3 — cancel SAL-ORD-2026-00013 from ERPNext, capture EE round-trip.

Run: bench --site smoke-test.local execute \
    ecommerce_super.easyecom.smoke_prechecks._section_11_s3_cancel.run
"""
from __future__ import annotations

import json
from typing import Any

import frappe


def run() -> dict[str, Any]:
    out: dict[str, Any] = {"steps": {}}
    so_name = "SAL-ORD-2026-00013"

    # 1. Confirm SO + Map state before cancel
    so_before = frappe.db.get_value(
        "Sales Order", so_name,
        ["docstatus", "status"], as_dict=True,
    )
    bom_before = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        {"sales_order": so_name},
        ["name", "status", "ee_order_id"], as_dict=True,
    )
    out["steps"]["1_before"] = {
        "so": dict(so_before) if so_before else None,
        "map": dict(bom_before) if bom_before else None,
    }

    # 2. Cancel the SO (triggers on_cancel hook)
    try:
        so = frappe.get_doc("Sales Order", so_name)
        so.flags.ignore_permissions = True
        so.cancel()
        out["steps"]["2_cancel_action"] = {
            "ok": True, "docstatus": so.docstatus,
        }
    except Exception as exc:
        out["steps"]["2_cancel_action"] = {
            "ok": False, "exception": type(exc).__name__,
            "message": str(exc)[:1500],
        }
        return out

    # 3. Capture latest cancel API Call
    ac = frappe.db.sql(
        """
        SELECT name, endpoint, http_method, request_url,
               request_payload, response_status_code,
               response_payload, status
        FROM `tabEasyEcom API Call`
        WHERE endpoint LIKE '%cancel%' OR endpoint LIKE '%Cancel%'
        ORDER BY creation DESC
        LIMIT 1
        """,
        as_dict=True,
    )
    if ac:
        row = dict(ac[0])
        for k in ("request_payload", "response_payload"):
            row[k] = (row.get(k) or "")[:2000]
        out["steps"]["3_cancel_api_call"] = row
    else:
        out["steps"]["3_cancel_api_call"] = {"detail": "no cancel API Call found"}

    # 4. Confirm Map row state after
    bom_after = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        {"sales_order": so_name},
        ["name", "status", "ee_order_id"], as_dict=True,
    )
    out["steps"]["4_after"] = {
        "map": dict(bom_after) if bom_after else None,
    }

    # 5. Re-read order from Harmony to confirm cancellation
    try:
        from ecommerce_super.easyecom.client.client import EasyEcomClient
        from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET
        client = EasyEcomClient(
            company="Smoke Test Co", location_key="ve9861483025",
        )
        resp = client.get(
            ORDER_DETAILS_GET,
            params={
                "reference_code": so_name,
                "include_ee_history": 1,
                "limit": 5,
            },
        )
        rows = resp.get("data") or []
        first = rows[0] if rows else None
        out["steps"]["5_ee_readback"] = {
            "row_count": len(rows),
            "order_status": (first.get("order_status") if first else None),
            "order_status_id": (first.get("order_status_id") if first else None),
            "easyecom_order_history": (
                first.get("easyecom_order_history") if first else None
            ),
        }
    except Exception as exc:
        out["steps"]["5_ee_readback"] = {
            "ok": False, "exception": type(exc).__name__,
            "message": str(exc)[:500],
        }

    return out
