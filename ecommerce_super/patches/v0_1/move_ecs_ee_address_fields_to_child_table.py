"""Move the 15 flat ecs_ee_billing_* / ecs_ee_shipping_* Custom Fields
on Sales Invoice into a Table child (EasyEcom Address).

BACKGROUND

  add_ecs_ee_buyer_address_si_fields (from #259, hotfixed by #265)
  added 13 varchar columns to tabSales Invoice. Even after the
  Data → Small Text hotfix, every one of these still carries a
  pointer in the SI row — SI is already close to the MariaDB
  65535-byte per-row limit (~150 fields between ERPNext + IC +
  puresta_erpnext_custom_app + our own).

  This patch moves those 13 fields off SI entirely by hosting them
  on a child DocType (EasyEcom Address, istable=1). Table Custom
  Fields on the parent add ZERO DB columns — the child rows link
  back via parent / parenttype / parentfield. Net row-size impact
  on tabSales Invoice: zero.

WHAT THIS PATCH DOES (idempotent)

  1. Adds the `ecs_ee_address` Table Custom Field on Sales Invoice
     (options=EasyEcom Address, insert_after=ecs_settlement_completed_at).

  2. For every SI that has data in ANY of the old ecs_ee_billing_*
     / ecs_ee_shipping_* fields, creates one child row in
     tabEasyEcom Address with the same values, linked via
     parent/parenttype/parentfield=ecs_ee_address.

  3. Removes the old 15 Custom Field records (13 Data + 2 breaks).
     Frappe's Custom Field delete hook drops the corresponding DB
     column, freeing the row-size budget on tabSales Invoice.

FIELD NAME MAPPING (old flat name → new child column)

  ecs_ee_billing_name         → billing_name
  ecs_ee_billing_address_1    → billing_address_1
  ecs_ee_billing_address_2    → billing_address_2
  ecs_ee_billing_city         → billing_city
  ecs_ee_billing_state        → billing_state
  ecs_ee_billing_pincode      → billing_pincode
  ecs_ee_billing_country      → billing_country
  ecs_ee_shipping_address_1   → shipping_address_1
  ecs_ee_shipping_address_2   → shipping_address_2
  ecs_ee_shipping_city        → shipping_city
  ecs_ee_shipping_state       → shipping_state
  ecs_ee_shipping_pincode     → shipping_pincode
  ecs_ee_shipping_country     → shipping_country

SITE-STATE HANDLING (three possible states this patch may encounter)

  A. Sites where #259/#265 landed successfully → old fields exist
     with data on some SIs. Full migration + cleanup path runs.

  B. Sites where #259 FAILED (row-size overflow, no #265 deployed
     yet) → old Custom Field records exist in tabCustom Field but
     the DB columns don't. Data migration is a no-op; Custom Field
     record cleanup + Table field add still run.

  C. Fresh sites → neither old Custom Fields nor DB columns exist.
     Only the Table Custom Field add runs.

  All three paths are safe re-runs — every step guards on existence.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


OLD_FIELD_MAP = {
    # old_fieldname → new child column name
    "ecs_ee_billing_name":       "billing_name",
    "ecs_ee_billing_address_1":  "billing_address_1",
    "ecs_ee_billing_address_2":  "billing_address_2",
    "ecs_ee_billing_city":       "billing_city",
    "ecs_ee_billing_state":      "billing_state",
    "ecs_ee_billing_pincode":    "billing_pincode",
    "ecs_ee_billing_country":    "billing_country",
    "ecs_ee_shipping_address_1": "shipping_address_1",
    "ecs_ee_shipping_address_2": "shipping_address_2",
    "ecs_ee_shipping_city":      "shipping_city",
    "ecs_ee_shipping_state":     "shipping_state",
    "ecs_ee_shipping_pincode":   "shipping_pincode",
    "ecs_ee_shipping_country":   "shipping_country",
}

# Non-data Custom Field records to remove (form-layout markers)
OLD_LAYOUT_FIELDS = [
    "ecs_ee_buyer_address_section",
    "ecs_ee_shipping_col_break",
]


def execute() -> None:
    # Step 1: add the Table Custom Field on Sales Invoice
    create_custom_fields(
        {
            "Sales Invoice": [
                {
                    "fieldname": "ecs_ee_address",
                    "label": "EasyEcom Address",
                    "fieldtype": "Table",
                    "options": "EasyEcom Address",
                    "insert_after": "ecs_settlement_completed_at",
                    "description": (
                        "EE-supplied billing + shipping address as a "
                        "child row. Moved to a Table child in "
                        "move_ecs_ee_address_fields_to_child_table "
                        "so the address columns don't inflate the "
                        "already-heavy tabSales Invoice row size."
                    ),
                }
            ]
        }
    )

    # Step 2: migrate data (only if the old DB columns exist)
    columns = frappe.db.get_table_columns("Sales Invoice")
    old_cols_present = [c for c in OLD_FIELD_MAP if c in columns]
    if old_cols_present:
        _migrate_existing_si_address_data(old_cols_present)

    # Step 3: remove old Custom Field records (data + layout).
    # Frappe's Custom Field on_trash drops the corresponding DB column.
    _remove_old_custom_fields()

    frappe.clear_cache(doctype="Sales Invoice")
    frappe.clear_cache(doctype="EasyEcom Address")


def _migrate_existing_si_address_data(old_cols_present: list[str]) -> None:
    """Read every SI that has ANY non-empty old address column,
    materialise a child row in tabEasyEcom Address."""
    select_cols = ", ".join(f"`{c}`" for c in old_cols_present)
    where_any = " OR ".join(
        f"(`{c}` IS NOT NULL AND `{c}` != '')" for c in old_cols_present
    )
    rows = frappe.db.sql(
        f"""SELECT name, {select_cols}
            FROM `tabSales Invoice`
            WHERE {where_any}""",
        as_dict=True,
    )
    print(
        f"[move_ecs_ee_address] found {len(rows):,} SIs with legacy "
        f"address data — migrating to child rows"
    )
    migrated = 0
    for r in rows:
        si_name = r["name"]
        # Skip if a child row already exists (idempotent re-run)
        if frappe.db.exists(
            "EasyEcom Address",
            {"parenttype": "Sales Invoice",
             "parent": si_name,
             "parentfield": "ecs_ee_address"},
        ):
            continue
        # Build child dict — only non-empty values
        payload = {
            OLD_FIELD_MAP[old_col]: r[old_col]
            for old_col in old_cols_present
            if r.get(old_col)
        }
        if not payload:
            continue
        payload.update({
            "doctype": "EasyEcom Address",
            "parenttype": "Sales Invoice",
            "parent": si_name,
            "parentfield": "ecs_ee_address",
        })
        try:
            child = frappe.get_doc(payload)
            child.flags.ignore_permissions = True
            child.insert(ignore_permissions=True, ignore_links=True)
            migrated += 1
        except Exception as exc:
            frappe.logger().warning(
                f"[move_ecs_ee_address] SI {si_name}: could not create "
                f"child row: {type(exc).__name__}: {exc}"
            )
    frappe.db.commit()
    print(f"[move_ecs_ee_address] migrated {migrated:,} child rows")


def _remove_old_custom_fields() -> None:
    """Delete the 13 data-field Custom Field records + 2 layout marker
    records. Frappe's Custom Field on_trash drops the corresponding
    DB column when the record is deleted, freeing row-size budget."""
    to_delete = list(OLD_FIELD_MAP.keys()) + OLD_LAYOUT_FIELDS
    removed = 0
    for fieldname in to_delete:
        try:
            cf_name = frappe.db.get_value(
                "Custom Field",
                {"dt": "Sales Invoice", "fieldname": fieldname},
                "name",
            )
            if cf_name:
                frappe.delete_doc(
                    "Custom Field", cf_name,
                    ignore_permissions=True, force=True,
                )
                removed += 1
        except Exception as exc:
            frappe.logger().warning(
                f"[move_ecs_ee_address] could not remove Custom Field "
                f"{fieldname!r}: {type(exc).__name__}: {exc}"
            )
    frappe.db.commit()
    print(f"[move_ecs_ee_address] removed {removed} old Custom Field records")
