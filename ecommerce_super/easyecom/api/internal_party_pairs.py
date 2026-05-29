"""§10 Stage 1 — Internal Customer / Internal Supplier auto-creation.

**Packet correction (Stage 1 build finding 2026-05-29).** The packet
(§10 The model line 25 + DocTypes line 118) describes one Internal
Customer per ordered (source, target) pair, e.g.
`INTL-CUST-{src}-to-{tgt}` with `represents_company=tgt`. ERPNext's
selling/customer.py:243-258 enforces *at most one* Internal Customer
per `represents_company` value, site-wide. So in a 3-Company scenario
the packet asks for 6 Internal Customers (one per ordered pair) but
ERPNext refuses to create more than 3 (one per destination Company).

The ERPNext-canonical pattern is: one Internal Customer per
destination Company, with the `companies` ("Allowed To Transact With")
child table enumerating every source Company permitted to sell to it.
Symmetric on the supply side: one Internal Supplier per source
Company, with `companies` listing every target. Same intent (auto-
create the pair fabric for §10 internal transfers), ERPNext-aligned
cardinality (N + N instead of N × (N - 1)).

This module ships the ERPNext-aligned version. SPEC_10_patch_notes
captures the correction at §10 closeout.

Surface (whitelisted, FDE-facing):

  ensure_internal_party_pairs_for_account(account_name, confirm=True)
    Idempotent. For each EE-linked Company:
      - ONE Internal Customer (`INTL-CUST-for-{tgt}`) representing
        the Company, with `companies` listing every OTHER EE-linked
        Company as an allowed seller.
      - ONE Internal Supplier (`INTL-SUPP-from-{src}`) representing
        the Company, with `companies` listing every OTHER EE-linked
        Company as an allowed buyer.
    Returns a structured dict the JS handler renders.

After creating each Internal Customer, pushes it to EE via §8e's
existing /Wholesale/CreateCustomer machinery so the Customer Map
captures `ee_customer_id`. That id is what Stage 2's STN payload
references in the customer block per §10.G.

§8e extension note: §8e's push targets `/Wholesale/CreateCustomer`,
which is B2B/wholesale-flavored by URL. No `customer_type` argument
extension is needed at Stage 1. If Stage 2 STN testing reveals EE
silently requires a buyer-type discriminator we're not sending, that's
a STOP-and-report moment then.

Role-gated FDE / EasyEcom System Manager / System Manager.
"""

from __future__ import annotations

from typing import Any

import frappe


_ROLES_ALLOWED = {
    "System Manager",
    "EasyEcom System Manager",
    "EasyEcom FDE",
}


