"""gh#166 hardening — add IP allowlist + rate-limit fields to
EasyEcom Account so per-account tuning is possible from the desk.

Fields on EasyEcom Account:
  - gsp_ip_allowlist (Small Text): comma-separated IPv4 / IPv4-CIDR.
    Empty = no restriction (backwards-compatible). Populated = only
    accept Bearer usage from a listed IP. Applied inside validate_bearer.
  - gsp_rate_limit_per_min (Int): max calls per (endpoint, invoice_id,
    minute). Empty / 0 = default of 6 (one per 10 seconds). Enforced by
    the /einvoice/update + /ewaybill/update dispatch.

Idempotent per create_custom_fields.
"""
from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "EasyEcom Account": [
                {
                    "fieldname": "gsp_security_section",
                    "label": "Custom GSP — Security",
                    "fieldtype": "Section Break",
                    "insert_after": "gsp_basic_auth_secret",
                    "collapsible": 1,
                    "collapsible_depends_on": "eval:!doc.gsp_ip_allowlist && !doc.gsp_rate_limit_per_min",
                },
                {
                    "fieldname": "gsp_ip_allowlist",
                    "label": "GSP IP Allowlist",
                    "fieldtype": "Small Text",
                    "insert_after": "gsp_security_section",
                    "description": (
                        "Comma-separated IPv4 / IPv4-CIDR. When set, "
                        "Bearer tokens for this account are ONLY accepted "
                        "from a listed IP. Empty = no IP restriction. "
                        "Populate with EasyEcom's outbound IP range for "
                        "production hardening (gh#166)."
                    ),
                },
                {
                    "fieldname": "gsp_rate_limit_per_min",
                    "label": "GSP Rate Limit (calls/min per invoice_id)",
                    "fieldtype": "Int",
                    "insert_after": "gsp_ip_allowlist",
                    "default": "6",
                    "description": (
                        "Max /einvoice/update + /ewaybill/update calls "
                        "per (endpoint, invoice_id) per rolling 60s. "
                        "Default 6 = one call per 10s. Set to 0 to "
                        "disable rate limiting (not recommended)."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
