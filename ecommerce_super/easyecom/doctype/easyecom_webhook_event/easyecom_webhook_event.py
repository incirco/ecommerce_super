"""EasyEcom Webhook Event controller.

Inbound-centric, **append-only** (§4.1.2 / §31.7.4). One row per inbound
webhook. The composite UNIQUE (company, event_type, ee_event_id) is the
dedup key — enforced at the DB via index added in install.after_install.

The webhook receiver (`easyecom/api/webhook.py`, built in Phase H) is the
only place that should insert rows here. On duplicate insert (caught by
the UNIQUE), the receiver returns the existing row's name and returns 200
to EasyEcom so they stop retrying — see §6.4 and §3.8.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EasyEcomWebhookEvent(Document):
    def on_update(self) -> None:
        """Append-only enforcement (defence in depth — see API Call controller)."""
        if self.is_new():
            return
        prior = self.get_doc_before_save()
        if not prior:
            return
        # The only fields the receiver may update after insert are the
        # processing_* fields, spawned_queue_job, and downstream_documents.
        # All other fields are frozen at insert.
        for fieldname in (
            "company",
            "event_type",
            "ee_event_id",
            "received_at",
            "correlation_id",
            "auth_header_used",
            "token_verified",
            "allowed_ip_check",
            "source_ip",
            "http_method",
            "raw_payload",
            "payload_hash",
        ):
            if self.get(fieldname) != prior.get(fieldname):
                frappe.throw(
                    _(
                        "EasyEcom Webhook Event field {0} is append-only — cannot change after insert."
                    ).format(fieldname),
                    title=_("Append-Only Violation"),
                )


def find_duplicate(*, company: str, event_type: str, ee_event_id: str) -> str | None:
    """Return the name of an existing Webhook Event for this (company,
    event_type, ee_event_id), or None. Webhook receiver uses this to
    short-circuit before insert, but the DB UNIQUE is the authoritative
    enforcement."""
    return frappe.db.get_value(
        "EasyEcom Webhook Event",
        {"company": company, "event_type": event_type, "ee_event_id": ee_event_id},
        "name",
    )