def _check_role(action_label: str) -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(_ROLES_ALLOWED):
        frappe.throw(
            frappe._(
                "{0} requires EasyEcom FDE or System Manager."
            ).format(action_label),
            frappe.PermissionError,
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _ee_linked_companies() -> list[str]:
    """Distinct Companies tied to a Live + enabled EasyEcom Location.

    Mapped-but-not-Live locations are still in onboarding — only Live
    counts toward the §10 pair grid."""
    rows = frappe.db.sql(
        """
        SELECT DISTINCT frappe_company
        FROM `tabEasyEcom Location`
        WHERE workflow_state = 'Live'
          AND enabled = 1
          AND frappe_company IS NOT NULL
          AND frappe_company != ''
        ORDER BY frappe_company
        """,
        as_dict=True,
    )
    return [r["frappe_company"] for r in rows]


def _default_customer_group() -> str:
    leaf = frappe.db.get_value(
        "Customer Group", {"is_group": 0}, "name"
    )
    if leaf:
        return leaf
    frappe.throw(
        frappe._(
            "No leaf Customer Group found. Create one before invoking "
            "ensure_internal_party_pairs_for_account."
        )
    )


def _default_supplier_group() -> str:
    leaf = frappe.db.get_value(
        "Supplier Group", {"is_group": 0}, "name"
    )
    if leaf:
        return leaf
    frappe.throw(
        frappe._(
            "No leaf Supplier Group found. Create one before invoking "
            "ensure_internal_party_pairs_for_account."
        )
    )


def internal_customer_name(target_company: str) -> str:
    """One Internal Customer per destination Company.

    Packet-renamed in Stage 1 build per ERPNext's single-Internal-
    Customer-per-represents_company constraint.
    """
    return f"INTL-CUST-for-{target_company}"


def internal_supplier_name(source_company: str) -> str:
    """One Internal Supplier per source Company."""
    return f"INTL-SUPP-from-{source_company}"


def _find_existing_internal_customer(
    *, target_company: str
) -> str | None:
    """Match on (is_internal_customer=1, represents_company=target).
    The customer_name is informational; ERPNext's unique-by-
    represents_company constraint is the authoritative lookup key."""
    return frappe.db.get_value(
        "Customer",
        {
            "is_internal_customer": 1,
            "represents_company": target_company,
        },
        "name",
    )


def _find_existing_internal_supplier(
    *, source_company: str
) -> str | None:
    return frappe.db.get_value(
        "Supplier",
        {
            "is_internal_supplier": 1,
            "represents_company": source_company,
        },
        "name",
    )


def _create_internal_customer(
    *, target_company: str, source_companies: list[str]
) -> str:
    """Insert a new Internal Customer representing `target_company`,
    with every source Company in the `companies` (Allowed To Transact
    With) child table."""
    doc = frappe.new_doc("Customer")
    doc.update(
        {
            "customer_name": internal_customer_name(target_company),
            "customer_type": "Company",
            "customer_group": _default_customer_group(),
            "is_internal_customer": 1,
            "represents_company": target_company,
            "companies": [
                {"company": src} for src in source_companies
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _create_internal_supplier(
    *, source_company: str, target_companies: list[str]
) -> str:
    doc = frappe.new_doc("Supplier")
    doc.update(
        {
            "supplier_name": internal_supplier_name(source_company),
            "supplier_type": "Company",
            "supplier_group": _default_supplier_group(),
            "is_internal_supplier": 1,
            "represents_company": source_company,
            "companies": [
                {"company": tgt} for tgt in target_companies
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _reconcile_companies_child_table(
    *, doctype: str, docname: str, expected: list[str]
) -> bool:
    """Ensure the `companies` child table on an existing Internal
    Customer/Supplier contains every expected entry. Adds missing
    rows; does NOT remove rows the FDE may have added manually for
    reasons outside the integration's view. Returns True if changes
    were made."""
    doc = frappe.get_doc(doctype, docname)
    existing = {row.company for row in (doc.companies or [])}
    missing = [c for c in expected if c not in existing]
    if not missing:
        return False
    for c in missing:
        doc.append("companies", {"company": c})
    doc.save(ignore_permissions=True)
    return True


def _push_internal_customer_to_ee(customer_docname: str) -> dict:
    """Reuses §8e's push_one_customer. Returns the PushOutcome as a
    dict for the audit Comment. If the push fails, reports it but
    does NOT abort pair creation — the FDE can retry the push later."""
    try:
        from ecommerce_super.easyecom.flows.customer_push import (
            push_one_customer,
        )
        outcome = push_one_customer(customer_docname)
        return {
            "pushed": bool(outcome.pushed),
            "operation": outcome.operation,
            "ee_customer_id": getattr(outcome, "ee_customer_id", None)
            or getattr(outcome, "ee_product_id", None),
            "flag_reasons": list(outcome.flag_reasons or []),
        }
    except Exception as exc:
        return {
            "pushed": False,
            "operation": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


@frappe.whitelist()
def ensure_internal_party_pairs_for_account(
    account_name: str, confirm: int | bool | str = True
) -> dict[str, Any]:
    """Whitelisted entrypoint. Idempotent. Audit-Commented on Account.

    Per-destination: one Internal Customer (represents_company=dest;
    companies=all other EE-linked Companies as sellers).
    Per-source: one Internal Supplier (represents_company=src;
    companies=all other EE-linked Companies as buyers).

    Returns:
      {
        "ok": True | False,
        "account": "...",
        "companies": [...],
        "internal_customers": [
          {
            "represents": "<tgt>",
            "name": "<docname>",
            "created": True | False,
            "companies_updated": True | False,
            "pushed_to_ee": {...}
          }, ...
        ],
        "internal_suppliers": [...],
        "summary": "...",
      }
    """
    _check_role("Ensure Internal Party Pairs")

    if not _truthy(confirm):
        return {
            "ok": False,
            "message": (
                "Confirmation required — pass confirm=true. This "
                "creates one Internal Customer per destination Company "
                "and one Internal Supplier per source Company across "
                "every EE-linked Company on this Account, and pushes "
                "each Internal Customer to EE via §8e."
            ),
        }

    if not account_name or not frappe.db.exists(
        "EasyEcom Account", account_name
    ):
        return {
            "ok": False,
            "message": f"Account {account_name!r} not found.",
        }

    companies = _ee_linked_companies()
    if len(companies) < 2:
        return {
            "ok": True,
            "account": account_name,
            "companies": companies,
            "internal_customers": [],
            "internal_suppliers": [],
            "summary": (
                f"Only {len(companies)} EE-linked Company found — "
                "need ≥2 to form a transfer pair. No-op."
            ),
        }

    customers_out: list[dict[str, Any]] = []
    suppliers_out: list[dict[str, Any]] = []
    created_count = 0
    existed_count = 0
    reconciled_count = 0

    # One Internal Customer per destination Company.
    for tgt in companies:
        sources = [c for c in companies if c != tgt]
        existing = _find_existing_internal_customer(target_company=tgt)
        if existing:
            existed_count += 1
            updated = _reconcile_companies_child_table(
                doctype="Customer",
                docname=existing,
                expected=sources,
            )
            if updated:
                reconciled_count += 1
            cust_docname = existing
            created = False
        else:
            cust_docname = _create_internal_customer(
                target_company=tgt, source_companies=sources
            )
            created = True
            created_count += 1
        push = _push_internal_customer_to_ee(cust_docname)
        customers_out.append(
            {
                "represents": tgt,
                "name": cust_docname,
                "created": created,
                "companies_updated": (existing is not None) and updated
                if existing
                else False,
                "pushed_to_ee": push,
            }
        )

    # One Internal Supplier per source Company.
    for src in companies:
        targets = [c for c in companies if c != src]
        existing = _find_existing_internal_supplier(source_company=src)
        if existing:
            existed_count += 1
            updated = _reconcile_companies_child_table(
                doctype="Supplier",
                docname=existing,
                expected=targets,
            )
            if updated:
                reconciled_count += 1
            sup_docname = existing
            created = False
        else:
            sup_docname = _create_internal_supplier(
                source_company=src, target_companies=targets
            )
            created = True
            created_count += 1
        suppliers_out.append(
            {
                "represents": src,
                "name": sup_docname,
                "created": created,
                "companies_updated": (existing is not None) and updated
                if existing
                else False,
            }
        )

    # Audit Comment on the Account doc.
    acct = frappe.get_doc("EasyEcom Account", account_name)
    summary_html = (
        "<b>§10 Internal Party fabric ensured</b> by "
        f"<code>{frappe.session.user}</code> across "
        f"{len(companies)} EE-linked Companies "
        f"(<code>{', '.join(frappe.utils.escape_html(c) for c in companies)}</code>).<br>"
        f"Created: {created_count}; pre-existing: {existed_count}; "
        f"companies-list reconciled: {reconciled_count}."
    )
    acct.add_comment(comment_type="Info", text=summary_html)
    frappe.db.commit()

    return {
        "ok": True,
        "account": account_name,
        "companies": companies,
        "internal_customers": customers_out,
        "internal_suppliers": suppliers_out,
        "summary": (
            f"{len(customers_out)} Internal Customers + "
            f"{len(suppliers_out)} Internal Suppliers ensured "
            f"across {len(companies)} EE-linked Companies. "
            f"Created {created_count} new; {existed_count} already "
            f"existed; {reconciled_count} reconciled."
        ),
    }
