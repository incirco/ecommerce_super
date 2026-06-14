"""Diagnostic endpoint for §11 B2B sales push.

Mirror of §10's transfer_diagnostic.trace_dn: walks every gate the
§11 push touches and returns a structured trace the FDE can render
on the SO form. Read-only — never mutates state, never re-fires the
push.

Stage 2 implementation: walks Gate 0 + every precondition + Map row
state + recent Integration Discrepancies. Stage 3 will add the
polling-last-at + reconciliation diagnosis fields.

Usage from desk console:
    frappe.call(
      'ecommerce_super.easyecom.api.trace_b2b_so.trace_so',
      {so_name: 'SAL-ORD-2026-00042'},
    )
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.gating import (
    is_section_11_gated,
)
from ecommerce_super.easyecom.helpers.master_resolution import (
    resolve_ee_customer_id,
    resolve_ee_sku,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_account_for_warehouse,
    get_ee_location_for_warehouse,
)


@frappe.whitelist()
def trace_so(so_name: str) -> dict[str, Any]:
    """Walk every gate the §11 push touches and report visible state.

    Read-only. Returns a dict the SO form's "Trace B2B Push" button
    renders directly. Each gate has {gate: str, passed: bool|None,
    detail: str}. Downstream artifacts include the Map row + recent
    Integration Discrepancies.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "B2B trace requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )

    trace: dict[str, Any] = {
        "ok": True,
        "so_name": so_name,
        "gates": [],
        "downstream": {},
        "verdict": "",
    }

    if not so_name or not frappe.db.exists("Sales Order", so_name):
        trace["ok"] = False
        trace["gates"].append(
            {
                "gate": "so_exists",
                "passed": False,
                "detail": f"Sales Order {so_name!r} not found",
            }
        )
        trace["verdict"] = "SO not found"
        return trace

    so = frappe.get_doc("Sales Order", so_name)
    trace["gates"].append(
        {
            "gate": "so_exists",
            "passed": True,
            "detail": f"docstatus={so.docstatus}, set_warehouse={so.set_warehouse!r}",
        }
    )

    # Gate 0 — is §11 gated for this SO?
    gated = is_section_11_gated(so)
    trace["gates"].append(
        {
            "gate": "gate_0_ee_mapped_warehouse",
            "passed": gated,
            "detail": (
                f"set_warehouse {so.set_warehouse!r} → Live EE Location"
                if gated
                else (
                    "set_warehouse empty"
                    if not so.set_warehouse
                    else (
                        f"set_warehouse {so.set_warehouse!r} not mapped "
                        "to a Live + enabled EasyEcom Location"
                    )
                )
            ),
        }
    )
    if not gated:
        trace["verdict"] = (
            "Gate 0 not met — SO is purely ERPNext; §11 not involved."
        )
        return trace

    # EE Account resolution
    ee_account = get_ee_account_for_warehouse(so.set_warehouse)
    if not ee_account:
        trace["gates"].append(
            {
                "gate": "ee_account_resolved",
                "passed": False,
                "detail": (
                    "Warehouse maps to an EE Location but the Location "
                    "has no easyecom_account set."
                ),
            }
        )
        trace["verdict"] = (
            "EE Account unresolved — preconditions would block submit."
        )
        return trace
    trace["gates"].append(
        {
            "gate": "ee_account_resolved",
            "passed": True,
            "detail": (
                f"account={ee_account.name}, "
                f"module={ee_account.get('ecs_b2b_module') or '(unset)'}"
            ),
        }
    )

    # Precondition walks (mirror gating.validate_preconditions order).
    _walk_preconditions(trace, so, ee_account)

    # Downstream artifacts
    trace["downstream"]["b2b_order_map"] = _b2b_order_map_snapshot(so)
    trace["downstream"]["discrepancies"] = _recent_discrepancies(so_name)

    # Verdict
    failed_gates = [g for g in trace["gates"] if g["passed"] is False]
    if failed_gates:
        trace["ok"] = False
        trace["verdict"] = (
            f"{len(failed_gates)} precondition(s) failing — submit "
            "would refuse with: " + failed_gates[0]["detail"]
        )
    elif not trace["downstream"]["b2b_order_map"]:
        trace["verdict"] = (
            "All gates pass; no Map row yet — push hasn't fired or is "
            "still enqueued."
        )
    else:
        m = trace["downstream"]["b2b_order_map"]
        trace["verdict"] = (
            f"Map exists ({m['name']}, status={m['status']}); "
            "submit-side flow completed."
        )
    return trace


