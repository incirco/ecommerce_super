"""§11.5.1 Mode 1 Custom GSP — Basic auth secret on EasyEcom Account.

EE-side FDE pastes this secret on their EE Account config so EE can
authenticate against our /gettoken endpoint. Per-EE-Account scoping
matches the §8a Location / §11 push tenant model — multi-tenant
benches get unique secrets per account.

The secret is the password portion of the Basic auth header EE sends.
Our /gettoken endpoint matches incoming Basic auth against any
enabled EE Account's secret; if matched, mints a Bearer for that
account.

Encrypted at rest via Frappe's Password fieldtype.
"""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "EasyEcom Account": [
                {
                    "fieldname": "gsp_section_break",
                    "label": "Custom GSP (§11.5.1 Mode 1)",
                    "fieldtype": "Section Break",
                    "insert_after": "ecs_b2b_module",
                    "collapsible": 1,
                    "description": (
                        "Settings for the Custom GSP invoice flow — when "
                        "EE calls ERPNext to mint IRN via India Compliance. "
                        "Leave gsp_basic_auth_secret blank to disable Mode 1 "
                        "(Mode 2 polling-mirror works independently)."
                    ),
                },
                {
                    "fieldname": "gsp_basic_auth_secret",
                    "label": "Custom GSP Basic Auth Secret",
                    "fieldtype": "Password",
                    "insert_after": "gsp_section_break",
                    "description": (
                        "Shared secret EE uses in the HTTP Basic auth header "
                        "when calling our /gettoken endpoint. Configure the "
                        "SAME value on EE's Custom GSP setup. Per-Account "
                        "scoping — each EE Account gets a unique secret. "
                        "Leave blank to disable Custom GSP for this Account."
                    ),
                },
                {
                    "fieldname": "gsp_mint_einvoice",
                    "label": "Mint E-Invoice (IRN) via India Compliance",
                    "fieldtype": "Check",
                    "default": "1",
                    "insert_after": "gsp_basic_auth_secret",
                    "description": (
                        "When ON (default), /einvoice/update mints IRN on "
                        "NIC IRP via India Compliance and returns the IRN/QR "
                        "in the response. When OFF, the SI is still created "
                        "and submitted (GL impact happens), but NO NIC IRP "
                        "call is made — response carries empty irn/ack "
                        "fields and only the PDF URL. Turn OFF for clients "
                        "below the e-invoicing turnover threshold OR who "
                        "handle IRN externally."
                    ),
                },
                {
                    "fieldname": "gsp_mint_ewaybill",
                    "label": "Mint E-Way Bill via India Compliance",
                    "fieldtype": "Check",
                    "default": "1",
                    "insert_after": "gsp_mint_einvoice",
                    "description": (
                        "When ON (default), /ewaybill/update mints e-way "
                        "bill on NIC EWB via India Compliance. When OFF, no "
                        "NIC EWB call — response carries empty "
                        "eway_bill_number / eway_bill_date / eway_bill_pdf. "
                        "Turn OFF for clients who handle e-way bills "
                        "physically (forwarder paperwork) or via another "
                        "system. Note: NIC EWB usually requires an IRN, so "
                        "this is typically ON when E-Invoice is ON."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
