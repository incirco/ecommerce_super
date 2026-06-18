"""Bootstrap an Internal Supplier for a §10 stock-transfer pair.

Symmetric to `internal_customer_bootstrap`. §10's inbound side
(`transfer_inbound.py`) materialises a PR keyed on an Internal
Supplier that represents the *source* Company (the sender of the
inventory). Without it, the GRN handler refuses with

  "§10 Internal Supplier missing for source Company X → target
   Company Y."

For single-Company deployments, the same Company plays both roles —
the Supplier ends up representing the only Company AND being allowed
to transact with that same Company.

What the bootstrap configures, end-to-end:

  Supplier record:
    - supplier_name (deterministic: "Internal Supplier - <source>")
    - is_internal_supplier = 1
    - represents_company = source_company
    - email_id (placeholder unless overridden)
    - mobile_no (placeholder unless overridden)
    - default_currency (mirrored from source Company)
    - gst_category (mirrored from source Company)
    - gstin (mirrored from source Company when registered)
    - companies child: target_company added to "Allowed To Transact With"
    - supplier_group (Buying Settings default)

  Two linked Addresses (Billing + Shipping) mirroring the source
  Company's primary address so §10's inbound PR carries valid
  state/pincode/country for India Compliance.

Idempotent — re-runs add only what's missing.

Usage from the FDE worklist / console:

    bootstrap_internal_supplier(
        source_company="Modern Marwar Pvt Ltd",
        target_company="Modern Marwar B2C",
    )

Or via HTTP:

    POST /api/method/ecommerce_super.easyecom.customer.\
internal_supplier_bootstrap.bootstrap_internal_supplier
        ?source_company=...&target_company=...
"""
from __future__ import annotations

from typing import Any

import frappe


_PLACEHOLDER_EMAIL_DOMAIN = "internal-transfers.local"
_PLACEHOLDER_MOBILE = "9999900000"

_FALLBACK_ADDRESS = {
    "address_line1": "Internal Transfer Address (placeholder)",
    "city": "Mumbai",
    "state": "Maharashtra",
    "country": "India",
    "pincode": "400001",
}


@frappe.whitelist()
def bootstrap_internal_supplier(
    *,
    source_company: str,
    target_company: str,
    supplier_name: str | None = None,
    email_id: str | None = None,
    mobile_no: str | None = None,
    supplier_group: str | None = None,
) -> dict[str, Any]:
    """Create (or repair) the Internal Supplier pair for a §10 transfer.

    Args:
      source_company: Company that sends inventory — represented by
        the Internal Supplier.
      target_company: Company that receives inventory; added to the
        Supplier's "Allowed To Transact With".
      supplier_name: override the auto name "Internal Supplier -
        <source>".
      email_id: override the placeholder.
      mobile_no: override the placeholder.
      supplier_group: override default from Buying Settings.

    Returns: see `_describe`.
    """
    _check_permission()
    _validate_inputs(source_company, target_company)

    source_profile = _read_company_profile(source_company)
    name = supplier_name or f"Internal Supplier - {source_company}"

    existing = _find_existing(source_company)
    if existing:
        added_atw = _ensure_allowed_to_transact_with(
            existing, target_company
        )
        ensured_billing, ensured_shipping = _ensure_addresses(
            existing, source_profile
        )
        return {
            "supplier_name": existing,
            "created": False,
            "added_atw_row": added_atw,
            "added_billing_address": ensured_billing,
            "added_shipping_address": ensured_shipping,
            "details": _describe(existing),
        }

    slug = _slugify(source_company)
    resolved_email = email_id or (
        f"internal-supplier-{slug}@{_PLACEHOLDER_EMAIL_DOMAIN}"
    )
    resolved_mobile = mobile_no or _PLACEHOLDER_MOBILE
    resolved_group = supplier_group or _default_supplier_group()

    doc = frappe.new_doc("Supplier")
    doc.update(
        {
            "supplier_name": name,
            "supplier_type": "Company",
            "supplier_group": resolved_group,
            "is_internal_supplier": 1,
            "represents_company": source_company,
            "email_id": resolved_email,
            "mobile_no": resolved_mobile,
            "default_currency": source_profile["default_currency"],
            "gst_category": source_profile["gst_category"],
            "gstin": source_profile["gstin"],
        }
    )
    doc.append("companies", {"company": target_company})
    doc.flags.ignore_permissions = True
    doc.insert()

    ensured_billing, ensured_shipping = _ensure_addresses(
        doc.name, source_profile
    )
    frappe.db.commit()

    return {
        "supplier_name": doc.name,
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
        "bootstrap_internal_supplier requires the System Manager role.",
        frappe.PermissionError,
    )


