"""gh#200 repair — undo the shadow damage from the broken
`create_easyecom_integration_user._ensure_role_permissions()` and
`expand_integration_role_perms` patches.

Problem (reported by @garv999 on live16version.frappe.cloud):
  The prior implementation inserted Custom DocPerm rows directly via
  `frappe.new_doc("Custom DocPerm")`. Frappe's rule is: **if ANY
  Custom DocPerm exists for a DocType, ALL standard DocPerms are
  ignored**. On doctypes with no prior Custom DocPerm (Territory,
  Customer Group, Print Format, and every core master in _PERMISSIONS),
  our raw insert became the ONLY perm row, wiping every other role's
  access after the next Permission Manager resolution.

Repair strategy — per doctype in _PERMISSIONS:

  1. If NO Custom DocPerm rows exist besides ours (Case A —
     freshly-shadowed): delete our row so the doctype falls back to
     standard perms. This restores every other role instantly.
  2. If OTHER Custom DocPerm rows exist alongside ours (Case B — site
     had customizations before we came): the standard perms were
     already inactive on this doctype before our patch. Deleting our
     row leaves the pre-existing customizations intact — same state
     as before our patch touched this doctype.
  3. Then call the FIXED `_ensure_role_permissions()`, which uses
     `setup_custom_perms(doctype)` to copy standard DocPerms into
     Custom DocPerms (preserving all other roles) BEFORE inserting
     ours.

Idempotent: safe to re-run. The fixed `_ensure_role_permissions()` is
itself idempotent, and the deletion step is a no-op the second time
because there's nothing more to delete.

Live symptom this fixes on MMPL / any deployed site:
  Territory, Customer Group, Print Format visibility restored for
  Sales User, Territory Manager, System Manager, etc.
"""
from __future__ import annotations

import frappe


def execute() -> None:
    from ecommerce_super.patches.v0_1.create_easyecom_integration_user import (
        ROLE_NAME,
        _PERMISSIONS,
        _ensure_role_permissions,
    )

    for doctype in _PERMISSIONS:
        if not frappe.db.exists("DocType", doctype):
            continue

        # Delete every Custom DocPerm row for OUR role on this doctype.
        # We don't touch rows for any other role — those are either
        # pre-existing site customizations (Case B) or, on
        # correctly-repaired doctypes after this patch runs, copies
        # of the standard set that the FIXED patch just installed.
        frappe.db.delete("Custom DocPerm", {
            "parent": doctype,
            "role": ROLE_NAME,
        })

        # Clear meta cache so subsequent permission lookups (and the
        # fixed _ensure_role_permissions call below) see the fresh
        # post-delete state. Without this, setup_custom_perms may
        # skip the copy step believing Custom perms still exist.
        frappe.clear_cache(doctype=doctype)

    # Commit deletions before re-adding — a mid-run crash then leaves
    # the site in "standard-perms-restored, our-perms-missing" state,
    # which is functionally correct for other users (they can work) and
    # only breaks our own integration flow (better than the reverse).
    frappe.db.commit()

    # Now re-grant our role's permissions using the FIXED path, which
    # calls setup_custom_perms first — preserving every other role.
    _ensure_role_permissions()
    frappe.db.commit()
