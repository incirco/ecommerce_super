"""Seed missing fixtures for `ci-test.local`.

The ERPNext install on `ci-test.local` left only one UOM (Nos), no
default Company, and no GST Item Tax Templates. This script seeds
what the existing test suite assumes is present:

  - Standard UOMs (Kg, Box, Litre, Meter, etc.)
  - `_Test Company` with sensible defaults
  - HSN code 85171000 (referenced inline by many tests)
  - `GST 18% - TC` and `GST 5% - TC` Item Tax Templates (in case
    setup_complete didn't create them)

Idempotent. Designed to be re-run safely as ci-test.local is re-
provisioned. Throwaway — lives under `_ci_seed.py` so the python
import machinery can find it via `bench execute`.
"""

from __future__ import annotations

import frappe


_STANDARD_UOMS = [
    ("Box", "no"),
    ("Carton", "no"),
    ("Centimeter", "yes"),
    ("Cubic Meter", "yes"),
    ("Dozen", "no"),
    ("Foot", "yes"),
    ("Gram", "yes"),
    ("Hour", "yes"),
    ("Inch", "yes"),
    ("Kg", "yes"),
    ("Litre", "yes"),
    ("Meter", "yes"),
    ("Minute", "yes"),
    ("Millilitre", "yes"),
    ("Millimeter", "yes"),
    ("Pair", "no"),
    ("Set", "no"),
    ("Square Foot", "yes"),
    ("Square Meter", "yes"),
    ("Tonne", "yes"),
    ("Unit", "no"),
    ("Yard", "yes"),
]


def seed() -> dict:
    return {
        "uoms_created": _step("uoms_created", _seed_uoms),
        "company_created": _step("company_created", _seed_test_company),
        "hsn_created": _step("hsn_created", _seed_hsn_codes),
        "tax_templates_created": _step(
            "tax_templates_created", _seed_gst_templates
        ),
        "internal_customers_seeded": _step(
            "internal_customers_seeded", _seed_internal_customers
        ),
        "fiscal_years_seeded": _step(
            "fiscal_years_seeded", _seed_fiscal_years
        ),
    }


def _step(name: str, fn):
    """Run a seed step in its own transaction; rollback on error and
    record the failure rather than crashing the whole sweep."""
    try:
        result = fn()
        frappe.db.commit()
        return result
    except Exception as exc:  # noqa: BLE001
        frappe.db.rollback()
        return f"ERROR {type(exc).__name__}: {exc}"


def _seed_uoms() -> int:
    created = 0
    for uom_name, must_be_whole in _STANDARD_UOMS:
        if frappe.db.exists("UOM", uom_name):
            continue
        doc = frappe.new_doc("UOM")
        doc.uom_name = uom_name
        doc.must_be_whole_number = 0 if must_be_whole == "yes" else 1
        doc.enabled = 1
        doc.flags.ignore_permissions = True
        doc.insert()
        frappe.db.commit()
        created += 1
    return created


def _seed_test_company() -> bool:
    if frappe.db.exists("Company", "_Test Company"):
        return False
    doc = frappe.new_doc("Company")
    doc.company_name = "_Test Company"
    doc.abbr = "_TC"
    doc.default_currency = "INR"
    doc.country = "India"
    doc.create_chart_of_accounts_based_on = "Standard Template"
    doc.chart_of_accounts = "Standard"
    doc.flags.ignore_permissions = True
    doc.insert()
    return True


def _seed_hsn_codes() -> int:
    codes = ["85171000", "39239090", "61091000"]
    created = 0
    for code in codes:
        if frappe.db.exists("GST HSN Code", code):
            continue
        doc = frappe.new_doc("GST HSN Code")
        doc.hsn_code = code
        doc.description = f"Test HSN {code}"
        doc.flags.ignore_permissions = True
        doc.insert()
        created += 1
    return created