def _walk_preconditions(trace: dict, so: Any, ee_account: Any) -> None:
    # 1. Mixed warehouses
    bad_lines = [
        (i.idx, i.item_code, i.warehouse)
        for i in (so.items or [])
        if i.warehouse != so.set_warehouse
    ]
    trace["gates"].append(
        {
            "gate": "precondition_1_warehouse_unity",
            "passed": not bad_lines,
            "detail": (
                f"All {len(so.items or [])} lines on {so.set_warehouse!r}"
                if not bad_lines
                else f"{len(bad_lines)} line(s) on a different warehouse: {bad_lines[:3]!r}"
            ),
        }
    )

    # 2. EE Account B2B module
    module = (ee_account.get("ecs_b2b_module") or "").strip()
    trace["gates"].append(
        {
            "gate": "precondition_2_b2b_module_configured",
            "passed": bool(module),
            "detail": (
                f"module={module}" if module else "ecs_b2b_module unset on EE Account"
            ),
        }
    )

    # 3. Customer synced
    ee_cust_id = resolve_ee_customer_id(so.customer)
    trace["gates"].append(
        {
            "gate": "precondition_3_customer_synced",
            "passed": bool(ee_cust_id),
            "detail": (
                f"ee_customer_id={ee_cust_id}"
                if ee_cust_id
                else f"No Mapped Customer Map row for {so.customer!r}"
            ),
        }
    )

    # 4. All items synced
    unsynced = [
        i.item_code for i in (so.items or [])
        if not resolve_ee_sku(i.item_code)
    ]
    trace["gates"].append(
        {
            "gate": "precondition_4_items_synced",
            "passed": not unsynced,
            "detail": (
                f"All {len(so.items or [])} items synced"
                if not unsynced
                else f"Unsynced items: {unsynced[:5]!r}"
            ),
        }
    )

    # 5. Customer GSTIN for Old B2B
    try:
        customer = frappe.get_doc("Customer", so.customer)
    except Exception:
        customer = None
    if module == "Old B2B" and customer is not None:
        has_gstin = bool((customer.tax_id or "").strip())
        trace["gates"].append(
            {
                "gate": "precondition_5_gstin_strict_old_b2b",
                "passed": has_gstin,
                "detail": (
                    f"tax_id={customer.tax_id!r}"
                    if has_gstin
                    else "Old B2B requires GSTIN; customer has none"
                ),
            }
        )

    # 6. HSN on every item
    items_no_hsn = []
    for i in so.items or []:
        try:
            item_doc = frappe.get_cached_doc("Item", i.item_code)
            if not (item_doc.gst_hsn_code or "").strip():
                items_no_hsn.append(i.item_code)
        except Exception:
            items_no_hsn.append(f"{i.item_code} (read failed)")
    trace["gates"].append(
        {
            "gate": "precondition_6_hsn_present",
            "passed": not items_no_hsn,
            "detail": (
                f"All items carry HSN"
                if not items_no_hsn
                else f"Items missing HSN: {items_no_hsn[:5]!r}"
            ),
        }
    )

    # 7. Non-zero rate
    zero_rate = [
        i.item_code for i in (so.items or []) if float(i.rate or 0) <= 0
    ]
    trace["gates"].append(
        {
            "gate": "precondition_7_non_zero_rate",
            "passed": not zero_rate,
            "detail": (
                "All lines priced"
                if not zero_rate
                else f"Zero-rate items: {zero_rate[:5]!r}"
            ),
        }
    )

    # 8. Billing address
    has_billing = bool(
        customer is not None
        and (customer.customer_primary_address or "").strip()
    )
    trace["gates"].append(
        {
            "gate": "precondition_8_billing_address",
            "passed": has_billing,
            "detail": (
                f"customer_primary_address={customer.customer_primary_address}"
                if has_billing
                else f"Customer {so.customer!r} has no primary billing address"
            ),
        }
    )

    # 9. Shipping address
    has_so_shipping = bool((so.shipping_address_name or "").strip())
    has_customer_primary = bool(
        customer is not None
        and (customer.customer_primary_address or "").strip()
    )
    trace["gates"].append(
        {
            "gate": "precondition_9_shipping_address",
            "passed": has_so_shipping or has_customer_primary,
            "detail": (
                f"SO.shipping_address_name={so.shipping_address_name!r}"
                if has_so_shipping
                else (
                    f"Falls back to customer_primary={customer.customer_primary_address}"
                    if has_customer_primary
                    else "No SO shipping AND no customer primary address"
                )
            ),
        }
    )


def _b2b_order_map_snapshot(so: Any) -> dict | None:
    map_name = so.get("ecs_b2b_order_map")
    if not map_name or not frappe.db.exists(
        "EasyEcom B2B Order Map", map_name
    ):
        return None
    row = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        map_name,
        [
            "name",
            "module",
            "status",
            "ee_order_id",
            "ee_suborder_id",
            "ee_invoice_id",
            "pushed_at",
            "cancelled_at",
            "last_polled_at",
        ],
        as_dict=True,
    )
    return dict(row) if row else None


@frappe.whitelist()
def b2b_branch_chip(warehouse: str) -> dict[str, Any]:
    """Lightweight endpoint for the SO form's branch chip — returns
    whether this warehouse is §11-gated and (if so) which module +
    e-way origination its EE Account is configured for.

    Read-only. Always returns a dict so the JS callback has a stable
    shape even when no chip should render.
    """
    if not warehouse:
        return {"gated": False, "module": None, "eway_origination": None}
    ee_account = get_ee_account_for_warehouse(warehouse)
    if not ee_account:
        return {"gated": False, "module": None, "eway_origination": None}
    module = (ee_account.get("ecs_b2b_module") or "").strip() or None
    eway = (ee_account.get("ecs_eway_origination") or "").strip() or "EasyEcom"
    return {
        "gated": bool(module),
        "module": module,
        "eway_origination": eway,
    }


def _recent_discrepancies(so_name: str) -> list[dict]:
    rows = frappe.db.get_all(
        "EasyEcom Integration Discrepancy",
        filters={
            "reference_doctype": "Sales Order",
            "reference_name": so_name,
        },
        fields=["name", "kind", "status", "creation"],
        order_by="creation desc",
        limit=5,
    )
    return [dict(r) for r in rows]
