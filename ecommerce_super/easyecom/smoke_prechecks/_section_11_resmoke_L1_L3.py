"""L1+L2+L3 live re-smoke for §11 Phase 1 cancel hook wiring.

Run: bench --site smoke-test.local execute \
    ecommerce_super.easyecom.smoke_prechecks._section_11_resmoke_L1_L3.run
"""
from __future__ import annotations

import time
from typing import Any

import frappe
from frappe.utils import today


def _latest_call(endpoint_like: str) -> dict | None:
    rows = frappe.db.sql(
        """
        SELECT name, endpoint, http_method, request_url, request_payload,
               response_status_code, response_payload, status, modified
        FROM `tabEasyEcom API Call`
        WHERE endpoint LIKE %s
        ORDER BY creation DESC LIMIT 1
        """,
        (f"%{endpoint_like}%",), as_dict=True,
    )
    if not rows:
        return None
    r = dict(rows[0])
    for k in ("request_payload", "response_payload"):
        r[k] = (r.get(k) or "")[:2000]
    return r


def _make_b2b_so(suffix: str) -> str:
    so = frappe.new_doc("Sales Order")
    so.update({
        "customer": "ECS-S11-LIVESMOKE-CUST",
        "company": "Smoke Test Co",
        "set_warehouse": "Mumbai WH - STC",
        "transaction_date": today(),
        "delivery_date": today(),
        "currency": "INR",
    })
    so.append("items", {
        "item_code": "HPC-APC-001", "qty": 1, "rate": 1000,
        "warehouse": "Mumbai WH - STC", "delivery_date": today(),
        "gst_hsn_code": "39241090",
    })
    so.flags.ignore_permissions = True
    so.insert()
    so.submit()
    return so.name


def _make_vanilla_so(suffix: str) -> str:
    """A SO that is NOT routed through §11 — neither the customer nor
    warehouse is wired for EE on this code path. We achieve "not §11
    pushed" by simply skipping the on_submit push using a flag, then
    deleting any Map row that landed."""
    # Use an existing non-internal customer that's not in §11 fixtures
    # OR fall back to using the same livesmoke customer but flag to
    # skip the push. Cleanest: temporarily flip the EE Account to
    # disabled so on_submit_push's get_ee_account_for_warehouse
    # returns None → silently skip. After, restore. But that affects
    # the other steps. Better: create the SO and just delete its Map
    # row immediately so it looks "never pushed" for the cancel test.
    so = frappe.new_doc("Sales Order")
    so.update({
        "customer": "ECS-S11-LIVESMOKE-CUST",
        "company": "Smoke Test Co",
        "set_warehouse": "Mumbai WH - STC",
        "transaction_date": today(),
        "delivery_date": today(),
        "currency": "INR",
    })
    so.append("items", {
        "item_code": "HPC-APC-001", "qty": 1, "rate": 1000,
        "warehouse": "Mumbai WH - STC", "delivery_date": today(),
        "gst_hsn_code": "39241090",
    })
    so.flags.ignore_permissions = True
    so.insert()
    so.submit()
    # Strip the §11 hook's perception: clear the back-ref and delete
    # the Map row so the cancel-hook scope guard returns immediately.
    if so.get("ecs_b2b_order_map"):
        m = so.ecs_b2b_order_map
        frappe.db.set_value(
            "Sales Order", so.name, "ecs_b2b_order_map", None,
            update_modified=False,
        )
        if frappe.db.exists("EasyEcom B2B Order Map", m):
            frappe.db.delete("EasyEcom B2B Order Map", m)
        frappe.db.commit()
    return so.name


