"""EasyEcom Location controller.

Per-location record (SPEC §3.4, §8.4.1). Carries primary/operational flags,
Company resolution, warehouse mapping, JWT cache, per-location pull cursors,
and the EE-supplied location attributes from /getAllLocation.

Workflow-derived state (§8.4.1):
  - workflow_state owns the lifecycle (To Map → Mapped but not Live → Live;
    branch Skipped). The EasyEcom Location Workflow fixture defines the
    transitions; the FDE drives them via the standard form action buttons.
  - is_operational is DERIVED from workflow_state and is read-only on the
    form. Live → is_operational=1; everything else → 0. The FDE no longer
    toggles it directly. This avoids two competing notions of "on."

Validation rules (§3.4 / §8.4.1):
  - Exactly one location has is_primary = 1 (account-wide; single Account
    per deployment so this is enforced site-wide).
  - frappe_company mandatory iff is_operational; must be empty otherwise.
  - frappe_company is deliberately non-unique — many locations may resolve
    to the same Company.
  - mapped_warehouse, where set, must belong to frappe_company.
  - A location with neither is_primary nor is_operational is inert (no
    validation error — this is a valid steady state, §3.1.3).

JWT cache (§3.7.2):
  - jwt_token is Long Text. The controller encrypts on set, decrypts only
    when EasyEcomClient builds a request. Plaintext is never returned to
    any caller other than EasyEcomClient.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.password import decrypt, encrypt

# Default JWT validity in days (EasyEcom JWTs are valid for 90 days per §3.6).
JWT_VALIDITY_DAYS: int = 90
# Renewal margin: refresh once a JWT reaches this age (§3.6 — day 85 of 90).
JWT_RENEW_AT_AGE_DAYS: int = 85

# Per §8.4.1 — only the Live workflow state implies operational. Every other
# state (including Skipped) keeps is_operational = 0.
OPERATIONAL_WORKFLOW_STATE: str = "Live"


class EasyEcomLocation(Document):
    def validate(self) -> None:
        self._derive_is_operational_from_workflow_state()
        self._validate_exactly_one_primary()
        self._validate_operational_company_rule()
        self._validate_mapped_warehouse_in_company()

    def _derive_is_operational_from_workflow_state(self) -> None:
        """is_operational is workflow-derived (§8.4.1). Live → 1; else → 0.

        Run before _validate_operational_company_rule so that rule sees the
        derived value, not whatever stale boolean the doc carried in.
        """
        # Pre-workflow rows (before the fixture installs) have no state — leave
        # is_operational alone; the back-fill patch will set state explicitly.
        if not self.workflow_state:
            return
        self.is_operational = 1 if self.workflow_state == OPERATIONAL_WORKFLOW_STATE else 0

    def _validate_exactly_one_primary(self) -> None:
        if not self.is_primary:
            return
        existing = frappe.db.get_all(
            "EasyEcom Location",
            filters={"is_primary": 1, "name": ["!=", self.name or ""]},
            pluck="name",
        )
        if existing:
            frappe.throw(
                _(
                    "Exactly one EasyEcom Location may be marked Primary. {0} is already primary."
                ).format(existing[0]),
                title=_("Multiple Primary Locations"),
            )

    def _validate_operational_company_rule(self) -> None:
        """is_operational=1 requires frappe_company. The §8.4.1 workflow
        has an intermediate 'Mapped but not Live' state where the FDE has
        already assigned frappe_company (so the Map transition was legal)
        but Go Live hasn't fired yet — that's is_operational=0 with
        frappe_company SET. We allow that combination; only the missing-
        company-when-Live case is invalid (the workflow's Map condition
        already prevents it, this is defence-in-depth)."""
        if self.is_operational and not self.frappe_company:
            frappe.throw(
                _("Frappe Company is required when Operational is checked."),
                title=_("Company Required"),
            )

    def _validate_mapped_warehouse_in_company(self) -> None:
        if not self.mapped_warehouse or not self.frappe_company:
            return
        wh_company = frappe.db.get_value("Warehouse", self.mapped_warehouse, "company")
        if wh_company and wh_company != self.frappe_company:
            frappe.throw(
                _(
                    "Mapped Warehouse {0} belongs to Company {1}, but this Location resolves to {2}."
                ).format(self.mapped_warehouse, wh_company, self.frappe_company)
            )

    # ----- JWT cache: controller-managed encryption (§3.7.2) -----

    def set_jwt(
        self, plaintext_token: str, validity_days: int = JWT_VALIDITY_DAYS
    ) -> None:
        """Encrypt and persist a freshly-acquired JWT for this location.

        Caller is the EasyEcomClient (`client/auth.py`) — no other code path
        should call this. Sets jwt_token (ciphertext), jwt_acquired_at,
        jwt_expires_at and commits the change to the DB via db_set to skip
        validate() and the surrounding transaction.
        """
        now = frappe.utils.now_datetime()
        ciphertext = encrypt(plaintext_token)
        self.db_set(
            {
                "jwt_token": ciphertext,
                "jwt_acquired_at": now,
                "jwt_expires_at": now + timedelta(days=validity_days),
            },
            update_modified=False,
            commit=False,
        )

    def get_jwt_plaintext(self) -> str | None:
        """Return the decrypted JWT, or None if no JWT is cached.

        Caller is EasyEcomClient — never log or return this value upstream.
        """
        if not self.jwt_token:
            return None
        try:
            return decrypt(self.jwt_token)
        except Exception:
            # Corrupted ciphertext (encryption_key rotated, manual edit, etc.).
            # Treat as "no cached JWT" so the client re-authenticates cleanly.
            frappe.log_error(
                title="EasyEcom Location JWT decrypt failed",
                message=f"Location {self.name}: cached JWT cannot be decrypted; will re-authenticate.",
            )
            return None

    def clear_jwt(self) -> None:
        """Invalidate the cached JWT. Used on 401 re-auth and FDE actions."""
        self.db_set(
            {
                "jwt_token": None,
                "jwt_acquired_at": None,
                "jwt_expires_at": None,
            },
            update_modified=False,
            commit=False,
        )

    def jwt_age_days(self) -> int | None:
        """Return the age in days of the cached JWT, or None if no JWT."""
        if not self.jwt_acquired_at:
            return None
        acquired = (
            self.jwt_acquired_at
            if isinstance(self.jwt_acquired_at, datetime)
            else datetime.fromisoformat(str(self.jwt_acquired_at))
        )
        return (frappe.utils.now_datetime() - acquired).days

    def jwt_needs_renewal(self) -> bool:
        """True when the JWT has reached the renewal age (§3.6 — day 85)."""
        age = self.jwt_age_days()
        return age is not None and age >= JWT_RENEW_AT_AGE_DAYS


def get_primary_location() -> "EasyEcomLocation | None":
    """Return the (single) Primary location, or None if none configured."""
    name = frappe.db.get_value("EasyEcom Location", {"is_primary": 1}, "name")
    if not name:
        return None
    return frappe.get_doc("EasyEcom Location", name)


def resolve_company(location_key: str) -> str | None:
    """Resolve a location_key to its Frappe Company. Returns None for
    non-operational or unknown locations (which is correct, not an error
    — see §3.1.3)."""
    name = frappe.db.get_value(
        "EasyEcom Location", {"location_key": location_key}, "name"
    )
    if not name:
        return None
    return frappe.db.get_value("EasyEcom Location", name, "frappe_company")


def get_aging_locations(age_days: int = JWT_RENEW_AT_AGE_DAYS) -> list[dict[str, Any]]:
    """Return enabled operational locations whose JWT has reached `age_days`.
    Used by the day-85 renewal scheduler hook (§3.6, scheduler_events)."""
    cutoff = frappe.utils.now_datetime() - timedelta(days=age_days)
    return frappe.db.get_all(
        "EasyEcom Location",
        filters={
            "enabled": 1,
            "jwt_acquired_at": ["<=", cutoff],
        },
        fields=["name", "location_key", "jwt_acquired_at"],
    )
