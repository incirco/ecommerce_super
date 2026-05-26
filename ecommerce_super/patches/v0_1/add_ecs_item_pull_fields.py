"""Custom Fields on Item for the §8d Stage 2 pull.

The EasyEcom-Item-Pull ruleset writes into these fields. They're all
EasyEcom-origin diagnostic data — not used by ERPNext's transactional
logic — so they're read-only on the form (FDE-edit-only, not Operator).

- ecs_size, ecs_colour: short text, EE-supplied product attributes that
  don't map to native Item fields (Item's `variant_of` / Item Variants
  is its own machinery; we don't engage it). Useful for FDE-side review
  and listing parity.
- ecs_height_cm / ecs_length_cm / ecs_width_cm: dimensions in EE units
  (cm). ERPNext Item has no native physical-dimensions fields beyond
  weight_per_unit; capturing them on Item lets the §8d push reuse them.
- ecs_ee_product_id / ecs_ee_cp_id: EE internal IDs persisted on the
  Item itself (in addition to the EasyEcom Item Map row that owns the
  relationship). They're keys EE's push endpoints accept — the map row
  is the canonical store, but stamping them on the Item gives quick
  visibility without joining through the map.
- ecs_ee_cost / ecs_ee_mrp: EE's cost/mrp as captured at pull time. We
  do NOT write into Item.valuation_rate (auto-managed by ERPNext's
  stock ledger; manual writes create accounting noise) or standard_rate
  (touch only on FDE confirmation). These two are the source of truth
  for what EE believed at pull time; the FDE decides what to do.

Idempotent — `create_custom_fields` skips fields that already exist.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Item": [
                {
                    "fieldname": "ecs_ee_section",
                    "label": "EasyEcom",
                    "fieldtype": "Section Break",
                    "insert_after": "ecs_cess",
                    "collapsible": 1,
                    "description": (
                        "EasyEcom-origin product attributes captured by the §8d "
                        "pull. Read-only on the form — edits must happen in EE "
                        "(during onboarding) or on the corresponding EasyEcom "
                        "Item Map row."
                    ),
                },
                {
                    "fieldname": "ecs_ee_product_id",
                    "label": "EasyEcom product_id",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_section",
                    "read_only": 1,
                    "no_copy": 1,
                    "description": (
                        "EE internal product identifier. The EasyEcom Item Map "
                        "owns this relationship; this field surfaces it on the "
                        "Item for quick visibility. Push endpoints (Update / "
                        "ActivateDeactivate) accept this as a key."
                    ),
                },
                {
                    "fieldname": "ecs_ee_cp_id",
                    "label": "EasyEcom cp_id",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_product_id",
                    "read_only": 1,
                    "no_copy": 1,
                },
                {
                    "fieldname": "ecs_size",
                    "label": "EE Size",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_cp_id",
                    "read_only": 1,
                    "description": (
                        "Size attribute from the EE payload. ERPNext's Item "
                        "Variants machinery is intentionally not engaged here "
                        "(it's its own separate concern); this is just the EE "
                        "attribute captured for parity."
                    ),
                },
                {
                    "fieldname": "ecs_colour",
                    "label": "EE Colour",
                    "fieldtype": "Data",
                    "insert_after": "ecs_size",
                    "read_only": 1,
                },
                {
                    "fieldname": "ecs_ee_col_2",
                    "fieldtype": "Column Break",
                    "insert_after": "ecs_colour",
                },
                {
                    "fieldname": "ecs_height_cm",
                    "label": "EE Height (cm)",
                    "fieldtype": "Float",
                    "insert_after": "ecs_ee_col_2",
                    "read_only": 1,
                    "description": "Captured from EE payload (cm). EE's units.",
                },
                {
                    "fieldname": "ecs_length_cm",
                    "label": "EE Length (cm)",
                    "fieldtype": "Float",
                    "insert_after": "ecs_height_cm",
                    "read_only": 1,
                },
                {
                    "fieldname": "ecs_width_cm",
                    "label": "EE Width (cm)",
                    "fieldtype": "Float",
                    "insert_after": "ecs_length_cm",
                    "read_only": 1,
                },
                {
                    "fieldname": "ecs_ee_cost",
                    "label": "EE Cost",
                    "fieldtype": "Currency",
                    "insert_after": "ecs_width_cm",
                    "read_only": 1,
                    "description": (
                        "EE's `cost` at pull time. NOT written into Item."
                        "valuation_rate (auto-managed by the stock ledger). "
                        "FDE decides whether to roll into ERPNext pricing."
                    ),
                },
                {
                    "fieldname": "ecs_ee_mrp",
                    "label": "EE MRP",
                    "fieldtype": "Currency",
                    "insert_after": "ecs_ee_cost",
                    "read_only": 1,
                    "description": (
                        "EE's `mrp` at pull time. The pull also writes EE's "
                        "mrp into Item.standard_rate as the selling-price "
                        "best-fit; this field preserves the original value."
                    ),
                },
            ]
        },
        ignore_validate=True,
    )
    frappe.db.commit()
    print("[ecommerce_super] ensured §8d Stage-2 Item Custom Fields exist")
