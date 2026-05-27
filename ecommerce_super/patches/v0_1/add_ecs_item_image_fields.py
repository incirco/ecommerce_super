"""Custom Field on Item for §8d Stage-2 image pull (Option A).

ERPNext Item ships a native `image` field (Attach Image) — that's
where the primary product image lands (the §8d pull just writes EE's
`product_image_url` directly as a URL string; no download, no Frappe
File doc — Item.image accepts either an attached file path OR a
plain URL and renders an <img> from whichever it finds).

EE also returns an `additional_images` list per product (typically
2–4 secondary URLs — back/side/detail shots). ERPNext has no native
multi-image field on Item; we capture those as a JSON array in a
Long Text custom field for later use by:
  - Portal / customer-facing surfaces (render a gallery).
  - Sales Order / Sales Invoice line-item image enrichment.
  - Drift comparisons (deliberately NOT compared — CDN re-uploads
    cycle URLs even when the visual is identical; would generate
    false-positive drift). Captured for visibility, not for
    reconciliation.

Idempotent — create_custom_fields skips fields that already exist.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Item": [
                {
                    "fieldname": "ecs_additional_image_urls",
                    "label": "EE Additional Image URLs",
                    "fieldtype": "Long Text",
                    "insert_after": "ecs_ee_mrp",
                    "read_only": 1,
                    "description": (
                        "JSON array of secondary image URLs captured from "
                        "EE's `additional_images` field on the product "
                        "payload. The primary image lands in the standard "
                        "Item.image field (URL string). Both surfaces "
                        "expect EE's S3 URLs to remain reachable; we do "
                        "NOT download or proxy. Not drift-comparable."
                    ),
                },
            ]
        },
        ignore_validate=True,
    )
    frappe.db.commit()
    print("[ecommerce_super] ensured §8d image pull field exists")
