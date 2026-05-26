"""§8e Stage 2 — foundational country / state reference cache.

Pulls /getCountries (account-scoped) and /getStates?countryId=N for
each cached country, upserts into EasyEcom Country / EasyEcom State.
Pure reference data: no Sync Records, no entity Map rows, no flip-
dependent behaviour. The API Call rows are logged (the client layer
handles that automatically and tags is_foundational=1).

The cache is mode-irrelevant — onboarding and erpnext_mastered both
need the lookup tables to resolve state names to ids during push and
validate pincodes on pull.

Idempotent: re-running the discover refreshes existing rows in-place
(by id) and inserts any new rows EE has added. No row is deleted —
EE rarely retires ids (legacy ids like Daman & Diu id 35 persist
alongside the merged 3848), so we keep stale rows around. The resolver's
LARGEST-id-wins rule handles those gracefully.

Cache-shape decision (mirrors §8a Location + §8b Channel): one row per
EE entity in a dedicated DocType, upserted by id. NOT a JSON blob on
Settings, NOT a child table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    COUNTRIES_GET,
    STATES_GET,
)


# Top-level keys in the EE responses. Documented inline so a future EE
# shape drift surfaces at the constant rather than buried in code.
COUNTRIES_DATA_KEY: str = "countries"
STATES_DATA_KEY: str = "states"


@dataclass
class LookupsOutcome:
    """Per-call summary returned to the FDE button. Counts are factual:
    new = inserted this run; updated = refreshed in-place; skipped =
    payload row missing required id field."""

    countries_total: int = 0
    countries_new: int = 0
    countries_updated: int = 0
    countries_skipped: int = 0
    states_total: int = 0
    states_new: int = 0
    states_updated: int = 0
    states_skipped: int = 0
    countries_failed: list[dict[str, Any]] = field(default_factory=list)
    states_failed: list[dict[str, Any]] = field(default_factory=list)


def pull_countries_and_states(
    *, client: EasyEcomClient | None = None
) -> LookupsOutcome:
    """Discover-and-cache flow. Foundational §7.7.

    Two-phase:
      1) GET /getCountries → upsert EasyEcom Country rows.
      2) For each cached country, GET /getStates?countryId=N → upsert
         EasyEcom State rows.

    Phase 2 walks the FRESHLY-cached country list so a new country
    added by EE this run gets its states pulled the same call. (No
    point in waiting for the next scheduled run.)

    Returns LookupsOutcome — used by the whitelisted endpoint to build
    the FDE-facing summary.
    """
    if client is None:
        client = EasyEcomClient()

    outcome = LookupsOutcome()

    # === Phase 1: countries ===
    countries_resp = client.get(COUNTRIES_GET)
    countries_rows = (countries_resp or {}).get(COUNTRIES_DATA_KEY) or []
    if not isinstance(countries_rows, list):
        frappe.log_error(
            title="EasyEcom /getCountries: unexpected payload shape",
            message=(
                f"Expected dict with '{COUNTRIES_DATA_KEY}' list; got "
                f"{type(countries_resp).__name__} with '{COUNTRIES_DATA_KEY}'="
                f"{type(countries_rows).__name__}"
            ),
        )
        countries_rows = []

    outcome.countries_total = len(countries_rows)
    for row in countries_rows:
        try:
            result = _upsert_country(row)
            if result == "new":
                outcome.countries_new += 1
            elif result == "updated":
                outcome.countries_updated += 1
            elif result == "skipped":
                outcome.countries_skipped += 1
        except Exception as exc:  # noqa: BLE001 — fail one, continue
            outcome.countries_failed.append(
                {
                    "country_id": (row or {}).get("id"),
                    "country": (row or {}).get("country"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            frappe.log_error(
                title="EasyEcom Country upsert failed",
                message=(
                    f"{type(exc).__name__}: {exc}\nRow: {frappe.as_json(row)}"
                ),
            )

    frappe.db.commit()  # persist countries before fanning out to states

    # === Phase 2: states per country ===
    cached_countries = frappe.db.get_all(
        "EasyEcom Country",
        fields=["name", "country_id", "country_name"],
    )
    for country in cached_countries:
        try:
            states_resp = client.get(
                STATES_GET, params={"countryId": int(country.country_id)}
            )
        except Exception as exc:  # noqa: BLE001
            outcome.states_failed.append(
                {
                    "country_id": country.country_id,
                    "country": country.country_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            frappe.log_error(
                title=f"EasyEcom /getStates failed for country_id={country.country_id}",
                message=f"{type(exc).__name__}: {exc}",
            )
            continue

        states_rows = (states_resp or {}).get(STATES_DATA_KEY) or []
        if not isinstance(states_rows, list):
            frappe.log_error(
                title=f"EasyEcom /getStates: unexpected shape for country_id={country.country_id}",
                message=(
                    f"Expected dict with '{STATES_DATA_KEY}' list; got "
                    f"{type(states_resp).__name__}"
                ),
            )
            continue

        outcome.states_total += len(states_rows)
        for row in states_rows:
            try:
                result = _upsert_state(row, country_docname=country.name)
                if result == "new":
                    outcome.states_new += 1
                elif result == "updated":
                    outcome.states_updated += 1
                elif result == "skipped":
                    outcome.states_skipped += 1
            except Exception as exc:  # noqa: BLE001
                outcome.states_failed.append(
                    {
                        "country_id": country.country_id,
                        "state_id": (row or {}).get("id"),
                        "state_name": (row or {}).get("name"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                frappe.log_error(
                    title="EasyEcom State upsert failed",
                    message=(
                        f"{type(exc).__name__}: {exc}\nRow: {frappe.as_json(row)}"
                    ),
                )

    frappe.db.commit()
    return outcome


# ----- Per-row upsert helpers -----


def _upsert_country(row: dict[str, Any]) -> str:
    """Insert or refresh one EasyEcom Country row.

    Returns 'new' / 'updated' / 'skipped'. Skipped covers rows missing
    the required `id` field — they get a log_error but don't fail the
    whole batch.
    """
    country_id = (row or {}).get("id")
    if country_id is None:
        return "skipped"

    fields = {
        "country_id": int(country_id),
        "country_name": row.get("country") or "",
        "code_2": row.get("code_2") or "",
        "code_3": row.get("code_3") or "",
        "default_currency_code": row.get("default_currency_code") or "",
    }

    existing = frappe.db.get_value(
        "EasyEcom Country", {"country_id": int(country_id)}, "name"
    )
    if existing:
        # Refresh in-place. db.set_value bypasses validate so we don't
        # recurse into any controller that re-checks read-only fields.
        for k, v in fields.items():
            frappe.db.set_value(
                "EasyEcom Country", existing, k, v, update_modified=False
            )
        return "updated"

    doc = frappe.new_doc("EasyEcom Country")
    doc.update(fields)
    doc.insert(ignore_permissions=True)
    return "new"


def _upsert_state(row: dict[str, Any], *, country_docname: str) -> str:
    """Insert or refresh one EasyEcom State row.

    Returns 'new' / 'updated' / 'skipped'. The country link is a
    pre-resolved docname from the cached-countries iteration (no extra
    lookup per row).
    """
    state_id = (row or {}).get("id")
    if state_id is None:
        return "skipped"

    # Frappe Int columns are NOT NULL DEFAULT 0; db.set_value on the
    # UPDATE path rejects None. Store 0 to mean "no value" — the
    # resolver's validate_pincode_state treats start=0 as 'unknown_state'
    # (can't validate) and `end = zip_end_range or zip_start_range` so
    # 0 end falls back to start (single-prefix state). Both behaviours
    # are consistent with the EE null semantics.
    fields = {
        "state_id": int(state_id),
        "state_name": row.get("name") or "",
        "country": country_docname,
        "country_id": int(row.get("country_id") or 0),
        "is_union_territory": 1 if row.get("is_union_territory") else 0,
        "zip_start_range": int(row.get("zip_start_range") or 0),
        "zip_end_range": int(row.get("zip_end_range") or 0),
        "postal_code": row.get("postal_code") or "",
        "zone": row.get("Zone") or "",
    }

    existing = frappe.db.get_value(
        "EasyEcom State", {"state_id": int(state_id)}, "name"
    )
    if existing:
        for k, v in fields.items():
            frappe.db.set_value(
                "EasyEcom State", existing, k, v, update_modified=False
            )
        return "updated"

    doc = frappe.new_doc("EasyEcom State")
    doc.update(fields)
    doc.insert(ignore_permissions=True)
    return "new"
