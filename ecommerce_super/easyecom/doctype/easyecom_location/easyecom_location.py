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

# Per §8.4.1 — state-aware invariant on frappe_company:
#   - MAPPED_STATES (Mapped but not Live, Live) MUST have frappe_company set
#   - Skipped MUST have frappe_company empty
#   - To Map has NO CONSTRAINT — it is the mid-mapping state where the FDE
#     fills in frappe_company / mapped_warehouse in preparation for the Map
#     workflow transition. Forcing it empty here would clear the field on
#     plain Save and trap the FDE: clear → Map button vanishes (its
#     condition is `doc.frappe_company`) → cannot transition out of To Map.
SKIPPED_STATE: str = "Skipped"
MAPPED_STATES: frozenset[str] = frozenset({"Mapped but not Live", "Live"})


class EasyEcomLocation(Document):
    def validate(self) -> None:
        # Order matters:
        #  1. derive_is_operational reads workflow_state.
        #  2. clear_company_on_skipped_transition fires ONLY when entering
        #     Skipped from another state — so Mark Not Relevant from a
        #     mapped/live row cleanly drops the Company without forcing
        #     the FDE to clear the fields manually first.
        #  3. validate_company_matches_workflow_state enforces the
        #     §8.4.1 invariant against the post-clear, post-transition
        #     state.
        self._derive_is_operational_from_workflow_state()
        self._clear_company_on_skipped_transition()
        self._validate_exactly_one_primary()
        self._validate_company_matches_workflow_state()
        self._validate_mapped_warehouse_in_company()

    def _derive_is_operational_from_workflow_state(self) -> None:
        """is_operational is workflow-derived (§8.4.1). Live → 1; else → 0.

        Short-circuits when workflow_state is empty — that's the pre-workflow
        legacy path the back-fill patch operates on, where validate() is
        deliberately bypassed via db.set_value.
        """
        if not self.workflow_state:
            return
        self.is_operational = 1 if self.workflow_state == OPERATIONAL_WORKFLOW_STATE else 0

    def _clear_company_on_skipped_transition(self) -> None:
        """When the doc TRANSITIONS into Skipped (prior state != Skipped),
        clear frappe_company and mapped_warehouse. The §8.4.1 contract
        says Skipped rows carry no Company; Mark Not Relevant from a
        mapped/live row should drop the mapping cleanly without forcing
        the FDE to do a separate manual clear save first.

        Does NOT fire when:
          - workflow_state is empty (back-fill path)
          - workflow_state is not Skipped (the only auto-clear state;
            To Map is the deliberately-unconstrained mid-mapping state)
          - prior workflow_state was already Skipped (re-save within
            Skipped, not a transition — invariant below catches any
            disallowed Company that snuck in)
          - the doc is new (an inserter that wants to seed a row
            directly into Skipped with a phantom Company gets the
            invariant rejection instead of a silent strip)
        """
        if not self.workflow_state or self.workflow_state != SKIPPED_STATE:
            return
        if self.is_new():
            return
        prior = self.get_doc_before_save()
        if prior is None or prior.workflow_state == SKIPPED_STATE:
            return
        # Real transition INTO Skipped — clear the mapping fields.
        if self.frappe_company:
            self.frappe_company = None
        if self.mapped_warehouse:
            self.mapped_warehouse = None

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

    def _validate_company_matches_workflow_state(self) -> None:
        """State-aware invariant (§8.4.1):

          - workflow_state == Skipped                      → frappe_company empty
          - workflow_state in {Mapped but not Live, Live}  → frappe_company set
          - workflow_state == To Map                       → no constraint
            (this is the mid-mapping state — the FDE may set
            frappe_company in preparation for the Map transition; the
            transition's own condition `doc.frappe_company` is the gate
            into Mapped but not Live)

        Short-circuits when workflow_state is empty (back-fill exemption —
        the patch bypasses validate via db.set_value).

        This rule subsumes the older "is_operational requires
        frappe_company" check: Live is the only is_operational=1 state and
        Live requires Company.
        """
        if not self.workflow_state:
            return
        if self.workflow_state == SKIPPED_STATE:
            if self.frappe_company:
                # Clear hook only fires on transition INTO Skipped; a
                # plain save while in Skipped with a Company set comes
                # here. Refuse — the FDE should use Reconsider (→ To Map)
                # to re-engage with the location, not direct field writes.
                frappe.throw(
                    _(
                        "Workflow state Skipped must not carry a Frappe Company. "
                        "Use the workflow's Reconsider action to move the row "
                        "back to To Map before assigning a Company."
                    ),
                    title=_("Company Set on Skipped Location"),
                )
        elif self.workflow_state in MAPPED_STATES:
            if not self.frappe_company:
                frappe.throw(
                    _(
                        "Workflow state {0} requires a Frappe Company. "
                        "The Map transition assigns it; you cannot land in "
                        "{0} with the field empty."
                    ).format(self.workflow_state),
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
