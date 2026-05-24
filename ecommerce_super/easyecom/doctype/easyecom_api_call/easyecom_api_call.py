"""EasyEcom API Call controller.

Call-centric, **append-only** (§4.1.2 / §31.7.4). One row per outbound HTTP
call. The DocPerms grant create+read but NOT write or delete; the
controller's `on_update` is a defence-in-depth check that blocks any
post-insert mutation that slipped past the permission layer.

The redaction layer in `easyecom/utils/redaction.py` is called by the
EasyEcomClient (`client/client.py`) before the API Call row is inserted.
This controller assumes the values it receives are already redacted; it
does not redact again because that would mask logging bugs upstream.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EasyEcomAPICall(Document):
    def validate(self) -> None:
        # Foundational calls leave company blank and bear is_foundational=1.
        # Entity-sync calls require company. Enforce the rule.
        if self.is_foundational and self.company:
            frappe.throw(
                _(
                    "Foundational API Calls (token/location/test — §7.7) must leave Company blank."
                )
            )
        if not self.is_foundational and not self.company:
            frappe.throw(_("Non-foundational API Calls require a Company."))

    def on_update(self) -> None:
        """Defence in depth: append-only is also enforced via has_permission
        (§31.7.4 / Phase H). If we reach on_update past insert, something
        bypassed the permission layer — refuse loudly."""
        if not self.is_new() and self.has_value_changed_since_insert():
            frappe.throw(
                _(
                    "EasyEcom API Call is append-only — values may not change after insert."
                ),
                title=_("Append-Only Violation"),
            )

    def has_value_changed_since_insert(self) -> bool:
        """True if any field differs from the version in the database. Used
        only as a paranoia check against bypass paths; the permission layer
        is the primary enforcement."""
        prior = self.get_doc_before_save()
        if not prior:
            return False
        for fieldname in (
            "status",
            "endpoint",
            "request_url",
            "request_payload_hash",
            "response_status_code",
            "response_payload_hash",
            "company",
            "easyecom_account",
        ):
            if self.get(fieldname) != prior.get(fieldname):
                return True
        return False
