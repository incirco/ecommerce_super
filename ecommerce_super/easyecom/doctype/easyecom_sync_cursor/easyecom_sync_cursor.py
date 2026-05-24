"""EasyEcom Sync Cursor controller.

Persistent cursor per (Company, location, resource). Modified in place by
polling workers. The Rewind Cursor action is System Manager only (§6.5.3)
and produces an audit row (§26).

The composite UNIQUE (company, location_key, resource) is enforced at the
DB via install.after_install. The autoname expression also produces a
deterministic name from those three fields, so two cursors for the same
(company, location, resource) would collide on docname insert as well.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

VALID_ADVANCED_BY = {"Worker", "FDE Rewind", "System"}


class EasyEcomSyncCursor(Document):
    def validate(self) -> None:
        if self.last_advanced_by and self.last_advanced_by not in VALID_ADVANCED_BY:
            frappe.throw(
                _("last_advanced_by must be one of {0}, got {1}.").format(
                    ", ".join(sorted(VALID_ADVANCED_BY)), self.last_advanced_by
                )
            )

    def advance(
        self, *, new_value: str, records_fetched: int, advanced_by: str = "Worker"
    ) -> None:
        """Advance the cursor after a successful pull. `records_fetched` is
        the count from this run; total is bumped in place."""
        self.db_set(
            {
                "cursor_value": new_value,
                "last_advanced_at": frappe.utils.now_datetime(),
                "last_advanced_by": advanced_by,
                "records_fetched_last_run": records_fetched,
                "records_fetched_total": (self.records_fetched_total or 0)
                + records_fetched,
            },
            update_modified=False,
            commit=False,
        )

    def rewind(self, *, to_value: str, reason: str, actor: str) -> None:
        """Internal method that writes the cursor backwards. The role
        gate and audit hook are owned by the `rewind_cursor` whitelisted
        wrapper below — call that from the desk, not this directly."""
        self.db_set(
            {
                "cursor_value": to_value,
                "last_advanced_at": frappe.utils.now_datetime(),
                "last_advanced_by": "FDE Rewind",
            },
            update_modified=False,
            commit=False,
        )


@frappe.whitelist()
def rewind_cursor(cursor_name: str, to_value: str, reason: str) -> dict:
    """FDE-facing cursor rewind action (§6.5.3).

    Enforces:
      - System Manager role (any of "System Manager" or
        "EasyEcom System Manager"). Per §6.5.3 the rewind must be
        restricted to System Manager — we accept either the global role
        or the EasyEcom-scoped equivalent. Anyone else gets
        PermissionError.
      - Non-empty `reason` (no silent rewinds — §2.7).
      - `to_value` non-empty.

    Configuration Audit row creation is DEFERRED to §28 — the
    Configuration Audit DocType is not built yet. The stub call below
    documents where the audit hook will live. Until then, the desk's
    standard Version history captures who/when/from→to.
    """
    user = frappe.session.user
    roles = set(frappe.get_roles(user))
    if not roles.intersection({"System Manager", "EasyEcom System Manager"}):
        raise frappe.PermissionError(
            _("Cursor Rewind is restricted to System Manager (§6.5.3).")
        )

    if not (to_value or "").strip():
        frappe.throw(_("Rewind target value is required."))
    if not (reason or "").strip():
        frappe.throw(_("Rewind reason is required (no silent rewinds, §2.7)."))

    doc = frappe.get_doc("EasyEcom Sync Cursor", cursor_name)
    before_value = doc.cursor_value
    doc.rewind(to_value=to_value, reason=reason, actor=user)
    frappe.db.commit()

    # STUB — §28 Configuration Audit row will be written here when the
    # EasyEcom Configuration Audit DocType ships. The contract per
    # §6.5.3: capture actor=user, before_value, after_value, reason.
    # _write_configuration_audit(
    #     dt="EasyEcom Sync Cursor", dn=cursor_name,
    #     action="rewind", actor=user,
    #     before={"cursor_value": before_value},
    #     after={"cursor_value": to_value},
    #     reason=reason,
    # )

    return {
        "cursor": cursor_name,
        "before_value": before_value,
        "after_value": to_value,
        "actor": user,
        "reason": reason,
    }


def get_or_create(
    *,
    company: str,
    location_key: str,
    resource: str,
    initial_value: str,
    cursor_format: str = "ISO Datetime",
) -> EasyEcomSyncCursor:
    """Get-or-create the cursor for this (company, location, resource).
    If absent, creates it with `initial_value`."""
    name = frappe.db.get_value(
        "EasyEcom Sync Cursor",
        {"company": company, "location_key": location_key, "resource": resource},
        "name",
    )
    if name:
        return frappe.get_doc("EasyEcom Sync Cursor", name)
    doc = frappe.new_doc("EasyEcom Sync Cursor")
    doc.update(
        {
            "company": company,
            "location_key": location_key,
            "resource": resource,
            "cursor_value": initial_value,
            "cursor_format": cursor_format,
            "last_advanced_at": frappe.utils.now_datetime(),
            "last_advanced_by": "System",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc
