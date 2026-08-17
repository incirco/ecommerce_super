"""Raise `rate` field precision on Sales Order Item + Sales Invoice
Item from the Currency default (2 decimals for INR) to 4.

CONTEXT

  B2B backfill (PR #292) computes per-unit rate by dividing EE's
  tax-inclusive selling_price by (1 + tax_rate/100) to get a pre-tax
  rate. When ERPNext later adds GST back via item_tax_template, the
  SI grand_total should equal EE's total_amount exactly.

  At 2-decimal precision, however, rate values like 200/1.18 = 0.8475
  round to 0.85 — introducing ~0.3% rounding drift per invoice, above
  mirror's 0.01% variance threshold.

  4 decimals is sufficient (empirically): 0.8475 × 200 × 1.18 =
  200.01 vs EE's 200.00 → 0.005% drift, well under threshold.

WHY THIS IS SAFE

  - Amount is still stored at Currency precision (2 for INR) — no
    print-format changes visible on invoices.
  - Rate at 4 decimals is standard ERPNext practice for many clients
    (default in some Currency Format setups). Frappe supports it via
    Property Setter without any schema migration.
  - Affects only in-memory calculation precision. Downstream GL,
    GSTR-1, e-invoice all read amount (not rate) — untouched.

IDEMPOTENT — updates existing Property Setter if present, otherwise
creates. Clears cache so meta refresh picks up the new precision.
"""
from __future__ import annotations

import frappe


TARGETS = ("Sales Order Item", "Sales Invoice Item")
NEW_PRECISION = "4"


def execute() -> None:
    for doctype in TARGETS:
        existing = frappe.db.get_value(
            "Property Setter",
            {
                "doc_type": doctype,
                "field_name": "rate",
                "property": "precision",
            },
            "name",
        )
        if existing:
            frappe.db.set_value(
                "Property Setter", existing, "value", NEW_PRECISION
            )
            action = "updated"
        else:
            frappe.get_doc({
                "doctype": "Property Setter",
                "doctype_or_field": "DocField",
                "doc_type": doctype,
                "field_name": "rate",
                "property": "precision",
                "property_type": "Select",
                "value": NEW_PRECISION,
            }).insert(ignore_permissions=True)
            action = "created"

        frappe.clear_cache(doctype=doctype)
        print(
            f"[raise_rate_precision_for_backfill_accuracy] {action} "
            f"Property Setter: {doctype}.rate.precision = {NEW_PRECISION}"
        )
