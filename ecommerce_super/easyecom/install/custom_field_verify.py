"""Custom Field verification + auto-rescue (gh#48).

Patches that use `frappe.custom.doctype.custom_field.create_custom_fields`
have an observed silent-no-op failure mode on fresh installs: when the
patch fires before the parent DocType's meta is fully loaded into the
Frappe registry (a race intrinsic to `bench install-app`), the call
returns normally, `tabPatch Log` records "executed", but the underlying
column is never materialized. Documented on `ci-test.local` 2026-06-12
(see gh#48 reproduction); confirmed on `mmpl16` for the
`Warehouse.ecs_ee_location_label` column (gh#26).

This module provides:

  1. `verify_custom_field(dt, fieldname, ...)` — checks both the
     `tabCustom Field` row AND the underlying schema column. Returns
     an outcome string callers can branch on.

  2. `ensure_custom_field(dt, fieldname, spec)` — calls
     `verify_custom_field`, and if anything is missing, creates the
     field via plain `frappe.new_doc().insert()` (which triggers the
     per-doc `clear_cache + updatedb` in Custom Field's on_update)
     instead of the buggy `create_custom_fields` path. Idempotent;
     re-runs are a no-op once the field exists.

  3. `EXPECTED_FIELDS` — the canonical registry of every Custom Field
     the integration ships. The audit patch
     (`patches/v0_1/audit_and_rescue_custom_fields.py`) walks this list
     to rescue any field whose patch silently no-op'd.

The registry is the source of truth, deliberately duplicated from the
individual patches' specs. Two copies of the truth is the right tradeoff
here because:
  - The patches stay simple and call-site-tested (each does one thing).
  - The audit pattern is debuggable: one file lists everything that
    should exist.
  - A registry-vs-patch drift would surface either as a missing field
    (the audit creates it) or an unwanted field (no auto-deletion;
    requires manual intervention — which is correct, deletion is
    destructive).
"""

from __future__ import annotations

from typing import Any, Literal

import frappe


# Outcomes returned by `verify_custom_field`. Documented here so
# downstream code can branch on a known set.
VerifyOutcome = Literal[
    "ok",
    "missing_row",
    "missing_column",
    "missing_row_and_column",
    "doctype_missing",
]


def verify_custom_field(
    dt: str, fieldname: str, *, expected_fieldtype: str | None = None
) -> VerifyOutcome:
    """Check whether the Custom Field on `dt.fieldname` materialized.

    Returns one of:
      - "ok"                       — row exists in tabCustom Field AND
                                     the column exists on the schema.
      - "missing_row"              — row absent, column present (someone
                                     created the column manually).
      - "missing_column"           — row present, column absent
                                     (`create_custom_fields` silent
                                     no-op).
      - "missing_row_and_column"   — neither present (patch didn't
                                     run at all).
      - "doctype_missing"          — parent DocType isn't on this
                                     site (pre-EE install or stripped
                                     deployment).

    Defensive: catches every internal Frappe exception and returns the
    most pessimistic outcome rather than raising, so verification
    failures never block a migration step that called us for guidance.
    """
    if expected_fieldtype is None:
        expected_fieldtype = "Data"

    # `frappe.db.table_exists` already prepends "tab" internally —
    # passing `f"tab{dt}"` makes it probe for `tabtab<dt>` and report
    # every DocType as missing. Probe via `has_column(dt, "name")`
    # instead — every DocType table has a `name` column. This is the
    # exact pattern PR #53's smoke-precheck was meant to teach but
    # the audit framework itself had this same bug, defeating the
    # entire rescue path on production benches (live finding
    # 2026-06-16: smoke-test.local reported all 10 EXPECTED_FIELDS
    # as `doctype_missing` despite Item and Warehouse being present).
    try:
        if not frappe.db.has_column(dt, "name"):
            return "doctype_missing"
    except Exception:
        return "doctype_missing"

    try:
        row_exists = bool(
            frappe.db.exists(
                "Custom Field", {"dt": dt, "fieldname": fieldname}
            )
        )
    except Exception:
        row_exists = False

    try:
        column_exists = bool(frappe.db.has_column(dt, fieldname))
    except Exception:
        column_exists = False

    if row_exists and column_exists:
        return "ok"
    if row_exists and not column_exists:
        return "missing_column"
    if column_exists and not row_exists:
        return "missing_row"
    return "missing_row_and_column"


