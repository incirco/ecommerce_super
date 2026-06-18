"""Bootstrap an Internal Customer for a §10 stock-transfer pair.

ERPNext's stock-transfer-via-DN flow requires an Internal Customer
that represents the receiving Company AND is allowed to transact with
the sending Company. The same Customer also has to be pushable to EE
via §8e for the B2B branch of §10. The §8e ruleset enforces a list of
mandatory fields (email, mobile, currency, GST category/identifier,
billing+shipping state, postal codes, country) — every one of which
this bootstrap pre-populates so the resulting Customer is
push-to-EE-ready on creation.

What the bootstrap configures, end-to-end:

  Customer record:
    - customer_name (deterministic: "Internal - <target>")
    - is_internal_customer = 1
    - represents_company = target_company
    - email_id (placeholder unless overridden)
    - mobile_no (placeholder unless overridden)
    - default_currency (mirrored from target Company)
    - gst_category (mirrored from target Company)
    - gstin (mirrored from target Company when registered)
    - companies child: source_company added to
      "Allowed To Transact With"
    - customer_group, territory (Selling Settings defaults)

  Two linked Addresses (Billing + Shipping) mirroring the target
  Company's primary address (so §8e's address_type-strict lookups
  return non-empty values for billingState/billingPostalCode AND
  dispatchState/dispatchPostalCode).

`bootstrap_internal_customer` is idempotent: re-calling for the same
(source, target) pair returns the existing Customer name, adds the
source to `Allowed To Transact With` only if missing, and only
creates Billing/Shipping Addresses when they don't already exist.

Usage from the FDE worklist / console:

    bootstrap_internal_customer(
        source_company="Modern Marwar Pvt Ltd",
        target_company="Modern Marwar B2C",
    )

Or via HTTP for sites without shell access:

    POST /api/method/ecommerce_super.easyecom.customer.\
internal_customer_bootstrap.bootstrap_internal_customer
        ?source_company=...&target_company=...
"""
from __future__ import annotations

from typing import Any

import frappe


# Placeholder values used when the FDE doesn't supply real contact
# data. EE's CreateCustomer rejects empty strings for these fields
# (verified live against Harmony 2026-05-27), so we substitute
# non-empty values that are clearly synthetic. FDE can edit them
# post-creation if real values become available.
_PLACEHOLDER_EMAIL_DOMAIN = "internal-transfers.local"
_PLACEHOLDER_MOBILE = "9999900000"

# Fallback address scaffolding for cases where the target Company has
# no primary Address. India + a numeric pincode are the minimum §8e
# needs not to flag; FDE should update with real values when the
# target Company's address is configured.
_FALLBACK_ADDRESS = {
    "address_line1": "Internal Transfer Address (placeholder)",
    "city": "Mumbai",
    "state": "Maharashtra",
    "country": "India",
    "pincode": "400001",
}


@frappe.whitelist()
def bootstrap_internal_customer(
    *,
    source_company: str,
    target_company: str,
    customer_name: str | None = None,
    email_id: str | None = None,
    mobile_no: str | None = None,
    customer_group: str | None = None,
    territory: str | None = None,
) -> dict[str, Any]:
    """Create (or repair) the Internal Customer pair for a §10 transfer.

    The Customer produced here is configured to satisfy BOTH §10's
    routing predicates AND §8e's CreateCustomer push gate, so it can
    be pushed to EE without flagging.

    Args:
      source_company: Company that sends inventory (raises the DN).
      target_company: Company that receives inventory (represented
        by the Internal Customer).
      customer_name: override the auto name "Internal - <target>".
      email_id: override the placeholder email.
      mobile_no: override the placeholder mobile.
      customer_group: override default from Selling Settings.
      territory: override default from Selling Settings.

    Returns: see docstring of `_describe`.

    Raises:
      frappe.PermissionError: caller lacks System Manager.
      frappe.ValidationError: invalid Company inputs.
    """
    _check_permission()
    _validate_inputs(source_company, target_company)

    target_profile = _read_company_profile(target_company)
    name = customer_name or f"Internal - {target_company}"

    existing = _find_existing(target_company)
    if existing:
        added_atw = _ensure_allowed_to_transact_with(
            existing, source_company
        )
        ensured_billing, ensured_shipping = _ensure_addresses(
            existing, target_profile
        )
        return {
            "customer_name": existing,
            "created": False,
            "added_atw_row": added_atw,
            "added_billing_address": ensured_billing,
            "added_shipping_address": ensured_shipping,
            "details": _describe(existing),
        }

    # New customer creation.
    slug = _slugify(target_company)
    resolved_email = email_id or (
        f"internal-{slug}@{_PLACEHOLDER_EMAIL_DOMAIN}"
    )
    resolved_mobile = mobile_no or _PLACEHOLDER_MOBILE
    resolved_group = customer_group or _default_customer_group()
    resolved_territory = territory or _default_territory()

    doc = frappe.new_doc("Customer")
    doc.update(
        {
            "customer_name": name,
            "customer_type": "Company",
            "customer_group": resolved_group,
            "territory": resolved_territory,
            "is_internal_customer": 1,
            "represents_company": target_company,
            "email_id": resolved_email,
            "mobile_no": resolved_mobile,
            "default_currency": target_profile["default_currency"],
            "gst_category": target_profile["gst_category"],
            "gstin": target_profile["gstin"],
        }
    )
    doc.append("companies", {"company": source_company})
    doc.flags.ignore_permissions = True
    doc.insert()

    # Addresses are created in a separate step so they reference the
    # now-existing Customer.name via Dynamic Link.
    ensured_billing, ensured_shipping = _ensure_addresses(
        doc.name, target_profile
    )
    frappe.db.commit()

    return {
        "customer_name": doc.name,
        "created": True,
        "added_atw_row": True,
        "added_billing_address": ensured_billing,
        "added_shipping_address": ensured_shipping,
        "details": _describe(doc.name),
    }


