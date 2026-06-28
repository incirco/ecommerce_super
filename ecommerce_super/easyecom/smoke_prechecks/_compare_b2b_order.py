"""Compare what we pushed vs what EE has for SAL-ORD-2026-00018."""
from __future__ import annotations

from typing import Any
import json

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET


def run() -> dict[str, Any]:
    so_name = "SAL-ORD-2026-00020"
    company = "Smoke Test Co"
    location_key = "ve9861483025"

    client = EasyEcomClient(company=company, location_key=location_key)

    params = {
        "reference_code": so_name,
        "include_ee_history": 1,
        "include_custom_fields": 1,
    }
    try:
        resp = client.get(ORDER_DETAILS_GET, params=params)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "params": params}

    data_rows = resp.get("data") or []
    if not data_rows:
        return {"error": "no data rows returned", "raw": resp}

    ee_order = data_rows[0]

    # Field-by-field comparison
    fields_to_check = [
        "reference_code",
        "order_id",
        "invoice_id",
        "suborder_count",
        "order_type",
        "order_type_key",
        "order_status",
        "order_status_id",
        "order_date",
        "company_name",
        "warehouse_id",
        "marketplace",
        "merchant_c_id",
        "customer_name",
        "shipping_name",
        "billing_name",
        "city",
        "state",
        "country",
        "pin_code",
        "billing_city",
        "billing_state",
        "billing_country",
        "billing_pin_code",
        "buyer_gst",
        "total_amount",
        "total_tax",
        "collectable_amount",
        "payment_mode",
        "payment_mode_id",
        "order_quantity",
        "marketplace_invoice_num",
        "invoice_number",
    ]

    summary = {field: ee_order.get(field) for field in fields_to_check}

    # Line items
    items = []
    for item in (ee_order.get("order_items") or []):
        items.append({
            "sku": item.get("sku"),
            "marketplace_sku": item.get("marketplace_sku"),
            "AccountingSku": item.get("AccountingSku"),
            "productName": item.get("productName"),
            "item_quantity": item.get("item_quantity"),
            "suborder_quantity": item.get("suborder_quantity"),
            "selling_price": item.get("selling_price"),
            "tax_rate": item.get("tax_rate"),
            "taxable_value": item.get("taxable_value"),
            "tax_value": item.get("tax_value"),
            "igst": item.get("igst"),
            "cgst": item.get("cgst"),
            "sgst": item.get("sgst"),
            "suborder_id": item.get("suborder_id"),
            "suborder_num": item.get("suborder_num"),
        })

    # Easyecom history
    history = ee_order.get("easyecom_order_history") or []

    return {
        "ok": True,
        "ee_top_level": summary,
        "ee_line_items": items,
        "easyecom_order_history": history,
        "ee_response_keys": sorted(ee_order.keys()),
        "raw_full": ee_order,
    }
