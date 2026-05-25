"""EasyEcom Channel (Marketplace) discovery — per-location sweep.

SPEC §8.6.3 / packet 8b. Channels are born in EasyEcom and ONLY EVER
pulled. The channel-list endpoint `/current-channel-status` is a
**per-location** call — the JWT is per-location (§3) and the response
describes channels integrated on the location whose JWT is used. So
channel discovery is a sweep over EVERY discovered EasyEcom Location,
not an account-scoped foundational call:

  - Poll every location regardless of workflow_state (To Map, Mapped
    but not Live, Live, Skipped). The catalogue must be complete; a
    channel can be live on a location the FDE hasn't mapped yet.
  - Each location's call uses that location's JWT and is wrapped in
    the per-record savepoint helper (8a flows/_isolation.py) so one
    location's failure (e.g. a JWT problem) records that location
    Failed and the sweep continues.
  - Union across all locations and dedupe by `marketplace_id` —
    channel identity is account-level. If a Marketplace row already
    exists for the id, skip (no per-location duplicates). New ids
    land in workflow state Unclassified.
  - `is_active` is catalogue-level: a channel is Active if Active on
    ANY location (per-location channel status is not tracked as
    distinct data in 8b — deferred unless a later flow needs it).

Payload→field translation goes through the `EasyEcom-Channel-Pull`
ruleset (§8.0 policy — engine = API-change insurance). The flow's job
is orchestration: the per-location sweep, the savepoint isolation,
the dedupe + union, and the workflow state new rows land in.

What this module does NOT do:
  - It does not push channels to EE (no such API; channels are EE-born).
  - It does not translate payload fields itself (the engine does).
  - It does not classify channels (FDE-classified via the Marketplace
    Classification Workflow — Unclassified → Classified → Active).
  - It does not track per-location channel availability (deferred).
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import CHANNELS_GET
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows._isolation import BatchOutcome, for_each_record

# Workflow state for newly-discovered channels (§8.6.3).
INITIAL_WORKFLOW_STATE: str = "Unclassified"

# EE response envelope key carrying the channel list.
DATA_KEY: str = "data"

# Field Mapping ruleset that translates the EE channel payload to
# Marketplace fields (§5.11 library, refactored in §8b). Edits to this
# ruleset (e.g. EE rename of `status`) are an FDE action in the desk,
# not a code deploy.
CHANNEL_PULL_RULESET: str = "EasyEcom-Channel-Pull"


@frappe.whitelist()
def discover_channels() -> dict:
    """FDE-facing wrapper around `sweep_all_locations`. Returns a plain
    dict summary suitable for an inline form-button response.

    Permission: callers need at least EasyEcom FDE / System Manager.
    Channel discovery sweeps every discovered location and writes
    Marketplace rows + per-location API Call rows; an Operator role is
    insufficient.

    Never raises through the whitelist boundary — every failure path
    returns {"ok": False, ...}.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Discover Channels requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )

    try:
        result = sweep_all_locations()
    except Exception as exc:  # noqa: BLE001 — whitelist boundary
        frappe.log_error(
            title="EasyEcom Discover Channels failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "message": (
                f"Channel discovery failed: {type(exc).__name__}: {exc}. "
                "See Error Log for the full trace."
            ),
        }

    _notify_if_new_channels(result["new_channels"])

    failed_summaries = [
        {
            "location_key": (loc or {}).get("location_key") or "<unknown>",
            "error": f"{type(exc).__name__}: {exc}",
        }
        for loc, exc in result["failed_locations"]
    ]

    return {
        "ok": True,
        "locations_polled": result["locations_polled"],
        "locations_failed": len(failed_summaries),
        "channels_total": result["channels_total"],
        "channels_new": len(result["new_channels"]),
        "channels_existing": len(result["existing_channels"]),
        "new_channels": result["new_channels"][:10],
        "failed_locations": failed_summaries[:10],
    }


