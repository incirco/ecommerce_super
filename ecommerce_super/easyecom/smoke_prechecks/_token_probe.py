"""Read latest /access/token response for diagnosis.

Run: bench --site smoke-test.local execute \\
    ecommerce_super.easyecom.smoke_prechecks._token_probe.run
"""
from __future__ import annotations

import frappe


def run() -> dict:
    rows = frappe.db.sql(
        """
        SELECT name, request_payload, response_payload, response_status_code
        FROM `tabEasyEcom API Call`
        WHERE endpoint = '/access/token'
        ORDER BY modified DESC
        LIMIT 1
        """,
        as_dict=True,
    )
    if not rows:
        return {"detail": "No /access/token calls found"}
    r = rows[0]
    return {
        "name": r["name"],
        "status": r["response_status_code"],
        "request_payload": (r.get("request_payload") or "")[:1200],
        "response_payload": (r.get("response_payload") or "")[:1200],
    }
