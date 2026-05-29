"""§10 Stage 1 — pre-go-live readiness check for the Stock Transfer flow.

Parity with §9's precheck_buying_go_live. Returns a structured dict:

  { ok, account, blockers, warnings, checked }

Blockers must clear before §10 go-live; warnings are FDE-judgement.

Checks (blockers):
  1. Account exists.
  2. ≥2 EE-linked Companies (need a transfer pair to ever fire).
  3. default_in_transit_warehouse set on the Account (GIT parking
     warehouse for inbound IPRs).
  4. default_rejected_warehouse set on the Account (qc_fail bucket).
  5. lost_in_transit_threshold_days set (aged GIT detection).
  6. Internal Customer + Supplier pair exists for every (src, tgt)
     ordered pair of EE-linked Companies. Precheck does NOT
     auto-create — it reports missing pairs so the FDE explicitly
     invokes ensure_internal_party_pairs_for_account.
  7. Every Internal Customer has been pushed to EE (Customer Map row
     with ee_customer_id captured). The STN payload references this
     id in the customer block per §10.G.

Checks (warnings):
  - §9 precheck output for the same account — §10 Stage 2 outbound
    PO branch reuses §9 push machinery, so §9 must be go-live ready
    before §10 can fully fire. Surfaced as warning, not blocker:
    §10 STN-only deployments (every source EE-mapped) won't hit the
    PO branch.

Read-only. No mutation. Safe to call repeatedly.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.api.internal_party_pairs import (
    _ee_linked_companies,
    _find_existing_internal_customer,
    _find_existing_internal_supplier,
    internal_customer_name,
    internal_supplier_name,
)


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


@frappe.whitelist()
def precheck_section10_go_live(account_name: str) -> dict[str, Any]:
    _check_role("§10 Stock Transfer Pre-Go-Live Check")

    if not account_name or not frappe.db.exists(
        "EasyEcom Account", account_name
    ):
        return {
            "ok": False,
            "account": account_name,
            "blockers": [f"Account {account_name!r} not found."],
            "warnings": [],
            "checked": [],
        }

    blockers: list[str] = []
    warnings: list[str] = []
    checked: list[str] = []

    companies = _ee_linked_companies()
    _check_companies(companies, blockers, checked)
    _check_account_defaults(account_name, blockers, checked)
    if len(companies) >= 2:
        _check_internal_pairs(companies, blockers, checked)
    _add_section9_warning(account_name, warnings)

    return {
        "ok": not blockers,
        "account": account_name,
        "blockers": blockers,
        "warnings": warnings,
        "checked": checked,
    }


def _check_companies(
    companies: list[str], blockers: list[str], checked: list[str]
) -> None:
    if len(companies) < 2:
        blockers.append(
            f"Only {len(companies)} EE-linked Company found on Live "
            "Locations. §10 requires at least 2 to form a transfer "
            "pair. Map and Go-Live additional Locations to enable "
            "Stock Transfer flows."
        )
    else:
        checked.append(
            f"{len(companies)} EE-linked Companies found: "
            f"{', '.join(repr(c) for c in companies)} ✓"
        )


def _check_account_defaults(
    account_name: str, blockers: list[str], checked: list[str]
) -> None:
    """default_in_transit_warehouse, default_rejected_warehouse,
    lost_in_transit_threshold_days — Account-level. §3.3.6."""
    row = frappe.db.get_value(
        "EasyEcom Account",
        account_name,
        [
            "default_in_transit_warehouse",
            "default_rejected_warehouse",
            "lost_in_transit_threshold_days",
        ],
        as_dict=True,
    )
    if not row.default_in_transit_warehouse:
        blockers.append(
            "EasyEcom Account → default_in_transit_warehouse is not "
            "set. §10 inbound parks stock in this Warehouse on DN "
            "submit (GIT). Pick a warehouse on the Account form before "
            "going live."
        )
    elif not frappe.db.exists(
        "Warehouse", row.default_in_transit_warehouse
    ):
        blockers.append(
            f"EasyEcom Account → default_in_transit_warehouse = "
            f"{row.default_in_transit_warehouse!r}, but no Warehouse "
            "with that name exists."
        )
    else:
        checked.append(
            f"EasyEcom Account → default_in_transit_warehouse = "
            f"{row.default_in_transit_warehouse!r} ✓"
        )

    if not row.default_rejected_warehouse:
        blockers.append(
            "EasyEcom Account → default_rejected_warehouse is not "
            "set. §10 inbound IPRs route qc_fail qty here. Pick a "
            "warehouse on the Account form before going live."
        )
    elif not frappe.db.exists(
        "Warehouse", row.default_rejected_warehouse
    ):
        blockers.append(
            f"EasyEcom Account → default_rejected_warehouse = "
            f"{row.default_rejected_warehouse!r}, but no Warehouse "
            "with that name exists."
        )
    else:
        checked.append(
            f"EasyEcom Account → default_rejected_warehouse = "
            f"{row.default_rejected_warehouse!r} ✓"
        )

    if not row.lost_in_transit_threshold_days or int(
        row.lost_in_transit_threshold_days
    ) <= 0:
        blockers.append(
            "EasyEcom Account → lost_in_transit_threshold_days is "
            "missing or non-positive. §10 Stage 4 aged-GIT detection "
            "needs a positive day count (default 30)."
        )
    else:
        checked.append(
            f"EasyEcom Account → lost_in_transit_threshold_days = "
            f"{int(row.lost_in_transit_threshold_days)} ✓"
        )


def _check_internal_pairs(
    companies: list[str], blockers: list[str], checked: list[str]
) -> None:
    """ERPNext-aligned model (Stage 1 packet correction):
      - ONE Internal Customer per destination Company.
      - ONE Internal Supplier per source Company.

    Verify each exists AND every Internal Customer has been pushed to
    EE (ee_customer_id captured on Customer Map). The STN payload
    references the id in the customer block (§10.G); without it, EE
    rejects the order."""
    missing_customers: list[str] = []
    missing_suppliers: list[str] = []
    customers_without_ee_id: list[str] = []
    customers_ok = 0
    suppliers_ok = 0

    for tgt in companies:
        cust_docname = _find_existing_internal_customer(target_company=tgt)
        if not cust_docname:
            missing_customers.append(
                f"destination {tgt!r} (expected customer_name = "
                f"{internal_customer_name(tgt)!r})"
            )
        else:
            customers_ok += 1
            ee_id = frappe.db.get_value(
                "EasyEcom Customer Map",
                {
                    "erpnext_doctype": "Customer",
                    "erpnext_name": cust_docname,
                },
                "ee_customer_id",
            )
            if not ee_id:
                customers_without_ee_id.append(
                    f"{cust_docname} (represents {tgt})"
                )

    for src in companies:
        sup_docname = _find_existing_internal_supplier(source_company=src)
        if not sup_docname:
            missing_suppliers.append(
                f"source {src!r} (expected supplier_name = "
                f"{internal_supplier_name(src)!r})"
            )
        else:
            suppliers_ok += 1

    if missing_customers:
        blockers.append(
            "Missing Internal Customer rows for the following "
            "destination Companies: "
            + "; ".join(missing_customers)
            + ". Invoke ensure_internal_party_pairs_for_account "
            "to create them."
        )
    if missing_suppliers:
        blockers.append(
            "Missing Internal Supplier rows for the following "
            "source Companies: "
            + "; ".join(missing_suppliers)
            + ". Invoke ensure_internal_party_pairs_for_account "
            "to create them."
        )
    if customers_without_ee_id:
        blockers.append(
            "The following Internal Customers exist but have NOT "
            "been pushed to EE (no ee_customer_id on Customer Map): "
            + "; ".join(customers_without_ee_id)
            + ". The §10 STN payload references this id in the "
            "customer block; without it, EE will reject the order. "
            "Invoke ensure_internal_party_pairs_for_account (which "
            "also pushes) or push each customer manually."
        )
    if (
        customers_ok == len(companies)
        and suppliers_ok == len(companies)
        and not customers_without_ee_id
    ):
        checked.append(
            f"All {len(companies)} Internal Customers + "
            f"{len(companies)} Internal Suppliers exist; every "
            "customer has ee_customer_id ✓"
        )


def _add_section9_warning(account_name: str, warnings: list[str]) -> None:
    """§10 Stage 2 outbound PO branch reuses §9 machinery. If §9 isn't
    go-live ready, that branch can't fire. STN-only deployments (every
    source EE-mapped) don't need §9 ready, so this is a warning, not
    blocker."""
    try:
        from ecommerce_super.easyecom.api.buying_precheck import (
            precheck_buying_go_live,
        )
        s9 = precheck_buying_go_live(account_name)
        if not s9.get("ok"):
            warnings.append(
                "§9 buying precheck reports blockers — §10's PO-branch "
                "outbound path (source NOT EE-mapped, target EE-mapped) "
                "reuses §9 machinery and won't fire until §9 is go-live "
                "ready. STN-only deployments (every source EE-mapped) "
                "can ship without §9 cleared. §9 blockers: "
                + "; ".join(s9.get("blockers", []))
            )
    except Exception as exc:
        warnings.append(
            f"Could not run §9 precheck for cross-check: "
            f"{type(exc).__name__}: {exc}"
        )
