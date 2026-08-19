"""Add `ecs_ee_customer_code` Int custom field on Customer.

CONTEXT

  B2B backfill (PR #292) auto-creates Customer records for invoices
  whose buyer_gst is missing (URP), invalid (foreign / Nepal-style),
  or non-resolvable via GSTIN. To keep re-runs idempotent (no
  duplicate Customers on re-import), we need a stable per-EE-customer
  identifier to look up by.

  EE's getAllOrders payload includes `customer_code` — the internal
  EE-side customer identifier that stays constant across invoices
  from the same buyer. Perfect for our idempotency key.

FIELD

  ecs_ee_customer_code: Int, read-only on the form. Populated by
  the backfill when it auto-creates a Customer. Left empty on
  Customers created by other flows (customer_pull uses gstin as
  its key; live-created customers have no EE-side counterpart).

  Not marked unique — a hand-created Customer may end up sharing
  a code with an auto-created one during ops cleanup; the backfill
  looks up by exact-match and picks the first enabled one.

IDEMPOTENT per the create_custom_fields contract.
"""
from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Customer": [
                {
                    "fieldname": "ecs_ee_customer_code",
                    "label": "EE customer_code (source)",
                    "fieldtype": "Int",
                    "insert_after": "gst_category",
                    "read_only": 1,
                    "description": (
                        "EasyEcom internal customer_code that this "
                        "Customer was auto-created for by the B2B backfill "
                        "flow (PR #292 follow-up). Ensures re-runs match "
                        "existing Customers instead of duplicating. Empty "
                        "on Customers created by other flows."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
