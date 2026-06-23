"""§11 Phase 1 substrate — B2B configuration Custom Fields.

Adds three Custom Fields:
  1. EasyEcom Account.ecs_b2b_module      (Select: "" | "Old B2B" | "New B2B")
  2. EasyEcom Account.ecs_eway_origination (Select: "EasyEcom" | "ERPNext")
  3. Sales Order.ecs_b2b_order_map         (Link → EasyEcom B2B Order Map, read-only)

Uses the plain `doc.insert` rescue pattern (the same one
add_warehouse_ee_location_label_inline established for gh#26 / gh#48),
NOT `create_custom_fields`. After each insert, verifies the
column exists; if not, calls `db.updatedb` and re-checks. Raises
loudly if the field still isn't materialized — install-time
verification specifically for §11, not the broader install-verify
layer (that's a separate workstream per gh#48 / PR #53).

Idempotent: re-runs no-op via the column-exists check.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    _ensure_field(
        dt="EasyEcom Account",
        fieldname="ecs_b2b_module",
        spec={
            "label": "B2B Module",
            "fieldtype": "Select",
            "options": "\nOld B2B\nNew B2B",
            "default": "",
            "description": (
                "Which EasyEcom B2B module this account uses. Old B2B "
                "returns identifiers synchronously; New B2B queues "
                "asynchronously. Must match the EE-side account "
                "configuration."
            ),
        },
    )
    _ensure_field(
        dt="EasyEcom Account",
        fieldname="ecs_eway_origination",
        spec={
            "label": "E-way Origination",
            "fieldtype": "Select",
            "options": "EasyEcom\nERPNext",
            "default": "EasyEcom",
            "description": (
                "Where the IRN and e-way bill are generated for B2B "
                "orders. EasyEcom = EE handles via its default GSP. "
                "ERPNext = EE calls ERPNext's Custom GSP endpoint "
                "synchronously. Phase 2 build only — no behavioral "
                "effect until Phase 2 ships."
            ),
        },
    )
    _ensure_field(
        dt="EasyEcom Account",
        fieldname="ecs_polling_cadence_minutes",
        spec={
            "label": "B2B Polling Cadence (minutes)",
            "fieldtype": "Int",
            "default": "15",
            "description": (
                "How often each pending B2B Order Map is re-polled "
                "via /orders/V2/getOrderDetails. The scheduler tick "
                "is fixed at 5 minutes; this field gates which Maps "
                "get re-polled per tick. Default 15 — B2B invoice "
                "lifecycle is hours-to-days, so faster polling burns "
                "EE quota without operational benefit. EE's own "
                "marketplace cancellation sync runs every 5 min, so "
                "anything faster is wasted."
            ),
        },
    )
    _ensure_field(
        dt="Sales Order",
        fieldname="ecs_b2b_order_map",
        spec={
            "label": "EE B2B Order Map",
            "fieldtype": "Link",
            "options": "EasyEcom B2B Order Map",
            "read_only": 1,
            "in_list_view": 0,
            "no_copy": 1,
            "description": (
                "Link to the EasyEcom B2B Order Map for this SO. "
                "Populated automatically on submit if §11 push fires. "
                "Read-only for users."
            ),
        },
    )


def _ensure_field(*, dt: str, fieldname: str, spec: dict) -> None:
    """Create the Custom Field via plain doc.insert + verify column.

    Bypasses create_custom_fields entirely (the silent-no-op race
    documented in gh#26 / gh#48). Verifies post-insert that the
    underlying column materialized; raises loudly if not.
    """
    # Probe via has_column on `name` (always present on any DocType
    # table) — sidesteps Frappe's table_exists tab-prefix ambiguity.
    try:
        if not frappe.db.has_column(dt, "name"):
            return
    except Exception:
        # Parent DocType not yet on this site (pre-EE install or
        # stripped deployment) — patch is a no-op. The audit
        # framework (gh#48 PR #53) catches any unfulfilled
        # expectations later.
        return

    # 1. Column already there → no-op.
    if frappe.db.has_column(dt, fieldname):
        return

    # 2. Find or create the Custom Field row.
    existing = frappe.db.get_value(
        "Custom Field", {"dt": dt, "fieldname": fieldname}, "name"
    )
    if not existing:
        doc = frappe.new_doc("Custom Field")
        doc.update({"dt": dt, "fieldname": fieldname, **spec})
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        print(
            f"[ecommerce_super] §11 Phase 1: created Custom Field "
            f"{dt}.{fieldname}"
        )

    # 3. Materialize the column. db.updatedb reads the doctype's
    # full field set (built-in + custom) and runs ALTER TABLE.
    frappe.clear_cache(doctype=dt)
    try:
        frappe.db.updatedb(dt)
    except Exception as exc:
        frappe.log_error(
            title=(
                f"§11 Phase 1: updatedb({dt!r}) failed after "
                f"creating {fieldname!r}"
            ),
            message=f"{type(exc).__name__}: {exc}",
        )

    frappe.db.commit()

    # 4. Verify the column actually exists. The gh#48 hardening
    # specifically for §11 — if the spec says "this field exists
    # after this patch", that contract must hold or the patch
    # fails loud rather than silently leaving the DocType in
    # half-installed state.
    if not frappe.db.has_column(dt, fieldname):
        frappe.throw(
            f"§11 Phase 1 patch failed to materialize the {dt}.{fieldname} "
            "column. Custom Field row may exist but the schema column "
            "didn't materialize — investigate before re-running migrate."
        )

    # 5. Also verify the Custom Field row exists (defence in depth —
    # the column might exist from a prior install path while the
    # row was deleted).
    if not frappe.db.exists(
        "Custom Field", {"dt": dt, "fieldname": fieldname}
    ):
        frappe.throw(
            f"§11 Phase 1 patch: {dt}.{fieldname} column exists but "
            "tabCustom Field row is missing. Schema and metadata are "
            "out of sync — investigate."
        )
