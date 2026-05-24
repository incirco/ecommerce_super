"""EasyEcom Field Mapping Version — append-only snapshot (§5.12, §31.2.9).

One row per save of the parent EasyEcom Field Mapping. Created by the
parent's `on_update` hook (added in Phase G). The controller's job here
is to enforce append-only: writes/deletes after insert are not allowed
for any role except System Manager (which we permit only for emergency
correction with an audit trail elsewhere).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EasyEcomFieldMappingVersion(Document):
    def autoname(self) -> None:
        """ECS-FMV-<parent_mapping>-v<NNNN> where NNNN is zero-padded.

        Padding keeps the lexical sort consistent with the numeric sort
        even past 9 versions, which the §5.12 retention model demands
        (snapshots accumulate indefinitely)."""
        if not (self.parent_mapping and self.version is not None):
            frappe.throw("parent_mapping and version are required.")
        self.name = f"ECS-FMV-{self.parent_mapping}-v{int(self.version):04d}"

    def on_update(self) -> None:
        if self.is_new():
            return
        if "System Manager" in frappe.get_roles(frappe.session.user):
            return
        frappe.throw(
            _(
                "EasyEcom Field Mapping Version is append-only; "
                "edits are forbidden after creation."
            )
        )

    def on_trash(self) -> None:
        if "System Manager" in frappe.get_roles(frappe.session.user):
            return
        frappe.throw(
            _(
                "EasyEcom Field Mapping Version is append-only; "
                "deletes are forbidden."
            )
        )
