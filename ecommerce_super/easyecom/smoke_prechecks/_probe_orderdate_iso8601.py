"""Probe whether EE honors ISO 8601 timezone offset for orderDate.

Sends a synthetic Old B2B payload with orderDate in three candidate
formats; compares EE's stored value against what we sent.

We've already established:
  send "2026-06-28 00:00:00"        → display "2026-06-28 05:30:00"   (UTC interpretation, +5:30 shift)
  send "2026-06-27 18:30:00"        → display "2026-06-28 00:00:00"   (UTC interpretation, +5:30 shift)

The goal: find a format where send == display.

Candidates to try:
  A. "2026-06-28T00:00:00+05:30"   (ISO 8601 with explicit IST offset)
  B. "2026-06-28 00:00:00+05:30"   (space-separated, IST offset)
  C. "2026-06-28"                  (date-only, no time)

Each candidate gets ONE push so we don't flood Harmony.
"""
from __future__ import annotations

import json
import time as _time
from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    CREATE_ORDER, ORDER_DETAILS_GET,
)


COMPANY = "Smoke Test Co"
LOCATION_KEY = "ve9861483025"
CUSTOMER_ID = "272694"  # EC's customer for ECS-S11-LIVESMOKE-CUST
GSTIN = "29AAHCM7727Q1ZI"


def _build_payload(order_number: str, order_date_str: str) -> dict:
    return {
        "orderType": "businessorder",
        "orderNumber": order_number,
        "orderDate": order_date_str,
        "expDeliveryDate": "2026-06-28 00:00:00",
        "is_market_shipped": 0,
        "remarks1": "",
        "remarks2": "",
        "shippingCost": 0,
        "discount": 0,
        "walletDiscount": 0,
        "promoCodeDiscount": 0,
        "prepaidDiscount": 0,
        "paymentMode": 2,
        "paymentGateway": "",
        "shippingMethod": 1,
        "paymentTransactionNumber": "",
        "collectableAmount": 1000.0,
        "packageWeight": 0,
        "packageHeight": 0,
        "packageWidth": 0,
        "packageLength": 0,
        "taxIdentificationNumber": GSTIN,
        "items": [
            {
                "OrderItemId": f"{order_number}-line-1",
                "Sku": "HPC-APC-001",
                "productName": "Harmony All-Purpose Cleaner",
                "Quantity": "1",
                "Price": 1000.0,
                "itemDiscount": 0,
            },
        ],
        "customer": [
            {
                "customerId": CUSTOMER_ID,
                "billing": {
                    "name": "ECS-S11-LIVESMOKE-CUST",
                    "addressLine1": "Plot 42, Industrial Area Phase 2",
                    "addressLine2": "Whitefield",
                    "city": "Bengaluru",
                    "state": "Karnataka",
                    "country": "India",
                    "postalCode": "560066",
                    "contact": "9000000000",
                    "email": "ops@livesmoke.test",
                },
                "shipping": {
                    "name": "ECS-S11-LIVESMOKE-CUST",
                    "addressLine1": "Plot 42, Industrial Area Phase 2",
                    "addressLine2": "Whitefield",
                    "city": "Bengaluru",
                    "state": "Karnataka",
                    "country": "India",
                    "postalCode": "560066",
                    "contact": "9000000000",
                    "email": "ops@livesmoke.test",
                },
            },
        ],
    }


def _push_and_check(client: EasyEcomClient, label: str, order_date_str: str) -> dict:
    """Push a probe order then read it back via getOrderDetails."""
    suffix = str(int(_time.time()))[-6:]
    order_number = f"PROBE-DATE-{label}-{suffix}"
    payload = _build_payload(order_number, order_date_str)

    push_resp = client.post(CREATE_ORDER, payload=payload)

    # Give EE a beat to commit the row before re-reading
    _time.sleep(2)

    try:
        details = client.get(
            ORDER_DETAILS_GET,
            params={
                "reference_code": order_number,
                "include_ee_history": 1,
            },
        )
    except Exception as exc:
        return {
            "label": label,
            "order_number": order_number,
            "sent_orderDate": order_date_str,
            "push_response": push_resp,
            "details_error": f"{type(exc).__name__}: {exc}",
        }

    rows = details.get("data") or []
    ee_order_date = rows[0].get("order_date") if rows else None
    ee_invoice_date = rows[0].get("invoice_date") if rows else None

    match = (ee_order_date == order_date_str)

    return {
        "label": label,
        "order_number": order_number,
        "sent_orderDate": order_date_str,
        "ee_order_date_displayed": ee_order_date,
        "match": match,
        "ee_invoice_date": ee_invoice_date,
        "push_response_code": push_resp.get("code"),
        "push_response_message": push_resp.get("message"),
    }


def run() -> dict[str, Any]:
    client = EasyEcomClient(company=COMPANY, location_key=LOCATION_KEY)
    client.refresh_jwt()

    out: dict[str, Any] = {"baseline": {
        "send_2026-06-28_00:00:00": "display 2026-06-28 05:30:00 (verified earlier)",
        "send_2026-06-27_18:30:00": "display 2026-06-28 00:00:00 (verified earlier)",
    }}

    probes = [
        ("ISO8601_IST", "2026-06-28T00:00:00+05:30"),
        ("SPACE_IST",   "2026-06-28 00:00:00+05:30"),
        ("DATE_ONLY",   "2026-06-28"),
    ]

    out["probes"] = []
    for label, order_date_str in probes:
        result = _push_and_check(client, label, order_date_str)
        out["probes"].append(result)

    out["verdict"] = "Look at each probe's `match` field. True = wire == display."
    return out
