"""§12 B2C — Custom Fields on Sales Invoice for marketplace-pulled SIs.

When the §12 polling cron pulls a manifested order from EE and creates
an SI, these fields carry the marketplace context (channel, payment
mode, courier) plus the EE-supplied financial values that the recon
engine uses to detect variance against the ERPNext-computed SI totals.

Per Path 2 (locked 2026-06-29): SI's `taxes` table uses EE-supplied
tax (the system that generated the invoice = source of truth). The
ERPNext-computed tax via Item Tax Template is stored separately in
`ecs_erpnext_tax_check_total` purely as a variance signal. >1% delta
raises an Integration Discrepancy as an upstream-issue alert; SI data
is never amended.

Fields land inside the existing "EasyEcom Integration" collapsible
section on the SI form (created by add_b2b_mode2_sales_invoice_fields).
Insert after the §11.6 dispatch tracking_url field so the section reads:

  EE Invoice ID
  EE Invoice Number
  EE Invoice PDF URL
  EE B2B Order Map           ← §11 (B2B)
  EE Dispatch Status         ← §11.6
  EE Dispatched At
  EE Delivered At
  EE Tracking URL
  -- §12 (B2C) marketplace context --
  Marketplace
  Marketplace Order ID
  EE Order ID
  Payment Mode
  AWB Number
  Courier
  EE Invoice Total           ← recon source-of-truth (what EE said)
  EE Invoice Tax Total
  ERPNext Tax Check Total    ← variance signal vs Item Tax Template
"""
from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Sales Invoice": [
                {
                    "fieldname": "ecs_marketplace",
                    "label": "Marketplace",
                    "fieldtype": "Link",
                    "options": "Marketplace",
                    "insert_after": "ecs_easyecom_tracking_url",
                    "read_only": 1,
                    "search_index": 1,
                    "in_standard_filter": 1,
                    "description": (
                        "Marketplace channel for B2C SIs created via §12 "
                        "polling. Null for B2B SIs (§11) and for SIs not "
                        "minted via the integration."
                    ),
                },
                {
                    "fieldname": "ecs_marketplace_order_id",
                    "label": "Marketplace Order ID",
                    "fieldtype": "Data",
                    "insert_after": "ecs_marketplace",
                    "read_only": 1,
                    "search_index": 1,
                    "in_standard_filter": 1,
                    "description": (
                        "Marketplace-level order identifier (from EE "
                        "reference_code) — the recon engine's primary "
                        "join key for Settlement Lines. Distinct from "
                        "EE Invoice ID (shipment-level, internal to EE)."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_order_id",
                    "label": "EE Order ID",
                    "fieldtype": "Data",
                    "insert_after": "ecs_marketplace_order_id",
                    "read_only": 1,
                    "search_index": 1,
                    "description": (
                        "EE internal Order_id (shared across a split "
                        "order's shipments). Stable join key within EE "
                        "for diagnostic correlation across multiple SIs "
                        "from the same source order."
                    ),
                },
                {
                    "fieldname": "ecs_payment_mode",
                    "label": "Payment Mode",
                    "fieldtype": "Data",
                    "insert_after": "ecs_easyecom_order_id",
                    "read_only": 1,
                    "description": (
                        "EE-reported payment mode (Prepaid / COD / etc.). "
                        "Used by ops for COD-specific reconciliation "
                        "(COD remits arrive via a separate settlement "
                        "stream)."
                    ),
                },
                {
                    "fieldname": "ecs_awb_number",
                    "label": "AWB Number",
                    "fieldtype": "Data",
                    "insert_after": "ecs_payment_mode",
                    "read_only": 1,
                    "description": (
                        "Air Waybill / shipment tracking number from EE. "
                        "For B2C this is the primary tracking handle "
                        "(shipping_track_link in §11.6 may also be set)."
                    ),
                },
                {
                    "fieldname": "ecs_courier",
                    "label": "Courier",
                    "fieldtype": "Data",
                    "insert_after": "ecs_awb_number",
                    "read_only": 1,
                    "description": (
                        "Courier / carrier name from EE — Bluedart, "
                        "Delhivery, etc."
                    ),
                },
                {
                    "fieldname": "ecs_ee_invoice_total",
                    "label": "EE Invoice Total",
                    "fieldtype": "Currency",
                    "insert_after": "ecs_courier",
                    "read_only": 1,
                    "description": (
                        "EE-reported invoice total (grand_total) for this "
                        "shipment. Source-of-truth for recon — Settlement "
                        "Lines will reconcile against this value, not "
                        "against ERPNext's grand_total. Variance > 1 paisa "
                        "vs SI.grand_total raises an Integration "
                        "Discrepancy (Path 2 locked 2026-06-29)."
                    ),
                },
                {
                    "fieldname": "ecs_ee_invoice_tax_total",
                    "label": "EE Invoice Tax Total",
                    "fieldtype": "Currency",
                    "insert_after": "ecs_ee_invoice_total",
                    "read_only": 1,
                    "description": (
                        "EE-reported total tax for this shipment. "
                        "Captured for variance detection — see "
                        "ecs_erpnext_tax_check_total for the ERPNext-"
                        "computed counterpart."
                    ),
                },
                {
                    "fieldname": "ecs_erpnext_tax_check_total",
                    "label": "ERPNext Tax Check Total",
                    "fieldtype": "Currency",
                    "insert_after": "ecs_ee_invoice_tax_total",
                    "read_only": 1,
                    "description": (
                        "ERPNext-computed tax total (via Item Tax "
                        "Templates) for the same line items. NOT used "
                        "in the GL — the SI's taxes table carries EE's "
                        "tax (Path 2). This field exists purely as a "
                        "variance check: > 1% delta vs "
                        "ecs_ee_invoice_tax_total raises an Integration "
                        "Discrepancy as an alert. The Discrepancy is "
                        "informational — SI data is immutable; FDE "
                        "investigates the upstream cause (HSN code, tax "
                        "category, place-of-supply config, marketplace-"
                        "side adapter)."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
