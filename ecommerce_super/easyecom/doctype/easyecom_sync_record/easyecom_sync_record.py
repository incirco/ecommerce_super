"""EasyEcom Sync Record controller.

Entity-centric. One per (ERPNext doc, sync direction). Mutable in place
across retries — NOT append-only (contrast API Call and Webhook Event).

The composite UNIQUE constraint (company, entity_doctype, entity_name,
direction) is enforced at the DB level via an index added in
install.after_install. The controller's `find_or_create` is the canonical
upsert path so callers never construct duplicates by accident.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

# Valid status transitions (defensive — the integration owns transitions, but
# the controller validates them so any out-of-band UPDATE is caught).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "Pending": {"Running", "Cancelled", "AlreadySynced"},
    "Running": {"Success", "Failed", "Pending"},  # back to Pending on transient retry
    "Failed": {"Pending", "Cancelled"},  # FDE retry returns to Pending
    "Success": set(),  # terminal
    "Cancelled": set(),  # terminal
    "AlreadySynced": {"Pending"},  # FDE force-resync
}


class EasyEcomSyncRecord(Document):
    def validate(self) -> None:
        self._validate_entity_doctype_exists()
        self._validate_status_transition()
        self._validate_company_not_changed()

    def _validate_entity_doctype_exists(self) -> None:
        if self.entity_doctype and not frappe.db.exists("DocType", self.entity_doctype):
            frappe.throw(
                _("entity_doctype {0} is not a known DocType.").format(
                    self.entity_doctype
                )
            )

    def _validate_status_transition(self) -> None:
        if self.is_new() or not self.get_doc_before_save():
            return
        prior = self.get_doc_before_save()
        if not prior:
            return
        if prior.status == self.status:
            return
        allowed = ALLOWED_TRANSITIONS.get(prior.status, set())
        if self.status not in allowed:
            frappe.throw(
                _("Sync Record cannot transition {0} → {1} (allowed: {2}).").format(
                    prior.status,
                    self.status,
                    ", ".join(sorted(allowed)) or "(terminal)",
                )
            )

    def _validate_company_not_changed(self) -> None:
        if self.is_new() or not self.get_doc_before_save():
            return
        prior = self.get_doc_before_save()
        if prior and prior.company != self.company:
            frappe.throw(_("Sync Record Company cannot be changed once set."))


def find_existing(
    *,
    company: str,
    entity_doctype: str,
    entity_name: str,
    direction: str,
) -> EasyEcomSyncRecord | None:
    """Return the (single) Sync Record for this (entity, direction), or None."""
    name = frappe.db.get_value(
        "EasyEcom Sync Record",
        {
            "company": company,
            "entity_doctype": entity_doctype,
            "entity_name": entity_name,
            "direction": direction,
        },
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("EasyEcom Sync Record", name)


def upsert(
    *,
    company: str,
    entity_doctype: str,
    entity_name: str,
    entity_type: str,
    direction: str,
    correlation_id: str,
    idempotency_key: str,
    ee_location_key: str | None = None,
    parent_correlation_id: str | None = None,
    status: str = "Pending",
    **extra: Any,
) -> EasyEcomSyncRecord:
    """Get-or-create a Sync Record for this (entity, direction).

    Honours the composite-uniqueness invariant (§31.2.3 / §6.7). If an
    existing record is found, it is returned unchanged — the caller decides
    whether to mutate (e.g. bump attempts on retry) or no-op.
    """
    existing = find_existing(
        company=company,
        entity_doctype=entity_doctype,
        entity_name=entity_name,
        direction=direction,
    )
    if existing:
        return existing

    doc = frappe.new_doc("EasyEcom Sync Record")
    doc.update(
        {
            "company": company,
            "entity_doctype": entity_doctype,
            "entity_name": entity_name,
            "entity_type": entity_type,
            "direction": direction,
            "status": status,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "ee_location_key": ee_location_key,
            "parent_correlation_id": parent_correlation_id,
            **extra,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc
