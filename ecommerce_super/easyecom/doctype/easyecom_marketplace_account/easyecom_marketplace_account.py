"""§12 — EasyEcom Marketplace Account controller.

Per-(Company, Marketplace) seller configuration. v1 scope is the §12
B2C polling substrate; settlement/reconciliation fields (rate cards,
GSTIN, settlement template) deferred to recon-engine build per
SPEC §8.6.2 line 1860.

Lifecycle hook: after_insert auto-creates a per-account pseudo
Customer named "<Marketplace> B2C Pool - <Company>" so every B2C
SI minted via this Marketplace Account points at the same Customer
master row (the actual buyer address goes on the SI's Shipping
Address, not the Customer record).
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class EasyEcomMarketplaceAccount(Document):

    def after_insert(self) -> None:
        """Bootstrap the pseudo Customer + cursor on first insert.

        Idempotent — if a Customer with the conventional name already
        exists, we link it. Never raises (a failed bootstrap must not
        prevent the Marketplace Account from being created; the FDE can
        re-trigger via a manual save or the Setup wizard).
        """
        try:
            customer_name = _bootstrap_pseudo_customer(
                marketplace=self.marketplace,
                company=self.company,
            )
            if customer_name:
                self.db_set(
                    "pseudo_customer", customer_name, update_modified=False,
                )
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


def _bootstrap_pseudo_customer(*, marketplace: str, company: str) -> str | None:
    """Idempotently create the per-(Company, Marketplace) pool Customer.

    Naming: "<Marketplace display_name or name> B2C Pool - <Company>"

    Defaults applied:
      - customer_group = "Commercial" (Frappe default group; FDE can
        re-classify if a "B2C Marketplace" group is created later)
      - customer_type = "Individual" (B2C buyers are not companies)
      - territory = "India" (B2C orders are predominantly domestic;
        per-order Shipping Address carries the actual state)

    Returns the Customer docname (existing or newly created), or None
    if the marketplace / company couldn't be resolved.
    """
    if not marketplace or not company:
        return None

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
    customer_name = f"{label} B2C Pool - {company}"

    existing = frappe.db.exists("Customer", customer_name)
    if existing:
        return customer_name

    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name,
        "customer_type": "Individual",
        "customer_group": _resolve_default_group(),
        "territory": _resolve_default_territory(),
    })
    customer.flags.ignore_permissions = True
    customer.insert(ignore_if_duplicate=True)
    return customer.name


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
