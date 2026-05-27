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
    def before_insert(self) -> None:
        """Strip auto-populated user-default Company on foundational rows.

        Frappe v15/v16 auto-populates empty Link-to-Company fields from
        `frappe.defaults.get_user_default("Company")` during the
        default-resolution step of `insert()`. That step runs BEFORE
        `validate()`, which means the foundational-call path in
        `auth.acquire_jwt()` (and `client.log_api_call(...,
        company=None, is_foundational=True, ...)`) finds the row
        re-populated with the user's default Company by the time
        validate runs — tripping the §7.7 invariant.

        On a single-Company dev site there's no user default so this
        never fires; on a multi-Company FrappeCloud site (or any site
        where the FDE's User has a Company default set), every Test
        Connection click would throw "Foundational API Calls must
        leave Company blank."

        Fix: when is_foundational=1 we explicitly null out company
        AFTER Frappe's default-fill but BEFORE validate. Caller code
        already knows the truth (it passed company=None); this hook
        just guards the user-defaults injection point Frappe inserts.
        """
        if self.is_foundational and self.company:
            self.company = None

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