def ensure_custom_field(dt: str, fieldname: str, spec: dict[str, Any]) -> str:
    """Verify + repair a Custom Field via the plain-`doc.insert` path.

    Mirrors the gh#26 inline rescue pattern: bypasses `create_custom_fields`
    entirely so its silent-no-op race during install can't trip us.

    `spec` should include the Frappe Custom Field fields (`label`,
    `fieldtype`, `read_only`, etc.) — `dt` and `fieldname` are passed
    separately because they're the identity. `insert_after` is
    intentionally NOT respected: this rescue path puts the field at the
    end of the field list because the UX-ordering concern is secondary
    to having the column exist at all. Sites where the original patch
    ran successfully keep their original `insert_after` placement —
    `ensure_custom_field` is a no-op for them.

    Returns the Custom Field docname or "" if the doctype is missing.
    """
    outcome = verify_custom_field(
        dt, fieldname, expected_fieldtype=spec.get("fieldtype")
    )
    if outcome == "ok":
        return f"{dt}-{fieldname}"
    if outcome == "doctype_missing":
        # Nothing we can do until the parent DocType ships. Don't raise
        # — bench migrate against a partial install must not crash on us.
        return ""

    # Find or create the Custom Field row.
    name = frappe.db.get_value(
        "Custom Field", {"dt": dt, "fieldname": fieldname}, "name"
    )

    if not name:
        doc = frappe.new_doc("Custom Field")
        doc.update({"dt": dt, "fieldname": fieldname, **_sanitise_spec(spec)})
        doc.insert(ignore_permissions=True)
        name = doc.name
        frappe.db.commit()

    # At this point the row exists. Re-verify and force a schema sync
    # if the column still isn't there.
    after_insert = verify_custom_field(dt, fieldname)
    if after_insert == "missing_column":
        # Schema didn't materialize on insert (unusual but observed).
        # Force the doctype-level updatedb.
        try:
            frappe.clear_cache(doctype=dt)
            frappe.db.updatedb(dt)
            frappe.db.commit()
        except Exception as exc:
            frappe.log_error(
                title=f"gh#48: updatedb({dt!r}) failed",
                message=f"{type(exc).__name__}: {exc}",
            )

    return name


