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
        """FDE rewind action (§6.5.3). System Manager only — caller must
        check role before calling. This method writes the cursor and is
        expected to be paired with an EasyEcom Configuration Audit entry
        by the calling action."""
        self.db_set(
            {
                "cursor_value": to_value,
                "last_advanced_at": frappe.utils.now_datetime(),
                "last_advanced_by": "FDE Rewind",
            },
            update_modified=False,
            commit=False,
        )


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