def _validate_inputs(source_company: str, target_company: str) -> None:
    """Single-Company deployments use one Internal Supplier that both
    represents and is allowed to transact with the only Company —
    source == target is allowed."""
    if not source_company or not target_company:
        frappe.throw(
            "Both source_company and target_company are required.",
            frappe.ValidationError,
        )
    for company in (source_company, target_company):
        if not frappe.db.exists("Company", company):
            frappe.throw(
                f"Company {company!r} does not exist.",
                frappe.ValidationError,
            )


def _find_existing(source_company: str) -> str | None:
    return frappe.db.get_value(
        "Supplier",
        {
            "is_internal_supplier": 1,
            "represents_company": source_company,
            "disabled": 0,
        },
        "name",
    )


def _ensure_allowed_to_transact_with(
    supplier_name: str, target_company: str
) -> bool:
    """Idempotently add `target_company` to the Supplier's
    `companies` (Allowed To Transact With) child table. Returns True
    iff a new row was added."""
    existing_companies = [
        row.company for row in frappe.get_all(
            "Allowed To Transact With",
            filters={"parent": supplier_name},
            fields=["company"],
        )
    ]
    if target_company in existing_companies:
        return False
    doc = frappe.get_doc("Supplier", supplier_name)
    doc.append("companies", {"company": target_company})
    doc.flags.ignore_permissions = True
    doc.save()
    return True


def _read_company_profile(company: str) -> dict[str, Any]:
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
    supplier_name: str, source_profile: dict[str, Any]
) -> tuple[bool, bool]:
    """Both Billing + Shipping Addresses are linked to the Supplier
    via Dynamic Link, populated from the source Company's primary
    Address (or fallback)."""
    addr_data = source_profile["address"]
    added_billing = _ensure_address_of_type(
        supplier_name, address_type="Billing", data=addr_data
    )
    added_shipping = _ensure_address_of_type(
        supplier_name, address_type="Shipping", data=addr_data
    )
    return added_billing, added_shipping


def _ensure_address_of_type(
    supplier_name: str, *, address_type: str, data: dict[str, Any]
) -> bool:
    existing = frappe.db.sql(
        """
        SELECT a.name
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
          AND a.address_type = %s
        LIMIT 1
        """,
        (supplier_name, address_type),
    )
    if existing:
        return False

    addr = frappe.new_doc("Address")
    addr.update(
        {
            "address_title": f"{supplier_name} ({address_type})",
            "address_type": address_type,
            "address_line1": data["address_line1"],
            "city": data["city"],
            "state": data["state"],
            "country": data["country"],
            "pincode": data["pincode"],
            "is_primary_address": 1 if address_type == "Billing" else 0,
            "is_shipping_address": (
                1 if address_type == "Shipping" else 0
            ),
        }
    )
    addr.append(
        "links",
        {"link_doctype": "Supplier", "link_name": supplier_name},
    )
    addr.flags.ignore_permissions = True
    addr.insert()
    return True


def _describe(supplier_name: str) -> dict[str, Any]:
    supplier = frappe.db.get_value(
        "Supplier",
        supplier_name,
        [
            "represents_company", "email_id", "mobile_no",
            "default_currency", "gst_category", "gstin",
        ],
        as_dict=True,
    ) or {}
    atw = [
        row.company for row in frappe.get_all(
            "Allowed To Transact With",
            filters={"parent": supplier_name},
            fields=["company"],
        )
    ]
    addresses = frappe.db.sql(
        """
        SELECT a.address_type, a.state, a.pincode, a.country
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        """,
        (supplier_name,),
        as_dict=True,
    )
    return {
        "represents_company": supplier.get("represents_company"),
        "allowed_to_transact_with": atw,
        "email_id": supplier.get("email_id"),
        "mobile_no": supplier.get("mobile_no"),
        "default_currency": supplier.get("default_currency"),
        "gst_category": supplier.get("gst_category"),
        "gstin": supplier.get("gstin"),
        "addresses": [dict(a) for a in addresses],
    }


def _default_supplier_group() -> str:
    setting = frappe.db.get_single_value(
        "Buying Settings", "supplier_group"
    )
    if setting:
        return setting
    fallback = frappe.db.get_value(
        "Supplier Group", {"is_group": 0}, "name"
    )
    return fallback or "All Supplier Groups"


def _slugify(s: str) -> str:
    out: list[str] = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "supplier"