def run() -> dict[str, Any]:
    out: dict[str, Any] = {"steps": {}}

    # --- L1: fresh push (write path re-confirm) -----------------
    try:
        suffix = str(int(time.time()))[-6:]
        l1_so = _make_b2b_so(suffix)
        # Run the push worker synchronously (the queue handler is the
        # async path; bench-execute doesn't run RQ workers, so we
        # invoke the function directly to get a deterministic result).
        from ecommerce_super.easyecom.flows.b2b_sales.push import (
            push_b2b_order_async,
        )
        push_b2b_order_async(sales_order=l1_so)
        l1_map = frappe.db.get_value(
            "EasyEcom B2B Order Map", {"sales_order": l1_so},
            ["name", "status", "ee_order_id", "ee_suborder_id",
             "ee_invoice_id"], as_dict=True,
        )
        l1_create = _latest_call("createOrder")
        out["steps"]["L1_push"] = {
            "verdict": "LIVE-VERIFIED" if (
                l1_map and l1_map.get("ee_order_id")) else "FAIL",
            "so": l1_so,
            "map": dict(l1_map) if l1_map else None,
            "api_call": l1_create,
        }
    except Exception as exc:
        out["steps"]["L1_push"] = {
            "verdict": "FAIL",
            "exception": f"{type(exc).__name__}: {str(exc)[:1500]}",
        }
        return out

    # --- L2: UI-cancel path via doc.cancel() (THE actual fix
    # verification — fires the new before_cancel hook) ----------
    try:
        so_doc = frappe.get_doc("Sales Order", l1_so)
        so_doc.flags.ignore_permissions = True
        so_doc.cancel()  # ← triggers before_cancel hook
        l2_map_after = frappe.db.get_value(
            "EasyEcom B2B Order Map", l1_map["name"],
            ["status", "cancelled_at"], as_dict=True,
        )
        l2_cancel_call = _latest_call("cancelOrder")
        # Confirm SO docstatus = 2 (cancelled)
        l2_so_docstatus = frappe.db.get_value(
            "Sales Order", l1_so, "docstatus")
        out["steps"]["L2_ui_cancel"] = {
            "verdict": "LIVE-VERIFIED" if (
                l2_so_docstatus == 2
                and (l2_map_after or {}).get("status") == "Cancelled"
                and (l2_cancel_call or {}).get("response_status_code") == 200
            ) else "FAIL",
            "so_docstatus_after": l2_so_docstatus,
            "map_after": dict(l2_map_after) if l2_map_after else None,
            "api_call": l2_cancel_call,
        }
    except Exception as exc:
        out["steps"]["L2_ui_cancel"] = {
            "verdict": "FAIL",
            "exception": f"{type(exc).__name__}: {str(exc)[:1500]}",
        }

    # --- L3: vanilla (non-EE) SO cancel — scope guard live test --
    try:
        # Snapshot of API Call latest BEFORE
        before_call = _latest_call("cancelOrder")
        before_count = frappe.db.count("EasyEcom API Call")

        l3_so = _make_vanilla_so(str(int(time.time()))[-6:])
        # Verify scope guard: this SO has NO Map row, so the
        # before_cancel hook should bail immediately.
        l3_so_doc = frappe.get_doc("Sales Order", l3_so)
        l3_so_doc.flags.ignore_permissions = True
        l3_so_doc.cancel()
        l3_so_docstatus = frappe.db.get_value(
            "Sales Order", l3_so, "docstatus")

        after_call = _latest_call("cancelOrder")
        after_count = frappe.db.count("EasyEcom API Call")

        # Scope guard passes if no NEW cancelOrder API Call was made
        # AND no NEW EasyEcom API Call rows were created during this
        # vanilla cancel.
        no_new_cancel_call = (
            (before_call or {}).get("name") == (after_call or {}).get("name")
        )
        out["steps"]["L3_vanilla_cancel"] = {
            "verdict": "LIVE-VERIFIED" if (
                l3_so_docstatus == 2 and no_new_cancel_call
                and after_count == before_count
            ) else "FAIL",
            "so": l3_so,
            "so_docstatus_after": l3_so_docstatus,
            "ee_api_call_count_delta": after_count - before_count,
            "no_new_cancel_call": no_new_cancel_call,
        }
    except Exception as exc:
        out["steps"]["L3_vanilla_cancel"] = {
            "verdict": "FAIL",
            "exception": f"{type(exc).__name__}: {str(exc)[:1500]}",
        }

    return out
