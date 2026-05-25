"""EasyEcom Location discovery — pull-and-upsert from /getAllLocation.

SPEC §8.4.1. Locations are born in EasyEcom and ONLY EVER pulled into
ERPNext — there is no push. This flow drives one foundational API call,
then upserts one EasyEcom Location row per element of the returned
data[] array.

Payload → field translation is delegated to the EasyEcom-Location-Pull
**Field Mapping ruleset** (fixture, §5.11 library). Insurance against
EasyEcom changing their API: a renamed or restructured field is fixed
by an FDE editing the ruleset in the desk, not a code deploy. The
stockHandle → is_wms_location derivation is itself a ruleset transform
(conditional_constant). This flow's job is orchestration; the engine
does the mapping.

Foundational call (§7.7):
  - account-scoped: company=None, is_foundational=1 on the API Call row.
  - api_token in the response is credential-shaped — redacted in the
    logged payload (utils/redaction.py REDACTED_FIELDS includes
    'api_token') and NEVER mapped into the ruleset's output, so it
    never lands on the EasyEcom Location row either.

Upsert semantics (§8.4.1):
  - keyed on location_key (the natural EE identifier).
  - NEW rows land in workflow state "To Map" with is_wms_location set
    from the ruleset's derivation. frappe_company / mapped_warehouse are
    left blank for the FDE to fill via the Map workflow transition.
  - EXISTING rows have their EE-supplied fields refreshed in place; the
    workflow state and the FDE-set fields (frappe_company,
    mapped_warehouse, gstin, is_primary) — AND is_wms_location, which
    the FDE may have overridden — are LEFT UNTOUCHED. Re-pull never
    auto-advances or resets the workflow.

Per-record isolation (§7.1):
  - The inner loop runs through `for_each_record` so one bad location row
    cannot abort siblings. A failed upsert produces a `BatchOutcome.failed`
    entry; the caller (typically the queue worker or a scheduler) decides
    how to surface it. The whole batch never half-commits.

What this module does NOT do:
  - It does not push locations to EE (no such API; locations are
    EE-born).
  - It does not translate payload fields itself — the Field Mapping
    engine does. The ruleset is the contract; this flow is its driver.
  - It does not infer is_primary (FDE-set per §8.4.1).
  - It does not set gstin (FDE-set per §8.4.1).
  - It does not advance workflow state on existing rows.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import LOCATIONS_GET
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows._isolation import BatchOutcome, for_each_record

# Workflow state for newly-discovered locations (§8.4.1).
INITIAL_WORKFLOW_STATE: str = "To Map"

# EE response envelope key carrying the location list.
DATA_KEY: str = "data"

# Field Mapping ruleset that translates the EE payload to EasyEcom
# Location fields (§5.11 library, refactored in §8a). Edits to this
# ruleset (e.g. EE rename of a payload field) are an FDE action in the
# desk, not a code deploy.
LOCATION_PULL_RULESET: str = "EasyEcom-Location-Pull"

# Fields the FDE owns after first discovery — re-pull must not overwrite
# them. Everything else from the ruleset output is EE-supplied and gets
# refreshed in place.
_FDE_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "is_wms_location",  # FDE may override the ruleset's stockHandle derivation
    }
)


@frappe.whitelist()
def discover_locations() -> dict:
    """FDE-facing wrapper around `pull_locations`. Returns a plain dict
    summary suitable for an inline form-button response.

    Permission: callers need at least EasyEcom FDE / System Manager
    privilege. The discovery pull is account-scoped and writes a
    foundational API Call row plus EasyEcom Location rows; an Operator
    role isn't sufficient.

    Never raises through the whitelist — every failure path returns
    {"ok": False, ...} so the JS handler can render a clean message
    rather than a stack trace.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Discover Locations requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )

    try:
        outcome = pull_locations()
    except Exception as exc:  # noqa: BLE001 — the whitelist boundary
        frappe.log_error(
            title="EasyEcom Discover Locations failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "message": (
                f"Discovery pull failed: {type(exc).__name__}: {exc}. "
                "See Error Log for the full trace."
            ),
        }

    new_names, updated_names = _split_new_vs_updated(outcome.succeeded)
    failed_summaries = [
        {
            "location_key": (row or {}).get("location_key") or "<unknown>",
            "error": f"{type(exc).__name__}: {exc}",
        }
        for row, exc in outcome.failed
    ]

    _notify_if_new_locations(new_names)

    return {
        "ok": True,
        "total": outcome.total,
        "new_count": len(new_names),
        "updated_count": len(updated_names),
        "failed_count": outcome.failed_count,
        "new_locations": new_names[:10],  # cap for inline display
        "updated_locations": updated_names[:10],
        "failed_locations": failed_summaries[:10],
    }


