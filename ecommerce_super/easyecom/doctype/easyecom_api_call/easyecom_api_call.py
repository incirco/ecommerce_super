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
        # Three valid shapes (the §31.2.4 binary plus the §8b per-location
        # extension):
        #
        #   1. is_foundational=1, company blank
        #      → token / /getAllLocation / connection test (§7.7). Always
        #        account-scoped.
        #
        #   2. is_foundational=0, company set
        #      → entity-sync call against a mapped location whose Frappe
        #        Company is known. The §31.2.4 default.
        #
        #   3. is_foundational=0, company blank, location_key set
        #      → per-location call against a location that does NOT yet
        #        resolve to a Frappe Company (To Map / Skipped). The §8b
        #        channel-discovery sweep is the first user of this shape:
        #        it polls EVERY location regardless of workflow_state,
        #        because the channel catalogue must be complete (a channel
        #        can be live on a not-yet-mapped location). Per-Company
        #        filtering of these rows is by location_key, not company.
        #
        # Anything else is a bug.
        if self.is_foundational and self.company:
            frappe.throw(
                _(
                    "Foundational API Calls (token/location/test — §7.7) must leave Company blank."
                )
            )
        if not self.is_foundational and not self.company and not self.location_key:
            frappe.throw(
                _(
                    "Non-foundational API Calls require either a Company "
                    "(entity-sync against a mapped location) or a Location Key "
                    "(per-location call against an as-yet-unmapped location, "
                    "e.g. §8b channel discovery)."
                )
            )

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
