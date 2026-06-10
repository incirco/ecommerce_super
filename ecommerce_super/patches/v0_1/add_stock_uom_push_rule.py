"""Add missing `stock_uom → accounting_unit` rule to EasyEcom-Item-Push
ruleset (gh#38).

The Pull ruleset (`EasyEcom-Item-Pull`) had `accounting_unit → stock_uom`
since §8d Stage 2 (with the dirty-UOM substitution in the flow). The
Push ruleset (`EasyEcom-Item-Push`) was missing the inverse rule
entirely — ERPNext-side stock_uom updates were silently omitted from
the EE payload, so EE-side `accounting_unit` stayed frozen at its
Create-time value even when the FDE corrected the UOM in ERPNext.

This patch surgically appends the missing rule on existing sites if it
isn't already present. Fresh installs and sites that re-import the
JSON fixture pick up the rule via the standard fixture loader; this
patch is for deployed sites where the EasyEcom-Item-Push parent doc
already exists with v3-shape rules.

Idempotent: re-running is a no-op once the rule is present.

We do NOT force-overwrite the parent doc because the ruleset is
FDE-editable per the §8d Stage 3 design — surgical append preserves
any FDE-side customisations to the existing rules.
"""

from __future__ import annotations

import frappe


_RULESET_NAME = "EasyEcom-Item-Push"
_ERPNEXT_PATH = "stock_uom"
_EASYECOM_PATH = "accounting_unit"


def execute() -> None:
    if not frappe.db.table_exists("EasyEcom Field Mapping"):
        return  # pre-§8d Stage 2 install — fixture loader will handle it
    if not frappe.db.exists("EasyEcom Field Mapping", _RULESET_NAME):
        return  # ruleset hasn't shipped yet — fixture loader will plant it

    doc = frappe.get_doc("EasyEcom Field Mapping", _RULESET_NAME)
    for rule in doc.get("rules") or []:
        if rule.erpnext_path == _ERPNEXT_PATH and rule.easyecom_path == _EASYECOM_PATH:
            return  # already present — no-op

    # Append the missing rule. Idempotent via the early return above.
    doc.append(
        "rules",
        {
            "erpnext_path": _ERPNEXT_PATH,
            "easyecom_path": _EASYECOM_PATH,
            "transform_push": "identity",
            "transform_pull": "identity",
            "notes": (
                "gh#38 — added by patch on deployed sites. The Pull side has "
                "had this rule (accounting_unit → stock_uom) since Stage 2 "
                "but the Push side was omitting it entirely, so ERPNext-side "
                "UOM corrections never propagated to EE. Identity push: "
                "ERPNext's stock_uom lands on EE's accounting_unit as-is."
            ),
        },
    )
    # Bump version so any cached compilations re-fetch.
    doc.version = (doc.version or 1) + 1
    doc.last_modified_by = "Administrator"
    doc.last_modified_at = frappe.utils.now_datetime()
    doc.change_reason = (
        (doc.change_reason or "")
        + " | gh#38 patch: append stock_uom → accounting_unit push rule."
    )
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(
        f"[ecommerce_super] gh#38: appended {_ERPNEXT_PATH} → "
        f"{_EASYECOM_PATH} push rule to {_RULESET_NAME!r}"
    )
