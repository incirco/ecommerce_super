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


# Tax category candidate names — tried in order; first existing one wins.
# India Compliance ships "In-State" / "Out-State" (no "of"); some benches
# rename to "Out-of-State" or "Inter-State". The resolver tries multiple
# conventional names so the bootstrap works without manual setup.
# If none match, the pool Customer is created with no tax_category and
# the FDE re-points manually.
TAX_CATEGORY_CANDIDATES_IN_STATE: tuple[str, ...] = (
    "In-State",
    "Intra-State",
)
TAX_CATEGORY_CANDIDATES_OUT_OF_STATE: tuple[str, ...] = (
    "Out-State",
    "Out-of-State",
    "Inter-State",
)

# Back-compat alias for tests / external code.
DEFAULT_TAX_CATEGORY_IN_STATE: str = TAX_CATEGORY_CANDIDATES_IN_STATE[0]
DEFAULT_TAX_CATEGORY_OUT_OF_STATE: str = TAX_CATEGORY_CANDIDATES_OUT_OF_STATE[0]


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
        tax_category=_resolve_tax_category_from(TAX_CATEGORY_CANDIDATES_IN_STATE),
    )
    out_of_state_name = _bootstrap_one_customer(
        customer_name=f"{label} B2C Out-of-State - {company}",
        tax_category=_resolve_tax_category_from(TAX_CATEGORY_CANDIDATES_OUT_OF_STATE),
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
    """Return the preferred Tax Category if it exists; otherwise None.

    Kept as a single-candidate wrapper for back-compat with existing
    tests + callers. New code should use _resolve_tax_category_from()
    which takes a tuple of candidates."""
    return _resolve_tax_category_from((preferred,))


def _resolve_tax_category_from(candidates: tuple[str, ...]) -> str | None:
    """Try each candidate Tax Category in order; return the first one
    that exists. Returns None if none exist — the pool Customer gets
    no tax_category and the FDE re-points manually.

    Bench-portability: India Compliance ships some names; clients
    sometimes rename. Trying multiple candidates means the bootstrap
    works out of the box on most installations."""
    for candidate in candidates:
        try:
            if frappe.db.exists("Tax Category", candidate):
                return candidate
        except Exception:
            return None
    return None


def _resolve_default_group() -> str:
    """Return a non-group (leaf) Customer Group that exists.

    ERPNext rejects parent / group-type Customer Groups on Customer
    rows — only leaf groups are valid. The candidate list is tried in
    order; any candidate that's a group itself ('All Customer Groups',
    'Commercial' on some seeded benches) is skipped.

    Falls back to any leaf group on the bench. ERPNext sites with
    no leaf Customer Group are pathological — last-resort returns
    'Individual' (the standard Frappe leaf default)."""
    for candidate in ("Individual", "Commercial", "All Customer Groups"):
        if frappe.db.exists("Customer Group", candidate):
            is_group = frappe.db.get_value(
                "Customer Group", candidate, "is_group"
            )
            if not is_group:
                return candidate
    # Generic leaf fallback
    fallback = frappe.db.get_value(
        "Customer Group", {"is_group": 0}, "name"
    )
    return fallback or "Individual"


def _resolve_default_territory() -> str:
    """Return a non-group (leaf) Territory that exists. Same rule as
    `_resolve_default_group` — ERPNext rejects parent / group-type
    Territory rows on Customers. Tries candidates in order; falls
    back to any leaf Territory."""
    for candidate in ("India", "All Territories"):
        if frappe.db.exists("Territory", candidate):
            is_group = frappe.db.get_value(
                "Territory", candidate, "is_group"
            )
            if not is_group:
                return candidate
    fallback = frappe.db.get_value(
        "Territory", {"is_group": 0}, "name"
    )
    return fallback or "All Territories"
