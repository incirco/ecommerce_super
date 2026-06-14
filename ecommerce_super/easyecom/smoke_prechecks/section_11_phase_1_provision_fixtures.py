"""§11 Phase 1 — Live smoke fixture provisioner.

Step 2 of design-lead's Stage 3 live-smoke plan: create a Customer
with billing address + GSTIN, push to EE via §8e helper, verify
EasyEcom Customer Map materializes with status=Mapped and
ee_customer_id populated.

Makes ONE real EE write (the /Wholesale/CreateCustomer POST inside
push_one_customer). Authorized for Step 2 by design-lead 2026-06-14.

Idempotent — reuses existing fixtures if already provisioned.
"""

from __future__ import annotations

from typing import Any

import frappe


_CUSTOMER_NAME = "ECS-S11-LIVESMOKE-CUST"
_ADDRESS_TITLE = "ECS-S11-LIVESMOKE-BILLING"
# Karnataka GSTIN (state code 29). Real-shape GSTIN but obviously a
# test PAN. EE may reject if it validates GSTIN against GSP — accepted
# risk: if EE refuses, this surfaces in the push outcome.
_GSTIN = "29AAHCM7727Q1ZI"
_COMPANY = "Smoke Test Co"


def provision() -> dict:
    """Top-level entry — creates fixtures, pushes to EE, returns
    structured outcome."""
    out: dict = {
        "preflight": {},
        "steps": {},
    }

    # ---- Preflight: EE Account enabled + EE Location available.
    enabled_account = frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1}, "name"
    )
    if not enabled_account:
        out["preflight"]["account"] = {
            "ok": False,
            "detail": "No enabled EasyEcom Account.",
        }
        return out
    out["preflight"]["account"] = {
        "ok": True,
        "account": enabled_account,
    }

    # ---- Step A: ensure Customer + Address.
    customer_name = _ensure_customer()
    out["steps"]["a_customer_created"] = {
        "ok": True,
        "customer": customer_name,
    }
    address_name = _ensure_address(customer_name)
    shipping_addr_name = _ensure_address(
        customer_name, kind="Shipping"
    )
    out["steps"]["b_address_linked"] = {
        "ok": True,
        "billing": address_name,
        "shipping": shipping_addr_name,
    }
    # Set customer_primary_address + tax_id on the customer.
    frappe.db.set_value(
        "Customer",
        customer_name,
        {
            "customer_primary_address": address_name,
            "tax_id": _GSTIN,
        },
        update_modified=False,
    )
    frappe.db.commit()

    # ---- Step B: check existing Customer Map (re-runs idempotent).
    existing_map = frappe.db.get_value(
        "EasyEcom Customer Map",
        {
            "erpnext_doctype": "Customer",
            "erpnext_name": customer_name,
        },
        ["name", "status", "ee_customer_id"],
        as_dict=True,
    )
    if existing_map and existing_map.get("status") == "Mapped":
        out["steps"]["c_customer_push"] = {
            "ok": True,
            "skipped": "Customer Map already in Mapped status — no EE call",
            "map": dict(existing_map),
        }
        return _finalise(out, customer_name)

    # ---- Step C: push to EE.
    from ecommerce_super.easyecom.flows.customer_push import (
        push_one_customer,
    )
    try:
        outcome = push_one_customer(customer_name)
        out["steps"]["c_customer_push"] = {
            "ok": True,
            "operation": getattr(outcome, "operation", str(outcome)),
            "pushed": getattr(outcome, "pushed", None),
            "flag_reasons": getattr(outcome, "flag_reasons", None),
        }
    except Exception as exc:
        out["steps"]["c_customer_push"] = {
            "ok": False,
            "exception": type(exc).__name__,
            "message": str(exc)[:500],
        }
        return out

    return _finalise(out, customer_name)