def _sanitise_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Filter the spec to fields the Custom Field DocType actually
    accepts. Drops `dt` and `fieldname` (we set those explicitly) and
    `insert_after` (the rescue path doesn't honour positional placement)."""
    drop = {"dt", "fieldname", "insert_after"}
    return {k: v for k, v in spec.items() if k not in drop}


@frappe.whitelist()
def run_audit() -> dict[str, Any]:
    """Walk the EXPECTED_FIELDS registry, rescue any missing fields,
    and return a structured summary.

    Returns:
      {
        "total": int,
        "ok": int,
        "rescued": int,
        "doctype_missing": int,
        "details": [{"dt": ..., "fieldname": ..., "before": ...,
                     "after": ...}, ...]
      }

    Safe to call from `after_install`, from a patch, from a bench
    console command, or via HTTP for sites without shell access:
        GET /api/method/ecommerce_super.easyecom.install.custom_field_verify.run_audit
    (must be authenticated as System Manager — the rescue writes
    Custom Field rows and mutates schema).
    """
    # Permission gate — the rescue path inserts Custom Field rows and
    # runs `updatedb`, which would let any whitelisted-API caller
    # mutate schema. Restrict to System Manager + Administrator.
    if frappe.session.user != "Administrator" and (
        "System Manager" not in frappe.get_roles(frappe.session.user)
    ):
        frappe.throw(
            "run_audit requires the System Manager role.",
            frappe.PermissionError,
        )
    summary: dict[str, Any] = {
        "total": len(EXPECTED_FIELDS),
        "ok": 0,
        "rescued": 0,
        "doctype_missing": 0,
        "details": [],
    }
    for dt, fieldname, spec in EXPECTED_FIELDS:
        before = verify_custom_field(dt, fieldname)
        if before == "ok":
            summary["ok"] += 1
            continue
        if before == "doctype_missing":
            summary["doctype_missing"] += 1
            summary["details"].append(
                {"dt": dt, "fieldname": fieldname, "before": before, "after": before}
            )
            continue
        ensure_custom_field(dt, fieldname, spec)
        after = verify_custom_field(dt, fieldname)
        if after == "ok":
            summary["rescued"] += 1
        summary["details"].append(
            {"dt": dt, "fieldname": fieldname, "before": before, "after": after}
        )
    return summary


# ============================================================
# Canonical registry of all Custom Fields the integration ships.
# ============================================================
#
# Each entry is a (doctype, fieldname, spec) tuple. The audit patch
# walks this list, runs `ensure_custom_field` against each, and counts
# how many needed rescue (logged so smoke tests can assert zero-rescue
# on a healthy install).
#
# When adding a new Custom Field patch, ALSO add the field here. The
# audit's smoke-check (see test_custom_field_audit.py) will catch a
# missing entry — by design.
#
# Spec mirrors the originating patch's create_custom_fields() input
# minus `insert_after` (rescue path appends to end of field list).


EXPECTED_FIELDS: list[tuple[str, str, dict[str, Any]]] = [
    # gh#26 — Warehouse EE-mapping label.
    (
        "Warehouse",
        "ecs_ee_location_label",
        {
            "label": "EE Location",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
            "translatable": 0,
            "length": 140,
            "description": (
                "Auto-computed from EasyEcom Location's mapped_warehouse. "
                "Empty when not EE-mapped (or mapped only to a non-Live "
                "location)."
            ),
        },
    ),
    # §8d Stage 2 Item Pull data-bearing fields (originally shipped by
    # `add_ecs_item_pull_fields` via `create_custom_fields`, which has
    # the gh#48 silent-no-op race during install). Observed live on a
    # deployed bench 2026-06-16: Item Push UPDATE failed with
    # `OperationalError: Unknown column 'ecs_ee_product_id' in 'SET'`
    # despite the patch appearing in tabPatch Log. Layout-only fields
    # (Section Break ecs_ee_section, Column Break ecs_ee_col_2) are
    # deliberately NOT in this list — they don't materialize columns
    # and the verifier would loop on `missing_column` forever.
    (
        "Item", "ecs_ee_product_id",
        {
            "label": "EasyEcom product_id",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
            "description": (
                "EE internal product identifier. The EasyEcom Item Map "
                "owns this relationship; this field surfaces it on the "
                "Item for quick visibility. Push endpoints (Update / "
                "ActivateDeactivate) accept this as a key."
            ),
        },
    ),
    (
        "Item", "ecs_ee_cp_id",
        {
            "label": "EasyEcom cp_id",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
        },
    ),
    (
        "Item", "ecs_size",
        {
            "label": "EE Size",
            "fieldtype": "Data",
            "read_only": 1,
            "description": (
                "Size attribute from the EE payload. ERPNext's Item "
                "Variants machinery is intentionally not engaged here."
            ),
        },
    ),
    (
        "Item", "ecs_colour",
        {
            "label": "EE Colour",
            "fieldtype": "Data",
            "read_only": 1,
        },
    ),
    (
        "Item", "ecs_height_cm",
        {
            "label": "EE Height (cm)",
            "fieldtype": "Float",
            "read_only": 1,
            "description": "Captured from EE payload (cm). EE's units.",
        },
    ),
    (
        "Item", "ecs_length_cm",
        {
            "label": "EE Length (cm)",
            "fieldtype": "Float",
            "read_only": 1,
        },
    ),
    (
        "Item", "ecs_width_cm",
        {
            "label": "EE Width (cm)",
            "fieldtype": "Float",
            "read_only": 1,
        },
    ),
    (
        "Item", "ecs_ee_cost",
        {
            "label": "EE Cost",
            "fieldtype": "Currency",
            "read_only": 1,
            "description": (
                "EE's `cost` at pull time. NOT written into Item."
                "valuation_rate (auto-managed by the stock ledger)."
            ),
        },
    ),
    (
        "Item", "ecs_ee_mrp",
        {
            "label": "EE MRP",
            "fieldtype": "Currency",
            "read_only": 1,
            "description": (
                "EE's `mrp` at pull time. The pull also writes EE's "
                "mrp into Item.standard_rate as the selling-price "
                "best-fit; this field preserves the original value."
            ),
        },
    ),
    # §10 EE-managed Address back-pointer. Shipped by
    # `add_address_ee_location_field` via the same `create_custom_fields`
    # path with the gh#48 silent-no-op race. Live failure 2026-06-18 on
    # mmpl16.frappe.cloud: the §10 Internal Customer bootstrap's
    # Address-linking SQL `SELECT a.ecs_ee_location FROM tabAddress a`
    # raised `OperationalError: Unknown column 'a.ecs_ee_location'`
    # despite the Custom Field row being present.
    (
        "Address", "ecs_ee_location",
        {
            "label": "EasyEcom Location (managed)",
            "fieldtype": "Link",
            "options": "EasyEcom Location",
            "read_only": 1,
            "no_copy": 1,
            "in_standard_filter": 1,
            "description": (
                "Set when this Address is mirrored from an EasyEcom "
                "Location. Address fields lock on the form — edit "
                "the Location, then re-save to push changes back."
            ),
        },
    ),
]
# Additional entries are appended by individual flow packets as their
# Custom Field patches are written. Keep this list sorted by gh#-issue
# for ease of audit.
