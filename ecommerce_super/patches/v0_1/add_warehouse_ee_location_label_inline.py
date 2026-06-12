"""Rescue patch: create `Warehouse.ecs_ee_location_label` from EMBEDDED
spec when the earlier patch silently no-op'd (gh#26).

The original patch `add_warehouse_ee_location_label` uses
`frappe.custom.doctype.custom_field.create_custom_fields`, which has a
known footgun: if the `insert_after` target field doesn't exist on the
DocType at the moment the patch runs (e.g. another vertical app
removed `warehouse_name` or hadn't created it yet), the call quietly
returns without creating the field. Patch Log records "executed", the
field never exists, and every downstream consumer
(`warehouse_query.warehouse_with_ee_label`,
`warehouse_query.predict_section10_branch`,
`warehouse_label_sync.refresh`) raises
`MySQLdb.OperationalError: Unknown column 'ecs_ee_location_label'`.

Confirmed live on `mmpl16` (Frappe Cloud UAT, 2026-06-12 — see gh#26):
the prior patch appears in Patch Log but the column doesn't exist.

This patch is the rescue path. It:
  1. Checks whether the column already exists — if yes, no-op.
  2. Looks up the Custom Field row — if absent, creates one with the
     embedded spec (no `insert_after`; Frappe falls back to "end of
     fields" which is fine for a read-only metadata field).
  3. Calls `frappe.clear_cache(doctype="Warehouse")` so the next form
     load picks up the meta.
  4. Triggers `frappe.db.updatedb` (sync_for) on the Warehouse DocType
     to ensure the column actually materializes in the schema.

Idempotent: re-runs after the column exists no-op via the early check.
"""

from __future__ import annotations

import frappe


_FIELDNAME = "ecs_ee_location_label"
_CUSTOM_FIELD_NAME = f"Warehouse-{_FIELDNAME}"


def execute() -> None:
    # 1. Already there → no-op.
    try:
        if frappe.db.has_column("Warehouse", _FIELDNAME):
            return
    except Exception:
        return  # Warehouse table missing → not an EE install we should
                # touch.

    # 2. Find or create the Custom Field row. We don't use
    # `create_custom_fields` because its silent-no-op on missing
    # insert_after is the very bug we're rescuing.
    existing = frappe.db.get_value(
        "Custom Field",
        {"dt": "Warehouse", "fieldname": _FIELDNAME},
        "name",
    )
    if not existing:
        doc = frappe.new_doc("Custom Field")
        doc.update(
            {
                "dt": "Warehouse",
                "fieldname": _FIELDNAME,
                "label": "EE Location",
                "fieldtype": "Data",
                # Deliberately NO insert_after — Frappe appends to the
                # end of the field list. UX ordering is a secondary
                # concern next to "form actually loads".
                "read_only": 1,
                "no_copy": 1,
                "in_list_view": 1,
                "in_standard_filter": 1,
                "translatable": 0,
                "length": 140,
                "description": (
                    "Auto-computed from EasyEcom Location's "
                    "mapped_warehouse. Empty when not EE-mapped "
                    "(or mapped only to a non-Live location)."
                ),
            }
        )
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        print(
            f"[ecommerce_super] gh#26: created Custom Field "
            f"{_CUSTOM_FIELD_NAME!r} (rescue path — original patch "
            "no-op'd)"
        )

    # 3. Clear the meta cache so the next form load picks up the field.
    frappe.clear_cache(doctype="Warehouse")

    # 4. Materialize the column in the schema. Frappe's
    # `db.updatedb` (sync_for) reads the DocType's full field set
    # (built-in + custom) and runs ALTER TABLE to align the schema.
    # Without this the Custom Field row exists but the underlying
    # MariaDB column doesn't — the original symptom on mmpl16.
    try:
        frappe.db.updatedb("Warehouse")
    except Exception as exc:
        # If updatedb itself fails (unusual), log and let the next
        # `bench migrate` retry — we don't want this patch to
        # block the rest of the migration.
        frappe.log_error(
            title="gh#26: updatedb('Warehouse') failed after Custom Field creation",
            message=f"{type(exc).__name__}: {exc}",
        )

    frappe.db.commit()