def pull_locations(*, client: EasyEcomClient | None = None) -> BatchOutcome:
    """Fetch /getAllLocation and upsert one EasyEcom Location per row.

    Args:
        client: optional pre-built EasyEcomClient (for tests / replay).
            When None, a default client is constructed; that path is the
            production caller (scheduler / FDE button).

    Returns:
        BatchOutcome — succeeded carries the upserted docname strings,
        failed carries (raw_payload_dict, exception) pairs.
    """
    if client is None:
        client = EasyEcomClient()

    response = client.get(LOCATIONS_GET)
    rows = response.get(DATA_KEY) or []
    if not isinstance(rows, list):
        # EE shape drift — log and treat as empty so the caller sees a
        # clean BatchOutcome rather than a TypeError mid-loop.
        frappe.log_error(
            title="EasyEcom /getAllLocation: unexpected payload shape",
            message=(
                f"Expected dict with '{DATA_KEY}' list; got "
                f"{type(response).__name__} with '{DATA_KEY}'="
                f"{type(rows).__name__}"
            ),
        )
        return BatchOutcome()

    return upsert_locations_from_payload(rows)


def upsert_locations_from_payload(rows: list[dict]) -> BatchOutcome:
    """Drive the per-row upsert loop with savepoint isolation.

    Separated from `pull_locations` so tests can feed a fixture payload
    directly without mocking the HTTP layer. The Field Mapping executor
    is instantiated ONCE for the whole batch — its compilation step
    queries the DB for the ruleset; doing it per row would be wasted
    work.
    """
    executor = FieldMappingExecutor(LOCATION_PULL_RULESET)
    succeeded_names: list[str] = []

    def _handle(row: dict) -> None:
        name = _upsert_one(row, executor)
        succeeded_names.append(name)

    def _on_failure(row: dict, exc: BaseException) -> None:
        # Surface visibly — no silent drops (§2.7). We log here rather
        # than create a Sync Record because this is a foundational call
        # (§7.7) and Sync Records are for entity-sync work.
        location_key = (row or {}).get("location_key") or "<unknown>"
        frappe.log_error(
            title=f"EasyEcom Location upsert failed: {location_key}",
            message=f"{type(exc).__name__}: {exc}\nRow: {frappe.as_json(row)}",
        )

    outcome = for_each_record(
        rows,
        handler=_handle,
        on_failure=_on_failure,
        flow_name="location_discovery",
    )
    # Replace the opaque per-record refs in `outcome.succeeded` with the
    # actual docnames so the caller can report what got created/updated.
    outcome.succeeded = succeeded_names
    return outcome


