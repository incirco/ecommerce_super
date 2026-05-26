"""Country / state / pincode resolver helpers for §8e.

Pure functions over the cached `EasyEcom Country` and `EasyEcom State`
DocTypes (populated by the discover flow). Used by:

- Stage 3 pull: when an EE customer payload arrives with state NAME +
  pincode, validate_pincode_state() soft-flags dirty data
  (Created-Flagged) rather than rejecting.
- Stage 4 push: resolve_state(name, country_id) returns the int
  stateId for CreateCustomer's `billingStateId` / `dispatchStateId`.
  Unresolvable → returns None and the push handler flags
  (Flagged-Not-Created).

Design rules:
- **Never guess.** Case-insensitive, whitespace-stripped match only.
  Returns None on unresolvable input; the caller decides whether to
  flag (Stage 3 soft) or hold (Stage 4 hard) — the resolver doesn't
  manufacture a "closest match".
- **Deterministic dedupe.** When EE returns multiple state rows for
  the same name (legacy / merged admin units like Daman & Diu /
  Dadra & Nagar Haveli and Daman & Diu), the resolver picks the
  LARGEST state_id — the assumption is that EE's higher ids
  represent more recent / current administrative units (e.g.
  3848 > 35 for the merged Daman & Diu entity post-2020).
- **Soft pincode validation.** validate_pincode_state returns an
  enum-ish result (ok / mismatch / unknown_state) rather than
  throwing. Stage 3 maps mismatch to Created-Flagged; Stage 4 may
  warn but still push (EE's own validation will be the final word).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import frappe


# ----- Country resolution -----


@dataclass(frozen=True)
class CountryResolution:
    """The two pieces a §8.2 push needs from a country name:
    - `name`: canonical EE name (write payload's `country` field)
    - `country_id`: int — used to scope state lookups via getStates."""

    name: str
    country_id: int


def resolve_country(country_name: str | None) -> CountryResolution | None:
    """Look up a country by name (case-insensitive, whitespace-stripped).

    Returns None if the cache is empty (discover never ran) or the name
    doesn't match any cached row. The caller flags — this function
    never guesses or falls back to a default country.
    """
    if not country_name:
        return None
    needle = country_name.strip().lower()
    if not needle:
        return None

    # SQL LOWER() avoids loading all rows into Python just to lowercase.
    # The schema is read-only so a brief inline query is fine; no need
    # to push this into a stored procedure.
    row = frappe.db.sql(
        """SELECT country_id, country_name
           FROM `tabEasyEcom Country`
           WHERE LOWER(TRIM(country_name)) = %s
           LIMIT 1""",
        (needle,),
        as_dict=True,
    )
    if not row:
        return None
    return CountryResolution(
        name=row[0]["country_name"],
        country_id=int(row[0]["country_id"]),
    )


# ----- State resolution -----


def resolve_state(state_name: str | None, country_id: int) -> int | None:
    """Look up a state by (name, country_id). Returns the EE state_id
    (int) suitable for the push's `billingStateId` / `dispatchStateId`,
    or None when unresolvable.

    **Dupe handling (the EE legacy/merged case):** when more than one
    cached row matches the same name within a country, return the
    LARGEST state_id. Rationale: EE's higher ids correspond to more
    recent / current administrative units. Example: 'Daman & Diu'
    exists as id 35 (legacy UT) and 'Dadra & Nagar Haveli and Daman &
    Diu' as id 3848 (the merged 2020 UT). For exact name 'Daman & Diu'
    the resolver returns 35; for the merged name it returns 3848. The
    LARGEST rule applies when EE genuinely lists the same NAME twice
    (rare but observed historically).

    Case-insensitive, whitespace-stripped match.
    """
    if not state_name or country_id is None:
        return None
    needle = state_name.strip().lower()
    if not needle:
        return None

    row = frappe.db.sql(
        """SELECT state_id
           FROM `tabEasyEcom State`
           WHERE LOWER(TRIM(state_name)) = %s
             AND country_id = %s
           ORDER BY state_id DESC
           LIMIT 1""",
        (needle, int(country_id)),
        as_dict=True,
    )
    if not row:
        return None
    return int(row[0]["state_id"])


# ----- Pincode → state validation -----


PincodeMatch = Literal["ok", "mismatch", "unknown_state", "no_pincode"]


@dataclass(frozen=True)
class PincodeValidationResult:
    """The 4 outcomes a caller can route on:
    - 'ok': pincode falls in the state's prefix range
    - 'mismatch': pincode is outside the range — Stage 3 soft-flags
    - 'unknown_state': state_id not in the cache (discover stale)
    - 'no_pincode': pincode is empty / non-numeric
    """

    status: PincodeMatch
    state_id: int | None
    state_name: str | None
    expected_prefix_range: tuple[int, int] | None
    pincode_prefix: int | None


def validate_pincode_state(
    pincode: str | int | None, state_id: int | None
) -> PincodeValidationResult:
    """Soft-validate pincode falls in the cached state's prefix range.

    Returns a structured result rather than throwing — Stage 3 maps
    `mismatch` to Created-Flagged (the customer is still created; the
    FDE reviews). Stage 4 may emit a warning but still push (EE's
    server-side validation is the final word; we don't pre-block).

    EE's zip_start_range/zip_end_range are 2-3 digit prefixes (e.g.
    Karnataka 56-59 means any 6-digit pincode starting with 56, 57,
    58, or 59 is in Karnataka). When zip_end_range is null/zero, treat
    it as equal to zip_start_range (single-prefix state).

    Implementation detail: take the first N digits of the pincode
    where N is the digit-count of zip_start_range. This handles the
    mixed 2-digit (e.g. 56) and 3-digit (e.g. 790) prefix lengths
    EE returns. A pincode shorter than N digits is treated as
    no_pincode (insufficient data).
    """
    if state_id is None:
        return PincodeValidationResult(
            status="unknown_state",
            state_id=None,
            state_name=None,
            expected_prefix_range=None,
            pincode_prefix=None,
        )

    pin_str = str(pincode).strip() if pincode is not None else ""
    if not pin_str or not pin_str.isdigit():
        return PincodeValidationResult(
            status="no_pincode",
            state_id=state_id,
            state_name=None,
            expected_prefix_range=None,
            pincode_prefix=None,
        )

    state = frappe.db.get_value(
        "EasyEcom State",
        {"state_id": int(state_id)},
        ["state_name", "zip_start_range", "zip_end_range"],
        as_dict=True,
    )
    if not state:
        return PincodeValidationResult(
            status="unknown_state",
            state_id=state_id,
            state_name=None,
            expected_prefix_range=None,
            pincode_prefix=None,
        )

    start = state.get("zip_start_range")
    if start in (None, 0):
        # State has no pincode range in the cache — can't validate. Treat
        # as 'unknown_state' for the caller's purposes: we have no data
        # to compare against, so don't fabricate an 'ok' or 'mismatch'.
        return PincodeValidationResult(
            status="unknown_state",
            state_id=state_id,
            state_name=state.get("state_name"),
            expected_prefix_range=None,
            pincode_prefix=None,
        )

    end = state.get("zip_end_range") or start  # null → single-prefix state
    prefix_digits = len(str(int(start)))
    if len(pin_str) < prefix_digits:
        return PincodeValidationResult(
            status="no_pincode",
            state_id=state_id,
            state_name=state.get("state_name"),
            expected_prefix_range=(int(start), int(end)),
            pincode_prefix=None,
        )
    prefix = int(pin_str[:prefix_digits])

    status: PincodeMatch = "ok" if int(start) <= prefix <= int(end) else "mismatch"
    return PincodeValidationResult(
        status=status,
        state_id=state_id,
        state_name=state.get("state_name"),
        expected_prefix_range=(int(start), int(end)),
        pincode_prefix=prefix,
    )
