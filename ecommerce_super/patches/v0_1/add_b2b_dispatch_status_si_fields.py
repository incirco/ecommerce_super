"""§11.6 — Dispatch status Custom Fields on Sales Invoice.

§11 polling already fetches per-order status_id from EE on its */5
sweep, but Phase 1 only acted on status_id=9 (Cancelled) and ignored
the dispatch transitions (5=Shipped, 6=Delivered, 7=Returned). These
fields surface that information on the linked Sales Invoice so ERPNext
operations can see fulfilment state without flipping to EE's UI.

The polling tick stamps these fields when EE reports a dispatch
transition; nothing else writes to them.

All four fields live inside the existing "EasyEcom Integration"
collapsible section on the SI form (created by
add_b2b_mode2_sales_invoice_fields.py), inserted after the
EE B2B Order Map link so the section's narrative reads:

  EE Invoice ID
  EE Invoice Number
  EE Invoice PDF URL
  EE B2B Order Map
  -- dispatch context --
  Dispatch Status
  Dispatched At
  Delivered At
  Tracking URL

Idempotent — re-running create_custom_fields is safe.
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
                    "fieldname": "ecs_easyecom_dispatch_status",
                    "label": "EE Dispatch Status",
                    "fieldtype": "Select",
                    "options": "\nPending\nShipped\nDelivered\nReturned\nCancelled",
                    "insert_after": "ecs_easyecom_b2b_order_map",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "description": (
                        "EE-side fulfilment status, derived from the */5 "
                        "polling tick reading order_status_id on the linked "
                        "B2B Order Map. Pending = order in EE but not yet "
                        "shipped (status_id 1-4, 30). Shipped = handed to "
                        "courier (status_id 5). Delivered = POD received "
                        "(status_id 6). Returned = returned to origin "
                        "(status_id 7). Cancelled = order cancelled on EE "
                        "(status_id 9; the cancel branch also unlinks "
                        "/ cancels the SI per §11 cancellation flow). "
                        "Blank = polling has not yet observed a status — "
                        "either the order is brand new OR the SI was "
                        "minted before §11.6 shipped."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_dispatched_at",
                    "label": "EE Dispatched At",
                    "fieldtype": "Datetime",
                    "insert_after": "ecs_easyecom_dispatch_status",
                    "read_only": 1,
                    "description": (
                        "Timestamp when EE first reported status_id=5 "
                        "(Shipped). Set once on the Pending → Shipped "
                        "transition; never overwritten on subsequent "
                        "polls (delivery / return updates only stamp "
                        "their own fields)."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_delivered_at",
                    "label": "EE Delivered At",
                    "fieldtype": "Datetime",
                    "insert_after": "ecs_easyecom_dispatched_at",
                    "read_only": 1,
                    "description": (
                        "Timestamp when EE first reported status_id=6 "
                        "(Delivered). Set once on Shipped → Delivered; "
                        "subsequent polls do not overwrite. Null for "
                        "orders that haven't been delivered yet OR were "
                        "returned (status_id=7) without delivery."
                    ),
                },
                {
                    "fieldname": "ecs_easyecom_tracking_url",
                    "label": "EE Tracking URL",
                    # Long Text (off-page) instead of Data (in-row varchar)
                    # — see add_b2b_mode2_sales_invoice_fields for the
                    # mmpl16 row-overflow rationale. URLs aren't queried
                    # by indexed lookup so off-page storage is fine.
                    "fieldtype": "Long Text",
                    "insert_after": "ecs_easyecom_delivered_at",
                    "read_only": 1,
                    "description": (
                        "Carrier tracking URL from EE's "
                        "tracking_link / tracking_url field on the order. "
                        "Populated whenever EE provides it in the polling "
                        "response (usually arrives with the Shipped "
                        "transition). Empty when EE has no carrier data."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
