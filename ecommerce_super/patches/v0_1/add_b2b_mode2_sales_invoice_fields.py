"""§11.5.2 Mode 2 — Custom Fields on Sales Invoice for EE back-references.

When EE generates the GST invoice on its own side (Mode 2 — EE has
its own GSP integration or marketplace handles invoicing), we mirror
the invoice into ERPNext as a Draft Sales Invoice. These Custom
Fields let the SI carry back-refs to:

  - The EE invoice_id (EE's internal identifier — always populated)
  - The EE invoice_number (EE's GST invoice series — populated when
    EE generates)
  - The EE invoice PDF URL (downloadable from EE side)
  - The EasyEcom B2B Order Map (so FDE can navigate Map ↔ SI)

Mode 1 (Custom GSP) uses the SAME fields — when we ARE the GSP and
mint the IRN via India Compliance, India Compliance writes its own
fields (irn, ack_no, ack_dt) onto the SI; our ecs_easyecom_* fields
are the bridge identifying which EE order this SI corresponds to.

Idempotent — re-running create_custom_fields is safe on existing
rows.
"""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Sales Invoice": [
                {
                    "fieldname": "ecs_easyecom_section_break",
                    "label": "EasyEcom Integration",
                    "fieldtype": "Section Break",
                    "insert_after": "more_information",
                    "collapsible": 1,
                },
                {
                    "fieldname": "ecs_easyecom_invoice_id",
                    "label": "EE Invoice ID",
                    "fieldtype": "Data",
                    "insert_after": "ecs_easyecom_section_break",
                    "read_only": 1,
                    "search_index": 1,
                    "description": (
                        "EasyEcom's internal invoice identifier (always populated "
                        "for any EE order, regardless of whether the GST invoice "
                        "has been generated). Distinct from ecs_easyecom_invoice_number "
                        "which is EE's GST invoice series number."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_invoice_number",
                    "label": "EE Invoice Number",
                    "fieldtype": "Data",
                    "insert_after": "ecs_easyecom_invoice_id",
                    "read_only": 1,
                    "description": (
                        "EE-side GST invoice series number (e.g. 'BMH1-2526-8'). "
                        "Populated when EE generates the GST invoice via its own "
                        "GSP or marketplace integration (§11.5.2 Mode 2). "
                        "Empty in Mode 1 — we mint the SI fresh + India "
                        "Compliance writes IRN/ack_no/ack_dt on the SI directly."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_invoice_pdf_url",
                    "label": "EE Invoice PDF URL",
                    "fieldtype": "Data",
                    "insert_after": "ecs_easyecom_invoice_number",
                    "read_only": 1,
                    "description": (
                        "URL to the EE-hosted invoice PDF (from "
                        "documents.easyecom_invoice in the polling response). "
                        "Populated in Mode 2; null in Mode 1 (we generate our "
                        "own PDF via Frappe Print Format)."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_b2b_order_map",
                    "label": "EE B2B Order Map",
                    "fieldtype": "Link",
                    "options": "EasyEcom B2B Order Map",
                    "insert_after": "ecs_easyecom_invoice_pdf_url",
                    "read_only": 1,
                    "description": (
                        "Back-reference to the EasyEcom B2B Order Map row "
                        "this SI mirrors. Lets the FDE navigate Map ↔ SI "
                        "without searching by reference_code."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
