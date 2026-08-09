"""Custom Fields on Sales Invoice for EasyEcom buyer + shipping
address data.

CONTEXT

The B2C polling flow (§12) creates SIs against per-marketplace pool
Customers (e.g., "B2C Customer - Shopify (26) - Inter State") because
buyer privacy rules on most marketplaces forbid holding real buyer PII
against a real Customer record. That leaves the SI itself with no
structured place to record the actual buyer / shipping address —
`customer_address` is optional and would require creating an Address
record per SI (5,000+ Addresses/month bloats the master).

The client operations team also needs to filter / report SIs by buyer
city, state, pincode (e.g., "how much Shopify revenue landed in
Karnataka?"), which is not answerable from `address_display` (HTML text
blob) alone.

DECISION

Store the EE-supplied buyer + shipping address as **structured Data
fields on the SI itself** under a new "EasyEcom Buyer Address"
collapsible section. Two columns:

  BILLING                   SHIPPING
  ----------------------    ----------------------
  Name                      Address 1
  Address 1                 Address 2
  Address 2                 City
  City                      State
  State                     Pincode
  Pincode                   Country
  Country

The high-signal fields (city, state, pincode) get `in_standard_filter=1`
so list-view filtering is one click. All fields are `read_only=1` +
`no_copy=1` — the source of truth is EE, and copying an SI shouldn't
duplicate the buyer's address into a new invoice.

Contact (email, mobile) reuses ERPNext standard fields (contact_email,
contact_mobile) rather than duplicating — those are already writable
on SI without needing a linked Contact record.

Idempotent — `create_custom_fields` skips fields that already exist.
"""
from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ecommerce_super.easyecom._schema_utils import ensure_dynamic_row_format


def execute() -> None:
    ensure_dynamic_row_format("tabSales Invoice")
    create_custom_fields(
        {
            "Sales Invoice": [
                {
                    "fieldname": "ecs_ee_buyer_address_section",
                    "label": "EasyEcom Buyer Address",
                    "fieldtype": "Section Break",
                    "insert_after": "ecs_settlement_completed_at",
                    "collapsible": 1,
                    "description": (
                        "Structured billing + shipping address as supplied by "
                        "EasyEcom for the actual buyer. Populated by the §12 "
                        "B2C polling flow (and by any backfill runs). Filterable "
                        "by city / state / pincode from the list view."
                    ),
                },
                # ---------- Column 1: Billing ----------
                {
                    "fieldname": "ecs_ee_billing_name",
                    "label": "Billing Name",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_buyer_address_section",
                    "read_only": 1,
                    "no_copy": 1,
                    "description": (
                        "Buyer's name as supplied by EE (billing_name). May be "
                        "empty on marketplaces that suppress buyer PII "
                        "(e.g., Amazon Easyship)."
                    ),
                },
                {
                    "fieldname": "ecs_ee_billing_address_1",
                    "label": "Billing Address 1",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_billing_name",
                    "read_only": 1,
                    "no_copy": 1,
                },
                {
                    "fieldname": "ecs_ee_billing_address_2",
                    "label": "Billing Address 2",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_billing_address_1",
                    "read_only": 1,
                    "no_copy": 1,
                },
                {
                    "fieldname": "ecs_ee_billing_city",
                    "label": "Billing City",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_billing_address_2",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                    "description": "Filterable in list view.",
                },
                {
                    "fieldname": "ecs_ee_billing_state",
                    "label": "Billing State",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_billing_city",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                    "description": (
                        "Buyer's state — the raw EE-supplied string (used by "
                        "the recon engine when Place of Supply is unset)."
                    ),
                },
                {
                    "fieldname": "ecs_ee_billing_pincode",
                    "label": "Billing Pincode",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_billing_state",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                },
                {
                    "fieldname": "ecs_ee_billing_country",
                    "label": "Billing Country",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_billing_pincode",
                    "read_only": 1,
                    "no_copy": 1,
                    "default": "India",
                },
                # ---------- Column 2: Shipping ----------
                {
                    "fieldname": "ecs_ee_shipping_col_break",
                    "fieldtype": "Column Break",
                    "insert_after": "ecs_ee_billing_country",
                },
                {
                    "fieldname": "ecs_ee_shipping_address_1",
                    "label": "Shipping Address 1",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_shipping_col_break",
                    "read_only": 1,
                    "no_copy": 1,
                    "description": (
                        "Shipping address as supplied by EE (address_line_1). "
                        "Usually matches billing for online orders; may differ "
                        "for gift orders or corporate B2B ship-to."
                    ),
                },
                {
                    "fieldname": "ecs_ee_shipping_address_2",
                    "label": "Shipping Address 2",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_shipping_address_1",
                    "read_only": 1,
                    "no_copy": 1,
                },
                {
                    "fieldname": "ecs_ee_shipping_city",
                    "label": "Shipping City",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_shipping_address_2",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                },
                {
                    "fieldname": "ecs_ee_shipping_state",
                    "label": "Shipping State",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_shipping_city",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                },
                {
                    "fieldname": "ecs_ee_shipping_pincode",
                    "label": "Shipping Pincode",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_shipping_state",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                    "search_index": 1,
                },
                {
                    "fieldname": "ecs_ee_shipping_country",
                    "label": "Shipping Country",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_shipping_pincode",
                    "read_only": 1,
                    "no_copy": 1,
                    "default": "India",
                },
            ],
        }
    )