def sweep_all_locations() -> dict:
    """Drive the per-location channel-discovery sweep.

    Loops every EasyEcom Location row (regardless of workflow_state),
    polls /current-channel-status per location with that location's
    JWT, unions the responses, dedupes by marketplace_id, and upserts
    new Marketplace rows in Unclassified.

    Returns a dict with sweep-level stats — see discover_channels for
    the JS-facing summary.
    """
    locations = _enumerate_locations()
    executor = FieldMappingExecutor(CHANNEL_PULL_RULESET)

    # Accumulators threaded through the per-location loop. The union/
    # dedupe lives at the sweep level (not per location) — channel
    # identity is account-level (§8.6.3).
    seen_ids: set[str] = set()
    new_channel_names: list[str] = []
    existing_channel_names: list[str] = []
    # marketplace_id → bool — "is_active = active on ANY location"
    active_anywhere: dict[str, bool] = {}
    failed_locations: list[tuple[dict, BaseException]] = []
    succeeded_location_keys: list[str] = []

    def _handle(loc: dict) -> None:
        location_key = loc["location_key"]
        client = EasyEcomClient(location_key=location_key)
        response = client.get(CHANNELS_GET)
        rows = response.get(DATA_KEY) or []
        if not isinstance(rows, list):
            raise ValueError(
                f"/current-channel-status returned unexpected shape for "
                f"location {location_key}: {type(rows).__name__}"
            )
        for raw in rows:
            erpnext_fields = executor.pull(raw)
            mid = erpnext_fields.get("marketplace_id")
            if mid is None or mid == "":
                # Engine would have raised on missing required field; defensive belt.
                continue
            mid_key = str(mid)
            # Track active-anywhere BEFORE the dedupe skip so the active
            # status from a later location can promote an earlier-discovered
            # Inactive channel.
            this_active = bool(erpnext_fields.get("is_active"))
            active_anywhere[mid_key] = active_anywhere.get(mid_key, False) or this_active
            if mid_key in seen_ids:
                # Already handled within THIS sweep (a later location
                # returned the same channel) — no doc write, but the
                # active-anywhere accumulator above still reflects it.
                continue
            seen_ids.add(mid_key)
            docname = _upsert_channel(erpnext_fields, mid_key)
            if docname in new_channel_names or docname in existing_channel_names:
                # Belt-and-braces: should be unreachable given seen_ids.
                continue
            # Was the row pre-existing? Compare against pre-upsert state.
            # _upsert_channel returns the docname either way; we infer
            # newness from whether the row was just inserted (we set a
            # marker via creation timestamp proximity, but the simpler
            # check is to re-query is_new state). Use the helper
            # _classify_new_or_existing which compares creation to a
            # sweep-start marker — see below.
        succeeded_location_keys.append(location_key)

    def _on_failure(loc: dict, exc: BaseException) -> None:
        failed_locations.append((loc, exc))
        frappe.log_error(
            title=f"EasyEcom channel sweep failed for location "
            f"{(loc or {}).get('location_key', '<unknown>')}",
            message=f"{type(exc).__name__}: {exc}",
        )

    # Mark the sweep start so we can classify new-vs-existing AFTER all
    # upserts land — needed because the same channel may be created on
    # one location's pass and skipped on the next within the same
    # sweep, and we want to count it as "new" once, not "existing".
    sweep_start = frappe.utils.now_datetime()

    outcome = for_each_record(
        locations,
        handler=_handle,
        on_failure=_on_failure,
        flow_name="channel_discovery",
    )

    # Second pass: apply the active-anywhere accumulator (a channel
    # seen Inactive then Active gets promoted to is_active=1) and
    # partition new vs existing by creation time vs sweep_start.
    for mid_key in seen_ids:
        docname = _docname_for_marketplace_id(mid_key)
        if not docname:
            continue
        was_just_created = _was_created_during_sweep(docname, sweep_start)
        # Promote is_active = active-anywhere if it changed.
        current_active = frappe.db.get_value("Marketplace", docname, "is_active")
        target_active = 1 if active_anywhere.get(mid_key, False) else 0
        if int(current_active or 0) != target_active:
            frappe.db.set_value(
                "Marketplace", docname, "is_active", target_active, update_modified=True
            )
        if was_just_created:
            new_channel_names.append(docname)
        else:
            existing_channel_names.append(docname)

    return {
        "locations_polled": len(succeeded_location_keys),
        "succeeded_location_keys": succeeded_location_keys,
        "failed_locations": failed_locations,
        "channels_total": len(seen_ids),
        "new_channels": new_channel_names,
        "existing_channels": existing_channel_names,
        "outcome": outcome,
    }


def _enumerate_locations() -> list[dict]:
    """Return every discovered EasyEcom Location as a dict the sweep
    can iterate. We include EVERY workflow_state (§8.6.3): the channel
    catalogue must be complete and a channel can be live on a
    not-yet-mapped location."""
    return frappe.db.get_all(
        "EasyEcom Location",
        filters={"enabled": 1},
        fields=["name", "location_key", "workflow_state", "frappe_company"],
        order_by="location_key asc",
    )