def _upsert_one(row: dict, executor: FieldMappingExecutor) -> str:
    """Translate one /getAllLocation row through the Field Mapping
    engine and upsert the EasyEcom Location row it describes.

    The engine produces a flat dict of ERPNext fields per the
    EasyEcom-Location-Pull ruleset. This function never inspects the raw
    payload for field values — that would re-introduce the hardcoded-
    mapper risk the refactor removed.
    """
    erpnext_fields = executor.pull(row)
    location_key = erpnext_fields.get("location_key")
    if not location_key:
        # The ruleset declares location_key as required, so the engine
        # would have raised FieldMappingMissingRequiredError before us.
        # Defensive belt-and-braces.
        raise ValueError(
            "Field Mapping engine returned no location_key for /getAllLocation row "
            "(check EasyEcom-Location-Pull ruleset)."
        )

    existing_name = frappe.db.get_value(
        "EasyEcom Location", {"location_key": location_key}, "name"
    )

    if existing_name:
        return _update_existing(existing_name, erpnext_fields)
    return _create_new(erpnext_fields)


def _create_new(erpnext_fields: dict[str, Any]) -> str:
    """Insert a brand-new EasyEcom Location in workflow state To Map.

    The ruleset already supplied is_wms_location (derived from
    stockHandle). Workflow state and is_operational are flow-owned and
    set explicitly here — the ruleset does not (and should not) touch
    them.
    """
    doc = frappe.new_doc("EasyEcom Location")
    doc.update(erpnext_fields)
    doc.workflow_state = INITIAL_WORKFLOW_STATE
    # is_operational is workflow-derived in the controller; pre-set 0 so
    # the controller's derive step sees a fresh value on To Map insert.
    doc.is_operational = 0
    doc.insert(ignore_permissions=True)
    return doc.name


def _update_existing(name: str, erpnext_fields: dict[str, Any]) -> str:
    """Refresh EE-supplied fields in place; leave FDE-owned fields and
    workflow_state untouched.

    Two filters on the updates dict:

    1. Drop _FDE_OWNED_FIELDS (e.g. is_wms_location) — the ruleset may
       produce these (from the stockHandle derivation), but the FDE is
       allowed to override them on the form; re-pull must not stomp.

    2. Drop fields where the ruleset emitted None — meaning the source
       payload didn't carry the field. Treating None as "absent" rather
       than "explicitly NULL" achieves two things:
         - NOT NULL columns (e.g. Check fields like is_store, default
           '0') don't get a NULL write via raw set_value (which would
           raise IntegrityError; Frappe's field defaults only fire on
           insert, not on set_value).
         - An EE payload that momentarily drops an address field does
           not clobber the existing value with NULL. Additive refresh
           is the right semantics for the §8a contract — we update
           what EE supplies, leave alone what it doesn't.

    db.set_value bypasses validate (we don't want the workflow-state-
    derivation logic to fire on a re-pull) and bypasses the Workflow
    constraint that normally guards workflow_state writes. We're not
    touching workflow_state either way.
    """
    updates = {
        k: v
        for k, v in erpnext_fields.items()
        if k not in _FDE_OWNED_FIELDS and v is not None
    }
    if not updates:
        return name
    frappe.db.set_value(
        "EasyEcom Location",
        name,
        updates,
        update_modified=True,
    )
    return name


# ----- Trigger surface: scheduler + notification -----


