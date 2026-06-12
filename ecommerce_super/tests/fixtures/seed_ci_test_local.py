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