def _check_permission() -> None:
    user = frappe.session.user
    if user == "Administrator":
        return
    roles = frappe.get_roles(user)
    if "System Manager" in roles:
        return
    frappe.throw(
        "bootstrap_internal_customer requires the System Manager role.",
        frappe.PermissionError,
    )


def _validate_inputs(source_company: str, target_company: str) -> None:
    if not source_company or not target_company:
        frappe.throw(
            "Both source_company and target_company are required.",
            frappe.ValidationError,
        )
    if source_company == target_company:
        frappe.throw(
            "source_company and target_company must differ — an "
            "Internal Customer represents a counterparty Company.",
            frappe.ValidationError,
        )
    for company in (source_company, target_company):
        if not frappe.db.exists("Company", company):
            frappe.throw(
                f"Company {company!r} does not exist.",
                frappe.ValidationError,
            )


def _find_existing(target_company: str) -> str | None:
    return frappe.db.get_value(
        "Customer",
        {
            "is_internal_customer": 1,
            "represents_company": target_company,
            "disabled": 0,
        },
        "name",
    )


def _ensure_allowed_to_transact_with(
    customer_name: str, source_company: str
) -> bool:
    """Idempotently add `source_company` to the customer's
    `companies` (Allowed To Transact With) child table.

    Returns True iff a new row was added.
    """
    existing_companies = [
        row.company for row in frappe.get_all(
            "Allowed To Transact With",
            filters={"parent": customer_name},
            fields=["company"],
        )
    ]
    if source_company in existing_companies:
        return False
    doc = frappe.get_doc("Customer", customer_name)
    doc.append("companies", {"company": source_company})
    doc.flags.ignore_permissions = True
    doc.save()
    return True


def _read_company_profile(company: str) -> dict[str, Any]:
    """Read the fields we want to mirror onto the Internal Customer
    so it doesn't trip §8e's CreateCustomer gate.

    Returns a dict with: default_currency, gst_category, gstin, and
    a nested `address` dict containing address_line1, city, state,
    country, pincode. Missing fields fall back to the
    `_FALLBACK_ADDRESS` so downstream consumers never see empty
    strings for the §8e-mandatory fields.
    """
    company_row = frappe.db.get_value(
        "Company",
        company,
        ["default_currency", "country", "gst_category", "gstin"],
        as_dict=True,
    ) or {}
    address = _read_company_primary_address(company)
    return {
        "default_currency": company_row.get("default_currency") or "INR",
        "country": (
            company_row.get("country")
            or address.get("country")
            or "India"
        ),
        "gst_category": (
            company_row.get("gst_category") or "Unregistered"
        ),
        "gstin": company_row.get("gstin") or None,
        "address": {
            "address_line1": (
                address.get("address_line1")
                or _FALLBACK_ADDRESS["address_line1"]
            ),
            "city": address.get("city") or _FALLBACK_ADDRESS["city"],
            "state": address.get("state") or _FALLBACK_ADDRESS["state"],
            "country": (
                address.get("country")
                or company_row.get("country")
                or _FALLBACK_ADDRESS["country"]
            ),
            "pincode": (
                address.get("pincode") or _FALLBACK_ADDRESS["pincode"]
            ),
        },
    }