def _finalise(out: dict, customer_name: str) -> dict:
    """Verify Map landed + summarise."""
    final_map = frappe.db.get_value(
        "EasyEcom Customer Map",
        {
            "erpnext_doctype": "Customer",
            "erpnext_name": customer_name,
        },
        ["name", "status", "ee_customer_id", "ee_c_id"],
        as_dict=True,
    )
    out["steps"]["d_map_landed"] = {
        "ok": bool(
            final_map and final_map.get("status") == "Mapped"
        ),
        "map": dict(final_map) if final_map else None,
    }
    # Item + Warehouse confirmations (already exist per Stage 3
    # precheck; just confirm).
    out["steps"]["e_item_map_exists"] = {
        "ok": bool(
            frappe.db.get_value(
                "EasyEcom Item Map",
                {
                    "erpnext_doctype": "Item",
                    "status": "Mapped",
                },
                "erpnext_name",
            )
        ),
        "first_mapped_item": frappe.db.get_value(
            "EasyEcom Item Map",
            {
                "erpnext_doctype": "Item",
                "status": "Mapped",
            },
            "erpnext_name",
        ),
    }
    out["steps"]["f_live_warehouse_exists"] = {
        "ok": bool(
            frappe.db.get_value(
                "EasyEcom Location",
                {
                    "workflow_state": "Live",
                    "enabled": 1,
                    "mapped_warehouse": ("is", "set"),
                },
                "mapped_warehouse",
            )
        ),
        "first_live_warehouse": frappe.db.get_value(
            "EasyEcom Location",
            {
                "workflow_state": "Live",
                "enabled": 1,
                "mapped_warehouse": ("is", "set"),
            },
            "mapped_warehouse",
        ),
    }

    all_ok = all(v.get("ok") for v in out["steps"].values())
    out["overall"] = {
        "ok": all_ok,
        "passes": sum(1 for v in out["steps"].values() if v.get("ok")),
        "total": len(out["steps"]),
    }
    return out


def _ensure_customer() -> str:
    if frappe.db.exists("Customer", _CUSTOMER_NAME):
        return _CUSTOMER_NAME
    group = (
        frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
        or "All Customer Groups"
    )
    territory = (
        frappe.db.get_value("Territory", {"is_group": 0}, "name")
        or "All Territories"
    )
    doc = frappe.new_doc("Customer")
    doc.update(
        {
            "customer_name": _CUSTOMER_NAME,
            "customer_type": "Company",
            "customer_group": group,
            "territory": territory,
            "mobile_no": "9000000000",
            "email_id": "ops@livesmoke.test",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()
    return doc.name


def _ensure_address(customer_name: str, *, kind: str = "Billing") -> str:
    title = (
        _ADDRESS_TITLE
        if kind == "Billing"
        else _ADDRESS_TITLE.replace("BILLING", "SHIPPING")
    )
    existing = frappe.db.get_value(
        "Address",
        {"address_title": title},
        "name",
    )
    if existing:
        if not frappe.db.exists(
            "Dynamic Link",
            {
                "parent": existing,
                "link_doctype": "Customer",
                "link_name": customer_name,
            },
        ):
            addr = frappe.get_doc("Address", existing)
            addr.append(
                "links",
                {"link_doctype": "Customer", "link_name": customer_name},
            )
            addr.save(ignore_permissions=True)
            frappe.db.commit()
        return existing
    addr = frappe.new_doc("Address")
    fields: dict = {
        "address_title": title,
        "address_type": kind,
        "address_line1": "Plot 42, Industrial Area Phase 2",
        "address_line2": "Whitefield",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560066",
        "country": "India",
        "phone": "9000000000",
        "email_id": "ops@livesmoke.test",
        "links": [
            {"link_doctype": "Customer", "link_name": customer_name}
        ],
    }
    # Only the Billing address carries the GSTIN — Shipping in §8e
    # convention typically doesn't.
    if kind == "Billing":
        fields["gstin"] = _GSTIN
        fields["gst_category"] = "Registered Regular"
    addr.update(fields)
    addr.flags.ignore_permissions = True
    addr.insert()
    frappe.db.commit()
    return addr.name