def scheduled_discover_locations() -> None:
    """Scheduler-driven discovery run (§8.4.3 daily cadence).

    Wired in hooks.py scheduler_events. Catches every exception so a
    transient EE outage doesn't fail the whole scheduler tick; logs to
    Error Log so the FDE sees it on the next desk visit.

    Returns nothing — this is a scheduler hook, not a programmatic API.
    Writes a Notification Log entry only when new locations are
    discovered (no spam on quiet ticks).
    """
    try:
        outcome = pull_locations()
    except Exception as exc:  # noqa: BLE001 — scheduler boundary
        frappe.log_error(
            title="EasyEcom scheduled discovery failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return

    new_names, _updated = _split_new_vs_updated(outcome.succeeded)
    _notify_if_new_locations(new_names)


def _split_new_vs_updated(docnames: list[str]) -> tuple[list[str], list[str]]:
    """For a list of docnames returned by `pull_locations`, partition into
    rows in 'To Map' (just discovered) vs everything else (re-pull update).

    Heuristic: a brand-new row from `_create_new` lands in workflow_state
    'To Map' with no FDE touches. A re-pull only updates EE-supplied
    fields, never workflow_state. So workflow_state=='To Map' AND no
    frappe_company == 'just discovered.' Anything else is an update.

    This is a heuristic — a To Map row that was created in a PREVIOUS
    run and is still unmapped will look 'new' on this run too. That's
    acceptable for the notification's purpose ('here are the rows
    waiting for you'). It is NOT used for any operational decision.
    """
    if not docnames:
        return [], []
    rows = frappe.db.get_all(
        "EasyEcom Location",
        filters={"name": ("in", list(docnames))},
        fields=["name", "workflow_state", "frappe_company"],
    )
    new: list[str] = []
    updated: list[str] = []
    for row in rows:
        if row.workflow_state == "To Map" and not row.frappe_company:
            new.append(row.name)
        else:
            updated.append(row.name)
    return new, updated


def _notify_if_new_locations(new_docnames: list[str]) -> None:
    """**§18 PLACEHOLDER — DO NOT EXPAND.**

    Writes one Frappe Notification Log entry per EasyEcom FDE user when
    new locations appear. Uses ONLY Frappe's stock bell-icon primitive
    (Notification Log DocType) — does NOT touch the EasyEcom Integration
    Alert DocType, the §18 routing / severity / suppression machinery,
    or email fan-out. None of that exists yet, and we are NOT
    anticipating its shape here.

    When §18 ships, this whole function should be replaced wholesale by
    a call into the alerts framework (likely an `EasyEcomIntegrationAlert`
    constructor with severity="info", financial_impact=0, routing per
    Company). At that point, delete this function and its call sites in
    `discover_locations` and `scheduled_discover_locations`.

    Until then: quiet on empty input (no notification ticks); one row
    per FDE user when there's something to surface; no email.
    """
    if not new_docnames:
        return

    fde_users = _users_with_role("EasyEcom FDE")
    if not fde_users:
        # No FDEs yet (pre-onboarding); log silently rather than dropping.
        frappe.log_error(
            title="EasyEcom discovery: no EasyEcom FDE users to notify",
            message=(
                f"Discovered {len(new_docnames)} new EasyEcom Location row(s) "
                f"but no user has the EasyEcom FDE role assigned. "
                f"Locations: {', '.join(new_docnames[:10])}"
            ),
        )
        return

    count = len(new_docnames)
    subject = frappe._(
        "EasyEcom: {0} new location(s) to map"
    ).format(count)
    sample = ", ".join(new_docnames[:5])
    suffix = f" ({count - 5} more)" if count > 5 else ""
    body = frappe._(
        "Discovery pull found {0} new EasyEcom Location row(s): {1}{2}. "
        "Open the EasyEcom Location list filtered to 'To Map' to map them."
    ).format(count, sample, suffix)

    for user in fde_users:
        notif = frappe.new_doc("Notification Log")
        notif.update(
            {
                "for_user": user,
                "type": "Alert",
                "document_type": "EasyEcom Location",
                # Linking to the first new docname gives the bell a
                # clickable target; the body lists the full sample.
                "document_name": new_docnames[0],
                "subject": subject,
                "email_content": body,
                "from_user": "Administrator",
            }
        )
        notif.insert(ignore_permissions=True)


def _users_with_role(role: str) -> list[str]:
    """Return the enabled, non-Guest users carrying `role`."""
    return frappe.db.sql_list(
        """SELECT DISTINCT hr.parent
           FROM `tabHas Role` hr
           JOIN `tabUser` u ON u.name = hr.parent
           WHERE hr.role = %s
             AND hr.parenttype = 'User'
             AND u.enabled = 1
             AND u.name NOT IN ('Guest', 'Administrator')""",
        (role,),
    )