def _read_company_primary_address(company: str) -> dict[str, Any]:
    """Find the Company's primary Address via Dynamic Link, prefer
    is_primary_address. Returns {} if no Address is linked."""
    rows = frappe.db.sql(
        """
        SELECT a.address_line1, a.city, a.pincode, a.state, a.country
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Company'
          AND dl.link_name = %s
        ORDER BY a.is_primary_address DESC, a.creation ASC
        LIMIT 1
        """,
        (company,),
        as_dict=True,
    )
    return rows[0] if rows else {}


def _ensure_addresses(
    customer_name: str, target_profile: dict[str, Any]
) -> tuple[bool, bool]:
    """Make sure the Customer has BOTH a Billing AND a Shipping
    Address linked via Dynamic Link, populated from
    `target_profile['address']`. §8e's `_find_address` is strict on
    `address_type`, so we materialize both rows even when the data
    is identical.

    Returns (added_billing, added_shipping) — True for each row we
    actually created.
    """
    addr_data = target_profile["address"]
    added_billing = _ensure_address_of_type(
        customer_name, address_type="Billing", data=addr_data
    )
    added_shipping = _ensure_address_of_type(
        customer_name, address_type="Shipping", data=addr_data
    )
    return added_billing, added_shipping


def _ensure_address_of_type(
    customer_name: str, *, address_type: str, data: dict[str, Any]
) -> bool:
    """Create-if-missing an Address of `address_type` linked to the
    Customer. Idempotent."""
    existing = frappe.db.sql(
        """
        SELECT a.name
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Customer'
          AND dl.link_name = %s
          AND a.address_type = %s
        LIMIT 1
        """,
        (customer_name, address_type),
    )
    if existing:
        return False

    addr = frappe.new_doc("Address")
    addr.update(
        {
            "address_title": f"{customer_name} ({address_type})",
            "address_type": address_type,
            "address_line1": data["address_line1"],
            "city": data["city"],
            "state": data["state"],
            "country": data["country"],
            "pincode": data["pincode"],
            # Mark Billing as primary so cosmetic UI bits pick it up.
            "is_primary_address": 1 if address_type == "Billing" else 0,
            "is_shipping_address": (
                1 if address_type == "Shipping" else 0
            ),
        }
    )
    addr.append(
        "links",
        {"link_doctype": "Customer", "link_name": customer_name},
    )
    addr.flags.ignore_permissions = True
    addr.insert()
    return True


def _describe(customer_name: str) -> dict[str, Any]:
    customer = frappe.db.get_value(
        "Customer",
        customer_name,
        [
            "represents_company", "email_id", "mobile_no",
            "default_currency", "gst_category", "gstin",
        ],
        as_dict=True,
    ) or {}
    atw = [
        row.company for row in frappe.get_all(
            "Allowed To Transact With",
            filters={"parent": customer_name},
            fields=["company"],
        )
    ]
    addresses = frappe.db.sql(
        """
        SELECT a.address_type, a.state, a.pincode, a.country
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Customer'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        """,
        (customer_name,),
        as_dict=True,
    )
    return {
        "represents_company": customer.get("represents_company"),
        "allowed_to_transact_with": atw,
        "email_id": customer.get("email_id"),
        "mobile_no": customer.get("mobile_no"),
        "default_currency": customer.get("default_currency"),
        "gst_category": customer.get("gst_category"),
        "gstin": customer.get("gstin"),
        "addresses": [dict(a) for a in addresses],
    }


def _default_customer_group() -> str:
    setting = frappe.db.get_single_value(
        "Selling Settings", "customer_group"
    )
    if setting:
        return setting
    fallback = frappe.db.get_value(
        "Customer Group", {"is_group": 0}, "name"
    )
    return fallback or "All Customer Groups"


def _default_territory() -> str:
    setting = frappe.db.get_single_value(
        "Selling Settings", "territory"
    )
    if setting:
        return setting
    fallback = frappe.db.get_value(
        "Territory", {"is_group": 0}, "name"
    )
    return fallback or "All Territories"


def _slugify(s: str) -> str:
    """Lowercase + replace non-alphanumeric with dashes. Used only
    for placeholder-email local parts — no need for full RFC
    escaping."""
    out: list[str] = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "customer"
