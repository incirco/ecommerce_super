"""EasyEcom Location discovery — pull-and-upsert from /getAllLocation.

SPEC §8.4.1. Locations are born in EasyEcom and ONLY EVER pulled into
ERPNext — there is no push. This flow drives one foundational API call,
then upserts one EasyEcom Location row per element of the returned
data[] array.

Foundational call (§7.7):
  - account-scoped: company=None, is_foundational=1 on the API Call row.
  - api_token in the response is credential-shaped — redacted in the
    logged payload (utils/redaction.py REDACTED_FIELDS includes
    'api_token') and NEVER persisted onto the EasyEcom Location row.

Upsert semantics (§8.4.1):
  - keyed on location_key (the natural EE identifier).
  - NEW rows land in workflow state "To Map" with is_wms_location derived
    from stockHandle (1 → 1). frappe_company / mapped_warehouse are left
    blank for the FDE to fill via the Map workflow transition.
  - EXISTING rows have their EE-supplied fields refreshed in place; the
    workflow state and the FDE-set fields (frappe_company,
    mapped_warehouse, gstin, is_primary) are LEFT UNTOUCHED. Re-pull
    never auto-advances or resets the workflow.

Per-record isolation (§7.1):
  - The inner loop runs through `for_each_record` so one bad location row
    cannot abort siblings. A failed upsert produces a `BatchOutcome.failed`
    entry; the caller (typically the queue worker or a scheduler) decides
    how to surface it. The whole batch never half-commits.

What this module does NOT do:
  - It does not push locations to EE (no such API; locations are
    EE-born).
  - It does not infer is_primary (FDE-set per §8.4.1).
  - It does not set gstin (FDE-set per §8.4.1).
  - It does not advance workflow state on existing rows.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import LOCATIONS_GET
from ecommerce_super.easyecom.flows._isolation import BatchOutcome, for_each_record

# Workflow state for newly-discovered locations (§8.4.1).
INITIAL_WORKFLOW_STATE: str = "To Map"

# EE response envelope key carrying the location list.
DATA_KEY: str = "data"


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
    directly without mocking the HTTP layer.
    """
    succeeded_names: list[str] = []

    def _handle(row: dict) -> None:
        name = _upsert_one(row)
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


def _upsert_one(row: dict) -> str:
    """Create or update a single EasyEcom Location from one /getAllLocation
    row. Returns the docname of the resulting row."""
    location_key = (row or {}).get("location_key")
    if not location_key:
        raise ValueError("Location row missing required 'location_key'.")

    existing_name = frappe.db.get_value(
        "EasyEcom Location", {"location_key": location_key}, "name"
    )

    if existing_name:
        return _update_existing(existing_name, row)
    return _create_new(row)


def _create_new(row: dict) -> str:
    """Insert a brand-new EasyEcom Location in workflow state To Map."""
    doc = frappe.new_doc("EasyEcom Location")
    doc.update(_ee_supplied_fields(row))
    doc.workflow_state = INITIAL_WORKFLOW_STATE
    # is_wms_location is derived ONLY on first create — re-pull doesn't
    # override an FDE override. Pre-set is_operational=0 explicitly so the
    # controller's derive step sees a fresh value.
    doc.is_wms_location = 1 if _stock_handle_truthy(row) else 0
    doc.is_operational = 0
    doc.insert(ignore_permissions=True)
    return doc.name


def _update_existing(name: str, row: dict) -> str:
    """Refresh EE-supplied fields in place; leave FDE-set fields and
    workflow_state untouched."""
    # db_set the EE-supplied fields directly — bypasses validate (we don't
    # want the workflow-state-derivation logic to fire on a re-pull) and
    # bypasses the Workflow constraint that normally guards workflow_state
    # writes. We're not touching workflow_state.
    updates = _ee_supplied_fields(row)
    if not updates:
        return name
    frappe.db.set_value(
        "EasyEcom Location",
        name,
        updates,
        update_modified=True,
    )
    return name


def _ee_supplied_fields(row: dict) -> dict[str, Any]:
    """Map one /getAllLocation row to the EasyEcom Location fields that
    EE owns. Keys NOT present here (frappe_company, mapped_warehouse,
    gstin, is_primary, is_operational, workflow_state) are FDE-set or
    workflow-derived and must never be written by this flow.

    `api_token` is intentionally NOT mapped. The redaction layer redacts
    it from the logged API Call row; this layer's job is to ensure it
    never lands on the EasyEcom Location row either.
    """
    address_type = row.get("address type") or {}
    billing = (address_type.get("billing_address") or {}) if isinstance(
        address_type, dict
    ) else {}
    pickup = (address_type.get("pickup_address") or {}) if isinstance(
        address_type, dict
    ) else {}

    return {
        "location_key": row.get("location_key"),
        "location_name": row.get("location_name") or row.get("location_key"),
        "ee_company_id": _as_str(row.get("company_id")),
        "is_store": 1 if row.get("is_store") else 0,
        "copy_master_from_primary": 1 if row.get("copy_master_from_primary") else 0,
        "city": row.get("city"),
        "state": row.get("state"),
        "country": row.get("country"),
        "pincode": _as_str(row.get("zip")),
        "address_line": row.get("address"),
        "billing_street": billing.get("street"),
        "billing_state": billing.get("state"),
        "billing_zipcode": _as_str(billing.get("zipcode")),
        "billing_country": billing.get("country"),
        "pickup_street": pickup.get("street"),
        "pickup_state": pickup.get("state"),
        "pickup_zipcode": _as_str(pickup.get("zipcode")),
        "pickup_country": pickup.get("country"),
    }


def _stock_handle_truthy(row: dict) -> bool:
    """EE sends stockHandle as an int (0 or 1). Be tolerant of strings/None."""
    val = row.get("stockHandle")
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip() not in ("", "0", "false", "False", "no")
    return bool(val)


def _as_str(value: Any) -> str | None:
    """EE sends numerics for some text-shaped fields (pincode, company_id).
    Coerce to string so the Data fields don't trip Frappe's type coercion."""
    if value is None:
        return None
    return str(value)


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
