"""§12 — EasyEcom Marketplace Account controller.

Per-(Company, Marketplace) seller configuration. v1 scope is the §12
B2C polling substrate; settlement/reconciliation fields (rate cards,
GSTIN, settlement template) deferred to recon-engine build per
SPEC §8.6.2 line 1860.

Lifecycle hook: after_insert auto-creates TWO per-account pseudo
Customers — one for in-state buyers (drives CGST+SGST via tax_category)
and one for out-of-state buyers (drives IGST). Every B2C SI minted
via this Marketplace Account points at one of these two pool
Customers depending on the buyer's shipping address state vs the
Company's state. The actual buyer's address goes on the SI's Shipping
Address; the pool Customer is a tax-category carrier only.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


# Tax category names India Compliance ships by default. If a client
# renames their tax categories, the FDE re-points the pseudo customers
# to use the local names — the bootstrap defaults to these conventional
# values; resolver falls back to no tax_category if neither exists.
DEFAULT_TAX_CATEGORY_IN_STATE: str = "In-State"
DEFAULT_TAX_CATEGORY_OUT_OF_STATE: str = "Out-of-State"


class EasyEcomMarketplaceAccount(Document):

    def after_insert(self) -> None:
        """Bootstrap the two pseudo Customers + cursor on first insert.

        Idempotent — if a Customer with the conventional name already
        exists, we link it. Never raises (a failed bootstrap must not
        prevent the Marketplace Account from being created; the FDE can
        re-trigger via a manual save).
        """
        try:
            in_state_name, out_of_state_name = _bootstrap_pseudo_customers(
                marketplace=self.marketplace,
                company=self.company,
            )
            updates: dict = {}
            if in_state_name:
                updates["pseudo_customer_in_state"] = in_state_name
            if out_of_state_name:
                updates["pseudo_customer_out_of_state"] = out_of_state_name
            if updates:
                for k, v in updates.items():
                    self.db_set(k, v, update_modified=False)
        except Exception as exc:
            frappe.log_error(
                title=f"§12 Marketplace Account bootstrap: {self.name}",
                message=f"Pseudo-customer bootstrap failed: {type(exc).__name__}: {exc}",
            )

        # Cursor — forward-only cutover. Historical orders before this
        # timestamp are §102 backfill territory.
        if not self.last_pull_orders:
            self.db_set(
                "last_pull_orders", now_datetime(), update_modified=False,
            )


def _bootstrap_pseudo_customers(
    *, marketplace: str, company: str,
) -> tuple[str | None, str | None]:
    """Idempotently create both pool Customers (in-state + out-of-state)
    for one (Company, Marketplace).

    Naming:
      - "<Marketplace label> B2C In-State - <Company>"
      - "<Marketplace label> B2C Out-of-State - <Company>"

    Tax categories applied:
      - In-State pool → DEFAULT_TAX_CATEGORY_IN_STATE if exists
      - Out-of-State pool → DEFAULT_TAX_CATEGORY_OUT_OF_STATE if exists

    Returns (in_state_customer_name, out_of_state_customer_name) —
    either can be None if creation fails.
    """
    if not marketplace or not company:
        return (None, None)

    mp_display = (
        frappe.db.get_value(
            "Marketplace", marketplace,
            ["display_name", "marketplace_name"],
            as_dict=True,
        ) or {}
    )
    label = (
        mp_display.get("display_name")
        or mp_display.get("marketplace_name")
        or marketplace
    )

    in_state_name = _bootstrap_one_customer(
        customer_name=f"{label} B2C In-State - {company}",
        tax_category=_resolve_tax_category(DEFAULT_TAX_CATEGORY_IN_STATE),
    )
    out_of_state_name = _bootstrap_one_customer(
        customer_name=f"{label} B2C Out-of-State - {company}",
        tax_category=_resolve_tax_category(DEFAULT_TAX_CATEGORY_OUT_OF_STATE),
    )
    return (in_state_name, out_of_state_name)


def _bootstrap_one_customer(
    *, customer_name: str, tax_category: str | None,
) -> str | None:
    """Idempotently insert one pool Customer with sensible defaults."""
    existing = frappe.db.exists("Customer", customer_name)
    if existing:
        return customer_name

    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name,
        "customer_type": "Individual",
        "customer_group": _resolve_default_group(),
        "territory": _resolve_default_territory(),
        "tax_category": tax_category,
    })
    customer.flags.ignore_permissions = True
    customer.insert(ignore_if_duplicate=True)
    return customer.name


def _resolve_tax_category(preferred: str) -> str | None:
    """Return the preferred Tax Category if it exists; otherwise None
    so ERPNext applies whatever default the Company / SI implies. If
    a client renames their tax categories, they re-point the pseudo
    Customers manually."""
    try:
        if frappe.db.exists("Tax Category", preferred):
            return preferred
    except Exception:
        return None
    return None


def _resolve_default_group() -> str:
    """Return a Customer Group that exists. Prefers 'Commercial' (Frappe
    default); falls back to whatever first group exists. ERPNext sites
    always have at least one Customer Group."""
    for candidate in ("Commercial", "All Customer Groups", "Individual"):
        if frappe.db.exists("Customer Group", candidate):
            return candidate
    fallback = frappe.db.get_value("Customer Group", {}, "name")
    return fallback or "Commercial"


def _resolve_default_territory() -> str:
    """Return a Territory that exists. Prefers 'India'; falls back to
    'All Territories' (always present in ERPNext sites)."""
    for candidate in ("India", "All Territories"):
        if frappe.db.exists("Territory", candidate):
            return candidate
    fallback = frappe.db.get_value("Territory", {}, "name")
    return fallback or "All Territories"