def _seed_internal_customers() -> int:
    """Seed Internal Customers + billing Addresses for the canonical
    test Companies. Mirrors what a deployed client site looks like —
    the FDE creates Internal Customer pairs during §10 setup, and each
    pair ships with a primary Address Dynamic-Linked to the Customer.

    Without this seed, every §10 outbound / inbound / stage4 / substrate
    integration test that submits an Internal-Customer DN fails inside
    ERPNext's `validate_party_address`: the test factory creates a DN
    whose only available address is the warehouse's shipping Address
    (linked to `Warehouse` only), and ERPNext throws "Billing Address
    does not belong to the {customer}" because the Customer has zero
    Dynamic-Linked Addresses to fall back on.

    Each seeded Customer also gets `customer_primary_address` pointed
    at its own Address, so ERPNext's `set_missing_values` cascade picks
    that up for both `customer_address` (billing) and
    `shipping_address_name` on the DN.
    """
    test_companies = [
        c for c in ["_Test Company", "_Other Test Co"]
        if frappe.db.exists("Company", c)
    ]
    if not test_companies:
        return 0
    group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    if not group:
        return 0
    created = 0
    for company in test_companies:
        cust_name = f"INTL-CUST-for-{company}"
        existing = frappe.db.get_value(
            "Customer",
            {"is_internal_customer": 1, "represents_company": company},
            "name",
        )
        if existing:
            cust_name = existing
        else:
            cust = frappe.new_doc("Customer")
            cust.update(
                {
                    "customer_name": cust_name,
                    "customer_type": "Company",
                    "customer_group": group,
                    "is_internal_customer": 1,
                    "represents_company": company,
                    "companies": [{"company": company}],
                }
            )
            cust.flags.ignore_permissions = True
            cust.insert()
            cust_name = cust.name
            created += 1
        addr_title = f"Addr-{cust_name}"
        addr_name = frappe.db.get_value(
            "Address", {"address_title": addr_title}, "name"
        )
        if not addr_name:
            safe = company.replace(" ", "").replace("_", "").lower()
            addr = frappe.new_doc("Address")
            addr.update(
                {
                    "address_title": addr_title,
                    "address_type": "Billing",
                    "address_line1": "Internal Customer Test Premises",
                    "city": "Bengaluru",
                    "state": "Karnataka",
                    "pincode": "560001",
                    "country": "India",
                    "phone": "7777777777",
                    "email_id": f"intl-{safe}@test.local",
                    "links": [
                        {"link_doctype": "Customer", "link_name": cust_name}
                    ],
                }
            )
            addr.flags.ignore_permissions = True
            addr.insert()
            addr_name = addr.name
            created += 1
        current_primary = frappe.db.get_value(
            "Customer", cust_name, "customer_primary_address"
        )
        if (current_primary or "") != addr_name:
            frappe.db.set_value(
                "Customer", cust_name, "customer_primary_address",
                addr_name, update_modified=False,
            )
    return created


def _seed_fiscal_years() -> int:
    """Seed Indian Fiscal Year rows for the current + adjacent years.

    Without an active Fiscal Year that covers today's date, every
    PR / GRN / SI test that goes through ERPNext's
    `validate_fiscal_year` blows up with `FiscalYearError: Date
    {today} is not in any active Fiscal Year`. The §9 GRN pull
    stage3 test class cascades ~12 errors from this single fixture
    gap — the setUpClass creates a Purchase Order with `posting_date
    = today`, which is what triggers the throw.

    ci-test.local was provisioned without `setup_complete` running
    end-to-end (the seed comment at the top of this file mentions
    UOMs, Company, and HSN codes missing for the same reason).
    """
    from datetime import date
    today = date.today()
    # Indian FY: April 1 → March 31. Determine current FY's start year.
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    target_years = [fy_start_year - 1, fy_start_year, fy_start_year + 1]
    created = 0
    for start_year in target_years:
        fy_name = f"{start_year}-{start_year + 1}"
        if frappe.db.exists("Fiscal Year", fy_name):
            continue
        doc = frappe.new_doc("Fiscal Year")
        doc.year = fy_name
        doc.year_start_date = date(start_year, 4, 1)
        doc.year_end_date = date(start_year + 1, 3, 31)
        doc.flags.ignore_permissions = True
        try:
            doc.insert()
            created += 1
        except Exception:
            # Some Frappe versions enforce non-overlap; tolerate.
            pass
    return created


def _seed_gst_templates() -> int:
    company_abbr = "_TC"
    company = "_Test Company"
    if not frappe.db.exists("Company", company):
        return 0
    created = 0
    for rate in (5.0, 18.0):
        name = f"GST {int(rate)}% - {company_abbr}"
        if frappe.db.exists("Item Tax Template", name):
            continue
        # Resolve the canonical CGST / SGST / IGST accounts under the
        # company's chart of accounts. Skip if they're missing — the
        # test suite either tolerates absence or this template is
        # never referenced by those tests on this site.
        cgst = frappe.db.get_value("Account",
            {"company": company, "account_name": "Output Tax CGST"}, "name")
        sgst = frappe.db.get_value("Account",
            {"company": company, "account_name": "Output Tax SGST"}, "name")
        igst = frappe.db.get_value("Account",
            {"company": company, "account_name": "Output Tax IGST"}, "name")
        if not (cgst and sgst and igst):
            # Fall back to any GST output accounts the seed CoA
            # happened to plant.
            cgst = cgst or frappe.db.get_value("Account",
                {"company": company, "account_name": ("like", "%CGST%")}, "name")
            sgst = sgst or frappe.db.get_value("Account",
                {"company": company, "account_name": ("like", "%SGST%")}, "name")
            igst = igst or frappe.db.get_value("Account",
                {"company": company, "account_name": ("like", "%IGST%")}, "name")
        if not (cgst and sgst and igst):
            continue
        doc = frappe.new_doc("Item Tax Template")
        doc.title = f"GST {int(rate)}%"
        doc.company = company
        doc.taxes = []
        doc.append("taxes", {"tax_type": cgst, "tax_rate": rate / 2})
        doc.append("taxes", {"tax_type": sgst, "tax_rate": rate / 2})
        doc.append("taxes", {"tax_type": igst, "tax_rate": rate})
        doc.flags.ignore_permissions = True
        doc.insert()
        created += 1
    return created
