"""Update EasyEcom-Item-Push dimension push rules to read a fallback
chain for ERPNext-born Items (gh#44).

The original rules (`ecs_length_cm`, `ecs_width_cm`, `ecs_height_cm`,
`weight_per_unit`) read only the canonical EE-pull-populated fields.
ERPNext-born Items typically carry their physical attributes in
vendor- or migration-specific custom fields (`custom_*`,
`unicommerce_item_*`), so the canonical fields stay empty and the
push validator flags "Length missing or zero" even though the Item
form shows valid values.

The new expressions coalesce through:
  Weight: weight_per_unit → custom_weight → unicommerce_item_weight → 0
  Length: ecs_length_cm → custom_length → unicommerce_item_length → length → 0
  Height: ecs_height_cm → custom_height → unicommerce_item_height → height → 0
  Width:  ecs_width_cm → custom_width → custom_breadth → unicommerce_item_width → width → 0

UOM conversion logic unchanged — reads weight_uom / ecs_dim_uom as before.

This patch updates the four rules on the deployed EasyEcom-Item-Push
parent doc (if it exists), bumps the doc's `version` field so any
cached compilations re-fetch, and is idempotent (re-runs no-op when
the expressions already match the new shape).

We do NOT force-overwrite the parent doc because the ruleset is
FDE-editable per §8d Stage 3 design — surgical update preserves any
FDE-side customisations to the OTHER rules (Cost fallback,
Brand fallback, materialType conditional, etc.).
"""

from __future__ import annotations

import frappe


_RULESET_NAME = "EasyEcom-Item-Push"


# Target expressions per (erpnext_path, easyecom_path). Patch applies
# only when the rule's current expression doesn't already contain the
# fallback chain marker — making re-runs idempotent and protecting any
# FDE-side custom expression that's already been migrated.
_TARGETS: list[dict] = [
    {
        "erpnext_path": "weight_per_unit",
        "easyecom_path": "Weight",
        "fallback_marker": 'source_doc.get("custom_weight")',
        "expression": (
            "int(round((value or source_doc.get(\"custom_weight\") or "
            "source_doc.get(\"unicommerce_item_weight\") or 0) * "
            "{\"Kg\": 1000, \"Gram\": 1, \"Mg\": 0.001, \"Lbs\": 453.592, "
            "\"Oz\": 28.3495, \"Tonne\": 1000000}.get(source_doc.weight_uom, 1)))"
        ),
    },
    {
        "erpnext_path": "ecs_length_cm",
        "easyecom_path": "Length",
        "fallback_marker": 'source_doc.get("custom_length")',
        "expression": (
            "int(round((value or source_doc.get(\"custom_length\") or "
            "source_doc.get(\"unicommerce_item_length\") or "
            "source_doc.get(\"length\") or 0) * "
            "{\"Cm\": 1, \"M\": 100, \"Mm\": 0.1, \"Inch\": 2.54, "
            "\"Ft\": 30.48}.get(source_doc.get(\"ecs_dim_uom\"), 1)))"
        ),
    },
    {
        "erpnext_path": "ecs_height_cm",
        "easyecom_path": "Height",
        "fallback_marker": 'source_doc.get("custom_height")',
        "expression": (
            "int(round((value or source_doc.get(\"custom_height\") or "
            "source_doc.get(\"unicommerce_item_height\") or "
            "source_doc.get(\"height\") or 0) * "
            "{\"Cm\": 1, \"M\": 100, \"Mm\": 0.1, \"Inch\": 2.54, "
            "\"Ft\": 30.48}.get(source_doc.get(\"ecs_dim_uom\"), 1)))"
        ),
    },
    {
        "erpnext_path": "ecs_width_cm",
        "easyecom_path": "Width",
        "fallback_marker": 'source_doc.get("custom_width")',
        "expression": (
            "int(round((value or source_doc.get(\"custom_width\") or "
            "source_doc.get(\"custom_breadth\") or "
            "source_doc.get(\"unicommerce_item_width\") or "
            "source_doc.get(\"width\") or 0) * "
            "{\"Cm\": 1, \"M\": 100, \"Mm\": 0.1, \"Inch\": 2.54, "
            "\"Ft\": 30.48}.get(source_doc.get(\"ecs_dim_uom\"), 1)))"
        ),
    },
]


def execute() -> None:
    if not frappe.db.table_exists("EasyEcom Field Mapping"):
        return
    if not frappe.db.exists("EasyEcom Field Mapping", _RULESET_NAME):
        return  # fresh install — fixture loader plants the v5 directly.

    doc = frappe.get_doc("EasyEcom Field Mapping", _RULESET_NAME)
    rules = doc.get("rules") or []
    updated_any = False
    for target in _TARGETS:
        for rule in rules:
            if (
                rule.erpnext_path != target["erpnext_path"]
                or rule.easyecom_path != target["easyecom_path"]
            ):
                continue
            current_args = rule.transform_args or "{}"
            # Idempotency check: skip if the marker (the first fallback
            # field) is already present in the expression. Protects both
            # re-runs of this patch AND any FDE-side expression edit
            # that's already added the chain.
            if target["fallback_marker"] in current_args:
                continue
            # Rebuild transform_args preserving anything else the rule
            # might have (currently just `expression`, but defensive).
            try:
                args_dict = frappe.parse_json(current_args) or {}
            except Exception:
                args_dict = {}
            args_dict["expression"] = target["expression"]
            rule.transform_args = frappe.as_json(args_dict)
            updated_any = True
            break

    if not updated_any:
        return

    doc.version = (doc.version or 1) + 1
    doc.last_modified_by = "Administrator"
    doc.last_modified_at = frappe.utils.now_datetime()
    doc.change_reason = (
        (doc.change_reason or "")
        + " | gh#44 patch: add custom_*/unicommerce_item_*/<stock> "
        "fallback chain to Weight/Length/Height/Width push expressions."
    )
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(
        f"[ecommerce_super] gh#44: updated dimension push expressions on "
        f"{_RULESET_NAME!r}"
    )