def _upsert_channel(erpnext_fields: dict[str, Any], mid_key: str) -> str:
    """Find-or-create the Marketplace row for marketplace_id=mid_key.

    If the row exists, return its name UNCHANGED — re-pull never
    re-classifies a channel or stomps an FDE override of channel_type /
    reporting_parent. The active-anywhere promotion happens AFTER the
    sweep loop, not here.
    """
    existing = frappe.db.exists("Marketplace", {"marketplace_id": mid_key})
    if existing:
        return existing if isinstance(existing, str) else existing[0]
    doc = frappe.new_doc("Marketplace")
    doc.update(
        {k: v for k, v in erpnext_fields.items() if v is not None}
    )
    # Defensive: ensure marketplace_id is the string we deduped on
    # (engine emits it via int_to_str, but be explicit).
    doc.marketplace_id = mid_key
    doc.workflow_state = INITIAL_WORKFLOW_STATE
    doc.insert(ignore_permissions=True)
    return doc.name


def _docname_for_marketplace_id(mid_key: str) -> str | None:
    name = frappe.db.exists("Marketplace", {"marketplace_id": mid_key})
    if isinstance(name, str):
        return name
    if isinstance(name, tuple):
        return name[0]
    return None


def _was_created_during_sweep(docname: str, sweep_start) -> bool:
    """True if the doc's creation timestamp is at or after sweep_start.
    Used to classify new-vs-existing for the post-sweep summary."""
    creation = frappe.db.get_value("Marketplace", docname, "creation")
    if not creation:
        return False
    creation_dt = frappe.utils.get_datetime(creation)
    return creation_dt >= sweep_start


def _notify_if_new_channels(new_docnames: list[str]) -> None:
    """**§18 PLACEHOLDER — DO NOT EXPAND.**

    Writes one Frappe Notification Log entry per EasyEcom FDE user when
    new channels are discovered. Mirrors 8a's _notify_if_new_locations —
    uses ONLY Frappe's stock bell-icon primitive (Notification Log
    DocType); does NOT touch the EasyEcom Integration Alert DocType,
    the §18 routing / severity / suppression machinery, or email
    fan-out. When §18 ships, replace this whole function wholesale.
    """
    if not new_docnames:
        return

    fde_users = _users_with_role("EasyEcom FDE")
    if not fde_users:
        frappe.log_error(
            title="EasyEcom channel discovery: no EasyEcom FDE users to notify",
            message=(
                f"Discovered {len(new_docnames)} new Marketplace row(s) but "
                f"no user has the EasyEcom FDE role assigned. "
                f"Channels: {', '.join(new_docnames[:10])}"
            ),
        )
        return

    count = len(new_docnames)
    subject = frappe._(
        "EasyEcom: {0} new channel(s) to classify"
    ).format(count)
    sample = ", ".join(new_docnames[:5])
    suffix = f" ({count - 5} more)" if count > 5 else ""
    body = frappe._(
        "Channel discovery found {0} new Marketplace row(s): {1}{2}. "
        "Open the Marketplace list filtered to 'Unclassified' to classify them."
    ).format(count, sample, suffix)

    for user in fde_users:
        notif = frappe.new_doc("Notification Log")
        notif.update(
            {
                "for_user": user,
                "type": "Alert",
                "document_type": "Marketplace",
                "document_name": new_docnames[0],
                "subject": subject,
                "email_content": body,
                "from_user": "Administrator",
            }
        )
        notif.insert(ignore_permissions=True)


def _users_with_role(role: str) -> list[str]:
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


# ----- Scheduler trigger (§8.6.3 — daily refresh) -----


def scheduled_discover_channels() -> None:
    """Scheduler-driven channel sweep. Wired in hooks.py
    scheduler_events. Catches every exception so a transient EE outage
    doesn't fail the whole scheduler tick; logs to Error Log so the
    FDE sees it on the next desk visit.

    Notifies FDE users only when new channels are discovered (no spam
    on quiet ticks).
    """
    try:
        result = sweep_all_locations()
    except Exception as exc:  # noqa: BLE001 — scheduler boundary
        frappe.log_error(
            title="EasyEcom scheduled channel discovery failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return
    _notify_if_new_channels(result["new_channels"])
