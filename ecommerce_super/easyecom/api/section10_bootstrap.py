"""§10 Internal Customer bootstrap — Flow B.

One whitelisted action that does the multi-step §10 onboarding the
FDE used to have to thread by hand:

  1. Create (or reuse) an Internal Customer with is_internal_customer=1,
     customer_type=Company, represents_company=<FDE-picked Company>.
  2. Fill "Allowed To Transact With" with every other enabled Company so
     transfers from any source company match the substrate's
     _find_internal_customer loose-match path.
  3. Attach the EE-managed Addresses (synced by Flow A) so the §10 B2B
     payload's billing / shipping defaults pick up real data.
  4. Trigger the existing §8e push to mint the EE wholesale c_id +
     EasyEcom Customer Map row.

Idempotent: re-running finds the existing Customer, ensures the
companies list is complete, refreshes Address links, and re-fires §8e
push (which itself is idempotent — UpdateCustomer if Map exists,
CreateCustomer otherwise).

Single-customer model: assumes one Internal Customer for the whole
setup, matching transfer_push._find_internal_customer's loose path.
For sites that want one Customer per destination Company (strict
match), call the action once per destination — each call gets its own
Customer keyed by represents_company.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _


@frappe.whitelist()
def bootstrap_section10_internal_customer(
    represents_company: str,
    customer_name: str | None = None,
    push_to_ee: int | bool = 1,
) -> dict[str, Any]:
    """Bootstrap or refresh the §10 Internal Customer for a destination
    Company. Returns a structured summary the JS layer renders.
    """
    if not represents_company or not frappe.db.exists(
        "Company", represents_company
    ):
        return {
            "ok": False,
            "message": f"Company {represents_company!r} does not exist.",
        }

    customer_name = (customer_name or "").strip() or _default_customer_name(
        represents_company
    )

    # Reuse priority:
    #   1. Customer with same name (idempotent re-run, FDE picked
    #      the exact same name).
    #   2. Any Internal Customer with the same represents_company
    #      (covers the case where the FDE renamed the auto-created
    #      one).
    existing = frappe.db.get_value(
        "Customer",
        {"customer_name": customer_name, "is_internal_customer": 1},
        "name",
    ) or frappe.db.get_value(
        "Customer",
        {
            "is_internal_customer": 1,
            "represents_company": represents_company,
        },
        "name",
    )

    created = False
    if existing:
        customer = frappe.get_doc("Customer", existing)
    else:
        customer = _create_internal_customer(
            customer_name=customer_name,
            represents_company=represents_company,
        )
        created = True

    companies_added = _ensure_allowed_companies(
        customer, exclude=represents_company
    )
    customer.save(ignore_permissions=True)

    addresses_linked = _link_ee_managed_addresses(
        customer, source_companies=[
            row.company for row in customer.companies or []
        ],
    )

    push_summary: dict[str, Any] | None = None
    if int(push_to_ee or 0):
        push_summary = _push_to_ee(customer.name)

    return {
        "ok": True,
        "customer": customer.name,
        "customer_created": created,
        "represents_company": represents_company,
        "companies_added": companies_added,
        "companies_total": len(customer.companies or []),
        "addresses_linked": addresses_linked,
        "push_to_ee": push_summary,
    }


# ============================================================
# Helpers
# ============================================================


def _default_customer_name(represents_company: str) -> str:
    return f"Internal — {represents_company}"


def _create_internal_customer(
    *, customer_name: str, represents_company: str
) -> Any:
    """Mint a new internal Customer. Picks the default Customer Group
    and Territory from the site's defaults — keeps the bootstrap from
    having to guess across deployments."""
    customer = frappe.new_doc("Customer")
    customer.customer_name = customer_name
    customer.customer_type = "Company"
    customer.is_internal_customer = 1
    customer.represents_company = represents_company
    customer.customer_group = _resolve_default_customer_group()
    customer.territory = _resolve_default_territory()
    customer.flags.ignore_permissions = True
    customer.insert()
    return customer


def _resolve_default_customer_group() -> str:
    return (
        frappe.db.get_value("Customer Group", "All Customer Groups", "name")
        or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
        or "All Customer Groups"
    )


def _resolve_default_territory() -> str:
    return (
        frappe.db.get_value("Territory", "All Territories", "name")
        or frappe.db.get_value("Territory", {"is_group": 0}, "name")
        or "All Territories"
    )


def _ensure_allowed_companies(customer: Any, *, exclude: str) -> int:
    """Add every enabled Company (except `exclude`, which is the
    represents_company) to the Customer's "Allowed To Transact With"
    child. Idempotent."""
    all_companies = frappe.db.get_all(
        "Company",
        pluck="name",
        order_by="name asc",
    )
    existing = {
        (row.company or "") for row in (customer.companies or [])
    }
    added = 0
    for c in all_companies:
        if c == exclude:
            continue
        if c in existing:
            continue
        customer.append("companies", {"company": c})
        added += 1
    return added


def _link_ee_managed_addresses(
    customer: Any, source_companies: list[str]
) -> list[dict[str, Any]]:
    """For each source Company, pick the EE-managed Addresses (Flow A
    output) of warehouses belonging to that Company and link them to
    the Customer via Dynamic Link. Returns a per-Address summary."""
    if not source_companies:
        return []

    # Find every EE-managed Address (ecs_ee_location set) linked to a
    # Warehouse whose Company is in source_companies.
    addr_rows = frappe.db.sql(
        """
        SELECT DISTINCT a.name, a.ecs_ee_location, w.company AS company
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl
          ON dl.parent = a.name
        JOIN `tabWarehouse` w
          ON w.name = dl.link_name
        WHERE IFNULL(a.ecs_ee_location, '') != ''
          AND dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND w.company IN %(companies)s
        """,
        {"companies": tuple(source_companies)},
        as_dict=True,
    )

    summary: list[dict[str, Any]] = []
    for row in addr_rows:
        addr = frappe.get_doc("Address", row["name"])
        already_linked = any(
            (lnk.link_doctype == "Customer"
             and lnk.link_name == customer.name)
            for lnk in (addr.links or [])
        )
        if already_linked:
            summary.append({
                "address": addr.name, "company": row["company"],
                "action": "already_linked",
            })
            continue
        addr.append("links", {
            "link_doctype": "Customer",
            "link_name": customer.name,
        })
        addr.flags.ignore_permissions = True
        addr.save()
        summary.append({
            "address": addr.name, "company": row["company"],
            "action": "linked",
        })
    return summary


def _push_to_ee(customer_name: str) -> dict[str, Any]:
    """Run the §8e push synchronously and return the outcome shape
    the JS renders. Idempotent at the §8e layer — Map exists →
    UpdateCustomer, else CreateCustomer."""
    from ecommerce_super.easyecom.flows.customer_push import (
        push_one_customer,
    )

    try:
        outcome = push_one_customer(customer_name)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    ee_customer_id = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer", "erpnext_name": customer_name},
        "ee_customer_id",
    )
    return {
        "ok": bool(getattr(outcome, "pushed", False)) or bool(ee_customer_id),
        "operation": getattr(outcome, "operation", None),
        "pushed": bool(getattr(outcome, "pushed", False)),
        "ee_customer_id": ee_customer_id,
        "flag_reasons": list(getattr(outcome, "flag_reasons", []) or []),
    }
