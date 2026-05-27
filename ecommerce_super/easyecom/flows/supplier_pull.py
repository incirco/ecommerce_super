"""§8f Stage 3 — EE→EN Supplier pull.

Foundational at the API Call layer (account-wide; no Company tag),
entity-sync at the flow layer (one Supplier Map row + one Sync Record
per supplier). Mirrors §8e Customer Pull's shape with three §8.3-
specific deviations:

  1. **Two-identifier split.** EE's vendor_c_id is the READ key (the
     join key — unique on the Supplier Map); EE's vendor_code (=
     vendor_id) is the WRITE key (used by /wms/CreateVendor and
     /wms/UpdateVendor in Stage 4). Both are captured on every pull
     so the §9/§10 PO/GRN flows can resolve via the Supplier Map
     without re-reading EE.
  2. **Country-aware GST gating.** Indian supplier → India Compliance
     validates GSTIN (PAN auto-extracted from gstin[2:12]); bad/blank
     → IC throws → catch → FNC. Foreign supplier (country resolved
     via Stage 2's cached countries) → gst_category='Overseas'
     BEFORE IC validate, so GSTIN/PAN are optional and IC accepts.
  3. **Lifecycle pull-side.** Vendors HAVE an `active` flag (unlike
     customers in §8e). active=0 → Supplier.disabled=1; active=1 →
     .disabled=0. EE-side deactivation propagates to ERPNext on pull.

Other §8f-specific points:
  - **Address envelope may be empty-array.** EE returns `address: {
    billing: {...}, dispatch: [] }` mixed shapes. The flow pre-
    flattens before invoking the engine — empty-array sub-fields are
    absent in the flattened payload and Permissive policy lets them
    skip; the flow treats absence as no-address (no row inserted).
  - **Cursor-paginated.** getVendors returns `nextUrl` (confirmed
    live — 2 pages, 30 vendors, cursor IS used). The flow walks all
    pages and persists the cursor page-by-page on EasyEcom Account.
    Mirrors §8d Item Pull's cursor pattern.
  - **Delta watermark.** getVendors supports created_after /
    updated_after / updated_before. Stage 3 stores
    supplier_pull_last_updated_at on the Account on clean-walk
    completion; Stage 6 wires the cron to use it.
  - **Map-row-only matching.** No natural-key auto-match (same dirty-
    data reasoning as customer pull — EE's tax_identification_number
    and vendor_name both have observed duplicates in real data).

Mode handling: Stage 3 pull is the onboarding-mode behaviour
(bidirectional, supervised). Post-flip (erpnext_mastered) the pull
becomes drift-detection only — Stage 5 will branch on
supplier_master_mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import VENDORS_GET
from ecommerce_super.easyecom.customer.state_resolver import resolve_country
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)
from ecommerce_super.easyecom.flows._isolation import for_each_record
from ecommerce_super.easyecom.flows._supplier_sync_records import (
    STATUS_DISCREPANCY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_supplier_pull_sync_record,
)


SUPPLIER_PULL_RULESET: str = "EasyEcom-Supplier-Pull"
RESPONSE_DATA_KEY: str = "data"
RESPONSE_NEXT_URL_KEY: str = "nextUrl"

# Status enum values — kept consistent with the Supplier Map JSON.
STATUS_MAPPED: str = "Mapped"
STATUS_DRIFT: str = "Drift"
STATUS_FLAGGED_NOT_CREATED: str = "Flagged-Not-Created"
STATUS_DISABLED: str = "Disabled"

MODE_ONBOARDING: str = "onboarding"
MODE_ERPNEXT_MASTERED: str = "erpnext_mastered"

# Stage 5 — drift detection comparable-field lists. The post-flip
# pull compares the EE-translated payload against the existing ERPNext
# Supplier + linked Addresses on these fields; any diff lands as a
# row in EasyEcom Supplier Map.drift_fields.
#
# Internal ids (ee_vendor_c_id / ee_vendor_id, vendor_code) are
# deliberately NOT compared — they're identity-management state, not
# user-visible content. A change there would be a remap operation,
# not drift in the §8.3 sense.
SUPPLIER_DRIFT_COMPARABLE_SUPPLIER_FIELDS: tuple[str, ...] = (
    "supplier_name",
    "gstin",
    "pan",
    "email_id",
    "mobile_no",
    "default_currency",
)

# (drift label, payload_key from ruleset, ERPNext Address field).
# "billing." / "dispatch." prefix on the label is what the FDE sees
# in the drift_fields child table — same shape as §8e Customer drift.
SUPPLIER_DRIFT_COMPARABLE_ADDRESS_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("billing.street", "billing_street", "address_line1"),
    ("billing.city", "billing_city", "city"),
    ("billing.pincode", "billing_zipcode", "pincode"),
    ("billing.state", "billing_state_name", "state"),
    ("billing.country", "billing_country_name", "country"),
    ("dispatch.street", "dispatch_street", "address_line1"),
    ("dispatch.city", "dispatch_city", "city"),
    ("dispatch.pincode", "dispatch_zipcode", "pincode"),
    ("dispatch.state", "dispatch_state_name", "state"),
    ("dispatch.country", "dispatch_country_name", "country"),
)

# Page-walk safety cap — well above 30-vendor sandbox but high
# enough that a real client with thousands of suppliers still
# completes. A bug producing infinite cursor loops will hit this
# and stop instead of running unbounded.
MAX_PAGES: int = 200

# Internal flag the (Stage 4) push hook checks to suppress the
# auto-push-on-save callback for a Supplier we just pulled (no
# point bouncing the same row right back to EE).
PING_PONG_FLAG: str = "easyecom_supplier_pull_in_flight"


SupplierStatus = Literal[
    "Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"
]


@dataclass
class SupplierOutcome:
    """Per-supplier result returned from the per-record handler.

    `supplier_docname` is None for FNC outcomes (no Supplier created).
    `country_kind` is 'india' / 'foreign' / 'unknown' — surfaced for
    test assertions and for the FDE-facing summary.
    """

    ee_vendor_c_id: str
    ee_vendor_id: str | None
    supplier_docname: str | None
    status: SupplierStatus
    operation: Literal["created", "skipped", "flagged", "disabled"]
    country_kind: Literal["india", "foreign", "unknown"]
    flag_reasons: list[str] = field(default_factory=list)


@dataclass
class PullOutcome:
    """Aggregate result of a discover_suppliers run."""

    total: int = 0
    pages_walked: int = 0
    created: int = 0
    skipped: int = 0
    disabled: int = 0
    created_flagged: int = 0
    flagged_not_created: int = 0
    drift_count: int = 0
    failed: int = 0
    final_cursor: str | None = None  # nextUrl after the last walked page
    outcomes: list[SupplierOutcome] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


# ----- Top-level cursor-walking entrypoint -----


def pull_suppliers(
    *,
    client: EasyEcomClient | None = None,
    account: str | None = None,
    start_fresh: bool = True,
    max_pages: int = MAX_PAGES,
    updated_after: str | None = None,
) -> PullOutcome:
    """Fetch /wms/V2/getVendors page by page and process each row.

    `start_fresh=True` clears the persisted cursor and pulls from the
    top; `False` resumes from the persisted cursor (for restarting
    after a partial failure without re-processing the early pages).

    `updated_after`: optional YYYY-MM-DD date string. When set, the
    FIRST page request includes the `updated_after` query param so
    EE returns only vendors updated on or after that date. Verified
    live 2026-05-27 — EE accepts the param; a future date returns
    `{"data": "No Data Found"}` (handled by the non-list-shape guard
    below). After the first page, the cursor's nextUrl already carries
    the filter; we don't re-attach the param. Mutually exclusive with
    `start_fresh=False` resume — a resume continues from the saved
    cursor which already encodes whatever filter the original call
    used.

    Returns PullOutcome. The flow ALWAYS writes Sync Records as it
    goes (the cursor walk is per-record-isolated via for_each_record);
    a partial walk's per-supplier results survive even if a later
    page raises.
    """
    if client is None:
        client = EasyEcomClient()

    account_doc = _enabled_account() if account is None else frappe.get_doc(
        "EasyEcom Account", account
    )

    if start_fresh:
        _clear_cursor(account_doc)
        next_endpoint: str | None = VENDORS_GET
    else:
        next_endpoint = account_doc.supplier_pull_cursor or VENDORS_GET

    account_mode = _resolve_supplier_master_mode(account_doc)
    executor = FieldMappingExecutor(SUPPLIER_PULL_RULESET)
    aggregate = PullOutcome()
    page_idx = 0

    while next_endpoint and page_idx < max_pages:
        page_idx += 1
        # Attach updated_after on the FIRST request only — the cursor
        # carries it forward on subsequent pages, and double-passing
        # could conflict.
        if page_idx == 1 and updated_after and start_fresh:
            response = client.get(
                next_endpoint, params={"updated_after": updated_after}
            )
        else:
            response = client.get(next_endpoint)
        rows = (response or {}).get(RESPONSE_DATA_KEY) or []
        if not isinstance(rows, list):
            frappe.log_error(
                title="EasyEcom /getVendors: unexpected shape",
                message=(
                    f"Expected dict with '{RESPONSE_DATA_KEY}' list; got "
                    f"{type(response).__name__} with '{RESPONSE_DATA_KEY}'="
                    f"{type(rows).__name__}"
                ),
            )
            rows = []

        _process_page(
            rows,
            executor=executor,
            account_mode=account_mode,
            aggregate=aggregate,
        )

        next_endpoint = (response or {}).get(RESPONSE_NEXT_URL_KEY) or None
        # Persist cursor so a crash here resumes from this page next time.
        _advance_cursor(account_doc, next_endpoint)
        aggregate.pages_walked = page_idx
        aggregate.final_cursor = next_endpoint

    if next_endpoint:
        # Hit MAX_PAGES — partial walk, leave high-water alone so the
        # next run still covers what we missed.
        frappe.log_error(
            title="EasyEcom supplier_pull: MAX_PAGES hit",
            message=(
                f"Walked {page_idx} pages, cursor still non-empty. "
                f"Resuming from saved cursor on next run."
            ),
        )
    else:
        # Clean full walk — bump the high-water mark for delta pulls.
        _set_clean_completion(account_doc, total=aggregate.total)

    return aggregate


# ----- Page-level loop with savepoint isolation -----


def _process_page(
    rows: list[dict],
    *,
    executor: FieldMappingExecutor,
    account_mode: str,
    aggregate: PullOutcome,
) -> None:
    """Run process_one_supplier over a single page's rows under
    savepoint isolation. Updates `aggregate` in place."""
    aggregate.total += len(rows)
    frappe.flags.__setattr__(PING_PONG_FLAG, True)

    def _handle(row: dict) -> None:
        outcome = process_one_supplier(
            row, executor=executor, account_mode=account_mode
        )
        aggregate.outcomes.append(outcome)
        if outcome.status == "Mapped" and outcome.operation == "created":
            aggregate.created += 1
        elif outcome.operation == "skipped":
            aggregate.skipped += 1
        elif outcome.operation == "disabled":
            aggregate.disabled += 1
        if outcome.status == "Created-Flagged":
            aggregate.created_flagged += 1
        elif outcome.status == "Flagged-Not-Created":
            aggregate.flagged_not_created += 1
        elif outcome.status == "Drift":
            aggregate.drift_count += 1

    def _on_failure(row: dict, exc: BaseException) -> None:
        ee_vendor_c_id = str((row or {}).get("vendor_c_id") or "<unknown>")
        aggregate.failed += 1
        aggregate.failures.append(
            {
                "ee_vendor_c_id": ee_vendor_c_id,
                "vendor_name": (row or {}).get("vendor_name"),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        frappe.log_error(
            title=f"EasyEcom Supplier pull failed: vendor_c_id={ee_vendor_c_id}",
            message=(
                f"{type(exc).__name__}: {exc}\nRow: {frappe.as_json(row)}"
            ),
        )

    try:
        for_each_record(
            rows,
            handler=_handle,
            on_failure=_on_failure,
            flow_name="supplier_pull",
        )
    finally:
        frappe.flags.__setattr__(PING_PONG_FLAG, False)


# ----- Per-record processing -----


def process_one_supplier(
    row: dict,
    *,
    executor: FieldMappingExecutor,
    account_mode: str = MODE_ONBOARDING,
) -> SupplierOutcome:
    """Translate one /wms/V2/getVendors row through the engine and
    create the ERPNext Supplier + Billing/Dispatch Addresses + Map.

    Three branching axes:
      1. account_mode: onboarding → accept-and-create; erpnext_mastered
         → drift-detection only (Stage 5, branches before any mutation).
      2. Existing map row: yes → reuse + status-refresh (lifecycle);
         no → attempt create.
      3. Country-kind: india → IC validates GSTIN+PAN; foreign → set
         gst_category=Overseas BEFORE IC validate.

    The address envelope is pre-flattened by `_flatten_vendor_row`
    before invoking the engine; the engine sees flat keys like
    `billing_city`, `dispatch_address_line`, etc. — same convention
    as the §8e Customer-Pull ruleset.
    """
    flattened = _flatten_vendor_row(row)
    erpnext_fields = executor.pull(flattened)
    ee_vendor_c_id = (erpnext_fields.get("ee_vendor_c_id") or "").strip()
    if not ee_vendor_c_id:
        raise ValueError(
            "Field Mapping engine returned no ee_vendor_c_id for /wms/V2/getVendors "
            "row (check EasyEcom-Supplier-Pull ruleset)."
        )
    ee_vendor_id = (erpnext_fields.get("ee_vendor_id") or "").strip() or None
    active_raw = erpnext_fields.get("active")
    is_active = _truthy(active_raw)

    # === Stage 5 phase gate — branch BEFORE GSTIN gating / address
    # creation so post-flip pulls NEVER mutate ERPNext. The drift
    # detector reads the EE payload, compares against the existing
    # ERPNext Supplier + Addresses, and writes a Drift Map row +
    # Discrepancy Sync Record on divergence. No Supplier rows are
    # created or modified in this branch. ===
    if account_mode == MODE_ERPNEXT_MASTERED:
        return _detect_drift_one_supplier(
            row=row,
            erpnext_fields=erpnext_fields,
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=ee_vendor_id,
        )

    # === 1) Map-row match (no natural-key auto-match) ===
    existing_map = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"ee_vendor_c_id": ee_vendor_c_id},
        ["name", "erpnext_name", "status", "ee_vendor_id"],
        as_dict=True,
    )
    if existing_map:
        # Refresh status from lifecycle: active=0 → Disabled; otherwise
        # leave whatever status the map row currently holds.
        new_status = _refresh_existing_row(
            map_name=existing_map.name,
            supplier_docname=existing_map.erpnext_name,
            current_status=existing_map.status,
            is_active=is_active,
            new_ee_vendor_id=ee_vendor_id,
        )
        _write_pull_sync_record(
            entity_name=existing_map.erpnext_name,
            ee_vendor_c_id=ee_vendor_c_id,
            status=STATUS_SUCCESS,
            last_error=None,
        )
        return SupplierOutcome(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=ee_vendor_id or existing_map.ee_vendor_id,
            supplier_docname=existing_map.erpnext_name,
            status=new_status,
            operation="disabled" if new_status == STATUS_DISABLED else "skipped",
            country_kind=_classify_country(
                erpnext_fields.get("billing_country_name")
                or erpnext_fields.get("dispatch_country_name")
            ),
            flag_reasons=[],
        )

    # === 2) Country-aware GST gating ===
    billing_country = (
        erpnext_fields.get("billing_country_name")
        or erpnext_fields.get("dispatch_country_name")
        or ""
    ).strip()
    country_kind = _classify_country(billing_country)
    erpnext_country = _resolve_erpnext_country(billing_country)

    gstin_raw = (erpnext_fields.get("gstin") or "").strip().upper()
    pan_raw = (erpnext_fields.get("pan") or "").strip().upper()
    if country_kind == "foreign":
        # Foreign supplier — set gst_category=Overseas BEFORE save so IC's
        # validate_party returns 'Overseas' from fetch_or_guess_gst_category
        # rather than calling guess_gst_category and tripping on a missing
        # Indian GSTIN. GSTIN + PAN are optional for foreign suppliers.
        gst_category: str | None = "Overseas"
        gstin = ""  # foreign suppliers don't carry an Indian GSTIN
        pan = ""  # foreign suppliers don't carry an Indian PAN
    else:
        # Indian (or unknown country — treat as India by default for
        # backwards-compat with the customer pull's URP handling).
        # Empty GSTIN → "Unregistered" gst_category so IC validate
        # doesn't try to extract a PAN from nothing.
        if not gstin_raw or gstin_raw == "URP":
            gst_category = "Unregistered"
            gstin = ""
        else:
            gst_category = None  # IC derives from GSTIN
            gstin = gstin_raw
        # For Indian + unregistered, PAN may still come through from EE;
        # only set it when GSTIN is empty (otherwise IC auto-extracts).
        pan = pan_raw if not gstin else ""

    # === 3) Atomic insert: Supplier + 2 Addresses. Any IC throw aborts
    #        the whole transaction and FNCs. ===
    inserted_for_cleanup: list[tuple[str, str]] = []
    try:
        supplier = _create_supplier(
            ee_vendor_c_id=ee_vendor_c_id,
            erpnext_fields=erpnext_fields,
            gstin=gstin,
            pan=pan,
            gst_category=gst_category,
            country=erpnext_country,
            is_active=is_active,
        )
        inserted_for_cleanup.append(("Supplier", supplier.name))

        billing_name = _create_address_strict(
            supplier_docname=supplier.name,
            address_type="Billing",
            street=erpnext_fields.get("billing_street") or "",
            city=erpnext_fields.get("billing_city") or "",
            zipcode=erpnext_fields.get("billing_zipcode") or "",
            state_name=erpnext_fields.get("billing_state_name") or "",
            country_name=(
                _resolve_erpnext_country(
                    erpnext_fields.get("billing_country_name") or ""
                )
                or erpnext_country
            ),
            gstin=gstin or None,
        )
        if billing_name:
            inserted_for_cleanup.append(("Address", billing_name))

        dispatch_name = _create_address_strict(
            supplier_docname=supplier.name,
            address_type="Shipping",
            street=erpnext_fields.get("dispatch_street") or "",
            city=erpnext_fields.get("dispatch_city") or "",
            zipcode=erpnext_fields.get("dispatch_zipcode") or "",
            state_name=erpnext_fields.get("dispatch_state_name") or "",
            country_name=(
                _resolve_erpnext_country(
                    erpnext_fields.get("dispatch_country_name") or ""
                )
                or erpnext_country
            ),
            gstin=gstin or None,
        )
        if dispatch_name:
            inserted_for_cleanup.append(("Address", dispatch_name))

    except frappe.ValidationError as exc:
        # Tax-relevant IC throw → roll back partial inserts and FNC.
        for doctype, name in reversed(inserted_for_cleanup):
            try:
                frappe.delete_doc(
                    doctype, name, force=True, ignore_permissions=True
                )
            except Exception:
                frappe.log_error(
                    title=f"EasyEcom supplier_pull: cleanup failed for {doctype} {name}",
                    message=f"{type(exc).__name__}: {exc}",
                )

        validator = _identify_validator(str(exc))
        reason = f"{validator}: {exc}"
        _create_fnc_map_row(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=ee_vendor_id,
            supplier_name=erpnext_fields.get("supplier_name"),
            reason=reason,
        )
        return SupplierOutcome(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=ee_vendor_id,
            supplier_docname=None,
            status="Flagged-Not-Created",
            operation="flagged",
            country_kind=country_kind,
            flag_reasons=[reason],
        )

    # === 4) Status from lifecycle ===
    status = STATUS_DISABLED if not is_active else STATUS_MAPPED
    _create_mapped_row(
        ee_vendor_c_id=ee_vendor_c_id,
        ee_vendor_id=ee_vendor_id,
        supplier_docname=supplier.name,
        status=status,
        flag_reasons=[],
    )
    _write_pull_sync_record(
        entity_name=supplier.name,
        ee_vendor_c_id=ee_vendor_c_id,
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return SupplierOutcome(
        ee_vendor_c_id=ee_vendor_c_id,
        ee_vendor_id=ee_vendor_id,
        supplier_docname=supplier.name,
        status=status,
        operation="created",
        country_kind=country_kind,
        flag_reasons=[],
    )


# ----- Pre-flatten helper -----


def _flatten_vendor_row(row: dict) -> dict:
    """Promote `address.billing.*` and `address.dispatch.*` to flat
    top-level keys in a copy of the row, so the Field Mapping engine
    can read them with single-segment paths.

    EE's getVendors returns `address: {billing: {...}, dispatch: {...}}`
    when both are populated, but EITHER may be `[]` (empty array, not
    object) when the supplier has no address of that kind. Treating
    `[]` as object-with-no-keys here gives the engine a clean absence
    rather than a runtime TypeError on `.get(...)` against a list.

    Returns a new dict — the original row is untouched (callers may
    still reference the raw nested shape for diagnostics)."""
    if not isinstance(row, dict):
        return row
    flat = dict(row)
    address = row.get("address") or {}
    if not isinstance(address, dict):
        return flat  # 'address' itself is junk — engine sees no address
    for kind in ("billing", "dispatch"):
        sub = address.get(kind)
        if isinstance(sub, dict):
            for ee_key, flat_key in (
                ("address", f"{kind}_address_line"),
                ("city", f"{kind}_city"),
                ("zip", f"{kind}_zip"),
                ("state_name", f"{kind}_state_name"),
                ("country", f"{kind}_country"),
                ("state_id", f"{kind}_state_id"),
            ):
                val = sub.get(ee_key)
                if val is not None and val != "":
                    flat[flat_key] = val
        # else: empty list, missing, or junk — leave keys absent.
    return flat


# ----- Country-classification helpers -----


def _classify_country(country_name: str | None) -> Literal["india", "foreign", "unknown"]:
    """Three-way: india / foreign / unknown.

    Indian-ness is keyed on the COUNTRY NAME being 'India' (or aliases)
    — if EE returns blank country, we treat as 'unknown' which the
    flow downgrades to Indian-default (matches the §8e customer URP
    handling). Foreign requires the country to be resolvable via the
    Stage 2 cache AND be != India.
    """
    if not country_name:
        return "unknown"
    needle = country_name.strip().lower()
    if not needle:
        return "unknown"
    if needle in ("india", "in", "ind", "bharat"):
        return "india"
    # Verify it's a real country (Stage 2 cache) — defends against
    # garbage country names.
    resolved = resolve_country(country_name)
    if resolved is None:
        return "unknown"
    if resolved.country_id == 1:
        return "india"
    return "foreign"


def _resolve_erpnext_country(country_name: str | None) -> str:
    """Canonicalise the country name to an ERPNext Country docname.

    ERPNext ships its own Country DocType (separate from EasyEcom
    Country). The EE country name usually matches the ERPNext name
    1:1 ('India', 'Italy', 'Armenia', etc.) but we resolve via the
    Stage 2 cache to canonicalise case (and to refuse junk before
    it lands as a literal-string country on the Supplier).

    Falls back to 'India' when nothing else is resolvable — matches
    the §8e customer pull's behaviour. Address.country is a Link to
    Country (Frappe will throw on save if the name doesn't exist).
    """
    if not country_name:
        return "India"
    resolved = resolve_country(country_name)
    if resolved is None:
        return "India"
    # The EE name typically lines up with ERPNext's standard country
    # list. If not, frappe.db.exists check + fallback to India.
    if frappe.db.exists("Country", resolved.name):
        return resolved.name
    return "India"


def _truthy(value: Any) -> bool:
    """Coerce EE's `active` field to bool. EE returns 1, 0, true,
    false, or sometimes the string 'false' (observed in one sample
    vendor — vendor_c_id 258016 has `unregisteredVendor: 'false'`)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


# ----- IC validator-tag helper -----


def _identify_validator(error_message: str) -> str:
    """Best-effort tag for the FNC flag_reason. Mirrors §8e's
    _identify_validator with PAN added — Supplier validation routes
    through validate_pan() in addition to GSTIN checks."""
    msg = error_message.lower()
    if "check digit" in msg or "invalid gstin" in msg:
        return "ic_gstin_check_digit"
    if "first 2 digits of gstin" in msg or "state number" in msg:
        return "ic_gstin_state_code_mismatch"
    if "postal code" in msg and "not associated" in msg:
        return "ic_pincode_state_mismatch"
    if "gstin format" in msg or "invalid format" in msg:
        return "ic_gstin_format"
    if "invalid pan" in msg or "pan format" in msg:
        return "ic_pan_format"
    return "ic_validate_failed"


# ----- ERPNext write helpers -----


def _create_supplier(
    *,
    ee_vendor_c_id: str,
    erpnext_fields: dict[str, Any],
    gstin: str,
    pan: str,
    gst_category: str | None,
    country: str,
    is_active: bool,
) -> Any:
    """Insert a new Supplier. Frappe auto-names the docname when
    supplier_naming_by is 'Naming Series' or 'Auto Name'; otherwise
    the docname is the supplier_name. We capture supplier.name from
    the returned doc.

    **Dup-name resilience**: Harmony's sandbox + plenty of real-client
    EE tenants have multiple vendors that legitimately share the
    same vendor_name (e.g. 'MSTEST_123', 'Akanksha', 'library' in
    Harmony — terse / test-data-ish names that collide on
    Supplier.name because ERPNext autonames by supplier_name). The
    second insert would raise DuplicateEntryError and the savepoint
    isolation would surface it as a failed pull. Catch on first
    failure, append a `-{ee_vendor_c_id}` suffix (guaranteed unique
    by EE), retry once. supplier_name itself stays as the EE value
    (only `.name` carries the disambiguation), so the FDE sees the
    real EE name in the list view + on linked POs/GRNs.

    Caller is responsible for catching frappe.ValidationError from
    India Compliance — we don't swallow here so the caller can decide
    between FNC (validate failure) vs raise (infra failure).
    """
    supplier_name = (
        (erpnext_fields.get("supplier_name") or "").strip()
        or f"EE Supplier {ee_vendor_c_id}"
    )

    def _build(name_for_docname: str) -> Any:
        s = frappe.new_doc("Supplier")
        payload: dict[str, Any] = {
            # supplier_name is what shows in the list/links; on first
            # try it equals the EE name, on retry it carries the
            # vendor_c_id suffix so the autoname doesn't collide.
            "supplier_name": name_for_docname,
            "supplier_type": "Company",
            "supplier_group": _default_supplier_group(),
            "country": country,
            "disabled": 0 if is_active else 1,
        }
        if erpnext_fields.get("default_currency"):
            payload["default_currency"] = erpnext_fields["default_currency"]
        if gstin:
            payload["gstin"] = gstin
        if pan:
            payload["pan"] = pan
        if gst_category:
            payload["gst_category"] = gst_category
        s.update(payload)
        return s

    supplier = _build(supplier_name)
    try:
        supplier.insert(ignore_permissions=True)
        return supplier
    except frappe.DuplicateEntryError:
        # ERPNext autoname collision — another Supplier already
        # carries this docname. Disambiguate by appending the EE
        # read-key (guaranteed unique). The new supplier_name shows
        # both pieces so the FDE can still find/identify the row.
        disambiguated = f"{supplier_name} ({ee_vendor_c_id})"
        supplier = _build(disambiguated)
        supplier.insert(ignore_permissions=True)
        frappe.log_error(
            title=f"supplier_pull: dup-name disambiguation for vendor_c_id={ee_vendor_c_id}",
            message=(
                f"EE vendor_name={supplier_name!r} collided with an existing "
                f"Supplier docname; created as {disambiguated!r} (docname="
                f"{supplier.name!r}). vendor_c_id remains the join key on "
                "the Supplier Map; downstream lookups via the Map are "
                "unaffected."
            ),
        )
        return supplier


def _create_address_strict(
    *,
    supplier_docname: str,
    address_type: Literal["Billing", "Shipping"],
    street: str,
    city: str,
    zipcode: str,
    state_name: str,
    country_name: str,
    gstin: str | None,
) -> str | None:
    """Insert an Address linked to the Supplier via Address.links.

    Returns the Address docname, or None when there's no content to
    write (empty-array dispatch — 16 of 30 captured vendors). Missing-
    address isn't an IC failure and doesn't trigger FNC. Strict on
    IC ValidationError: propagates up so the caller can roll back the
    Supplier + FNC the whole row (§8f Stage 3 — tax-relevant dirt is
    held, not soft-degraded; matches §8e).
    """
    if not (street or city or zipcode or state_name):
        return None

    address = frappe.new_doc("Address")
    address.append(
        "links",
        {"link_doctype": "Supplier", "link_name": supplier_docname},
    )
    address.update(
        {
            "address_title": supplier_docname,
            "address_type": address_type,
            "address_line1": street or "Address Line 1",
            "city": city or "Unknown",
            "pincode": zipcode or "",
            "state": state_name or "",
            "country": country_name or "India",
        }
    )
    if gstin:
        address.gstin = gstin
    address.insert(ignore_permissions=True)
    return address.name


def _create_mapped_row(
    *,
    ee_vendor_c_id: str,
    ee_vendor_id: str | None,
    supplier_docname: str,
    status: SupplierStatus,
    flag_reasons: list[str],
) -> str:
    """Insert the Supplier Map row that links the EE vendor_c_id to
    the ERPNext Supplier. flag_reasons joined with `||` (mirror §8d/§8e)."""
    row = frappe.new_doc("EasyEcom Supplier Map")
    row.update(
        {
            "ee_vendor_c_id": ee_vendor_c_id,
            "ee_vendor_id": ee_vendor_id or "",
            "erpnext_doctype": "Supplier",
            "erpnext_name": supplier_docname,
            "status": status,
            "flag_reason": "||".join(flag_reasons) if flag_reasons else "",
        }
    )
    row.insert(ignore_permissions=True)
    return row.name


def _create_fnc_map_row(
    *,
    ee_vendor_c_id: str,
    ee_vendor_id: str | None,
    supplier_name: str | None,
    reason: str,
) -> str:
    """Insert a Flagged-Not-Created Supplier Map row (no ERPNext
    Supplier linked — the row carries the FDE-facing FNC state)."""
    row = frappe.new_doc("EasyEcom Supplier Map")
    row.update(
        {
            "ee_vendor_c_id": ee_vendor_c_id,
            "ee_vendor_id": ee_vendor_id or "",
            "status": "Flagged-Not-Created",
            "flag_reason": reason[:140],  # flag_reason is Data
        }
    )
    row.insert(ignore_permissions=True)
    return row.name


def _refresh_existing_row(
    *,
    map_name: str,
    supplier_docname: str | None,
    current_status: str | None,
    is_active: bool,
    new_ee_vendor_id: str | None,
) -> SupplierStatus:
    """When a Supplier Map row already exists, refresh its lifecycle-
    derived state. active=0 → Disabled; active=1 from previously-
    Disabled → restore Mapped. Other statuses (Drift, Created-Flagged,
    FNC) are sticky — only the lifecycle flip toggles between Mapped
    and Disabled. Also opportunistically captures vendor_id if it
    wasn't set on the original row."""
    target_status: SupplierStatus
    if not is_active:
        target_status = STATUS_DISABLED
    elif current_status == STATUS_DISABLED:
        target_status = STATUS_MAPPED
    else:
        # Sticky non-lifecycle status (Mapped/Drift/Created-Flagged/FNC)
        target_status = current_status  # type: ignore[assignment]

    updates: dict[str, Any] = {}
    if target_status != current_status:
        updates["status"] = target_status
    if new_ee_vendor_id and not frappe.db.get_value(
        "EasyEcom Supplier Map", map_name, "ee_vendor_id"
    ):
        updates["ee_vendor_id"] = new_ee_vendor_id

    # Mirror the supplier-side disabled flag with the map row.
    if supplier_docname:
        current_disabled = frappe.db.get_value(
            "Supplier", supplier_docname, "disabled"
        )
        new_disabled = 0 if is_active else 1
        if int(current_disabled or 0) != new_disabled:
            frappe.db.set_value(
                "Supplier",
                supplier_docname,
                "disabled",
                new_disabled,
                update_modified=True,
            )

    if updates:
        for k, v in updates.items():
            frappe.db.set_value(
                "EasyEcom Supplier Map", map_name, k, v, update_modified=True
            )

    return target_status


def _write_pull_sync_record(
    *,
    entity_name: str | None,
    ee_vendor_c_id: str,
    status: str,
    last_error: str | None,
) -> str | None:
    return write_supplier_pull_sync_record(
        entity_name=entity_name,
        ee_vendor_c_id=ee_vendor_c_id,
        status=status,
        last_error=last_error,
    )


# ----- Defaults / account-state helpers -----


def _default_supplier_group() -> str:
    """Pick a non-group Supplier Group. ERPNext usually has 'All
    Supplier Groups' (root) + leaf children; we pick the first leaf
    or create a 'EasyEcom' leaf if none exists."""
    leaf = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if leaf:
        return leaf
    if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
        root = frappe.new_doc("Supplier Group")
        root.update(
            {"supplier_group_name": "All Supplier Groups", "is_group": 1}
        )
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Supplier Group")
    leaf_doc.update(
        {
            "supplier_group_name": "EasyEcom",
            "parent_supplier_group": "All Supplier Groups",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name


def _enabled_account() -> Any:
    """Return the single enabled EasyEcom Account. Mirrors §8d/§8e."""
    name = frappe.db.get_value("EasyEcom Account", {"enabled": 1}, "name")
    if not name:
        frappe.throw(
            "No enabled EasyEcom Account found. Enable one before running "
            "supplier pull."
        )
    return frappe.get_doc("EasyEcom Account", name)


def _resolve_supplier_master_mode(account_doc: Any) -> str:
    """Return supplier_master_mode for the account. Defaults to
    onboarding if blank (matches the schema default)."""
    return (
        getattr(account_doc, "supplier_master_mode", None)
        or MODE_ONBOARDING
    )


def _clear_cursor(account_doc: Any) -> None:
    frappe.db.set_value(
        "EasyEcom Account",
        account_doc.name,
        {"supplier_pull_cursor": "", "supplier_pull_cursor_at": None},
        update_modified=False,
    )


def _advance_cursor(account_doc: Any, next_endpoint: str | None) -> None:
    frappe.db.set_value(
        "EasyEcom Account",
        account_doc.name,
        {
            "supplier_pull_cursor": next_endpoint or "",
            "supplier_pull_cursor_at": frappe.utils.now_datetime(),
        },
        update_modified=False,
    )


def _set_clean_completion(account_doc: Any, *, total: int) -> None:
    """A clean walk (cursor exhausted, no failures) bumps the delta
    high-water and stores the total. Stage 6 cron uses
    supplier_pull_last_updated_at as the next pull's updated_after
    parameter."""
    frappe.db.set_value(
        "EasyEcom Account",
        account_doc.name,
        {
            "supplier_pull_last_updated_at": frappe.utils.now_datetime(),
            "supplier_pull_total_seen": int(total),
        },
        update_modified=False,
    )


# ============================================================
# Stage 5 — Drift detection (post-flip, erpnext_mastered mode)
# ============================================================
#
# When supplier_master_mode=erpnext_mastered, the pull becomes
# drift-detection-only: ERPNext owns the supplier master; EE-side
# new vendors and edits to mapped vendors show as Drift in the
# Supplier Map (audit-visible), NEVER auto-create or auto-overwrite.
#
# Three outcomes (mirror §8e Customer Drift):
#   1. EE-origin NEW vendor (no Supplier Map row): Drift row created
#      so the vendor_c_id is FDE-visible; no Supplier inserted.
#   2. EE-side EDIT to mapped Supplier: Drift status + structured
#      per-field diffs in drift_fields child table; Supplier untouched.
#   3. NO drift (quiet re-pull): drift child rows cleared, status
#      preserved (no auto-heal — Drift sticks until Dismiss).
#
# Sync Record direction stays Pull; status flips to Discrepancy
# (NOT Failed) because divergence is not a failure per §7.3.


def _detect_drift_one_supplier(
    *,
    row: dict,
    erpnext_fields: dict,
    ee_vendor_c_id: str,
    ee_vendor_id: str | None,
) -> "SupplierOutcome":
    """Post-flip pull — DETECTS drift, never accepts or overwrites.

    Mirrors §8e Customer Drift exactly: three outcomes, the "no flap"
    rule (Mapped stays Mapped, Drift sticks until Dismiss), no
    Accept-EE direction, no Supplier mutation under any branch.
    """
    map_row = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"ee_vendor_c_id": ee_vendor_c_id},
        ["name", "erpnext_name", "status", "ee_vendor_id"],
        as_dict=True,
    )

    country_kind = _classify_country(
        erpnext_fields.get("billing_country_name")
        or erpnext_fields.get("dispatch_country_name")
    )

    # === Case 1: EE-origin new vendor post-flip ===
    if not map_row:
        reason = (
            f"EE-origin new vendor vendor_c_id={ee_vendor_c_id} appeared "
            "post-flip (supplier_master_mode=erpnext_mastered); not "
            "created in ERPNext because ERPNext is the source of truth "
            "in steady state. FDE: (a) create the Supplier in ERPNext "
            "and push it, or (b) ignore EE-side novelty by marking this "
            "row Disabled."
        )
        _upsert_drift_map_row(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=ee_vendor_id,
            erpnext_name=None,
            reason=reason,
        )
        _write_pull_sync_record(
            entity_name=None,
            ee_vendor_c_id=ee_vendor_c_id,
            status=STATUS_DISCREPANCY,
            last_error=reason,
        )
        return SupplierOutcome(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=ee_vendor_id,
            supplier_docname=None,
            status="Drift",
            operation="flagged",
            country_kind=country_kind,
            flag_reasons=[reason],
        )

    # === Case 2: broken-link existing map (Map.erpnext_name points
    #             nowhere). Flip to Drift; FDE investigates. ===
    if not map_row.erpnext_name or not frappe.db.exists(
        "Supplier", map_row.erpnext_name
    ):
        reason = (
            f"Map row {map_row.name} has no linked Supplier (or the "
            "Supplier was deleted); cannot compare for drift. FDE: "
            "investigate the map row's link target."
        )
        _mark_existing_map_drift(map_row.name, reasons=[reason])
        _write_pull_sync_record(
            entity_name=None,
            ee_vendor_c_id=ee_vendor_c_id,
            status=STATUS_DISCREPANCY,
            last_error=reason,
        )
        return SupplierOutcome(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=map_row.ee_vendor_id,
            supplier_docname=map_row.erpnext_name,
            status="Drift",
            operation="flagged",
            country_kind=country_kind,
            flag_reasons=[reason],
        )

    # === Case 3: existing mapping — diff payload vs Supplier + Addresses ===
    supplier = frappe.get_doc("Supplier", map_row.erpnext_name)
    excluded_fields = _load_excluded_fields(map_row.name)
    diffs = _diff_supplier_payload_vs_docs(
        erpnext_fields=erpnext_fields,
        supplier=supplier,
        excluded_fields=excluded_fields,
    )

    if not diffs:
        # Quiet re-pull: no flap. Clear child diff rows so the FDE
        # doesn't see stale diffs from a prior run, but DO NOT change
        # status (a Drift row stays Drift until Dismiss; a Mapped row
        # stays Mapped — same §8d/§8e contract).
        _clear_drift_state(map_row.name)
        _write_pull_sync_record(
            entity_name=supplier.name,
            ee_vendor_c_id=ee_vendor_c_id,
            status=STATUS_SUCCESS,
            last_error=None,
        )
        return SupplierOutcome(
            ee_vendor_c_id=ee_vendor_c_id,
            ee_vendor_id=map_row.ee_vendor_id,
            supplier_docname=supplier.name,
            status=map_row.status or "Mapped",
            operation="skipped",
            country_kind=country_kind,
            flag_reasons=[],
        )

    _record_drift_with_table(map_row.name, diffs=diffs)
    reason_strings = [
        f"{d['field']}: ERPNext={d['erpnext_value']!r} EE→{d['ee_value']!r}"
        for d in diffs
    ]
    _write_pull_sync_record(
        entity_name=supplier.name,
        ee_vendor_c_id=ee_vendor_c_id,
        status=STATUS_DISCREPANCY,
        last_error=" || ".join(reason_strings),
    )
    return SupplierOutcome(
        ee_vendor_c_id=ee_vendor_c_id,
        ee_vendor_id=map_row.ee_vendor_id,
        supplier_docname=supplier.name,
        status="Drift",
        operation="flagged",
        country_kind=country_kind,
        flag_reasons=reason_strings,
    )


def _diff_supplier_payload_vs_docs(
    *,
    erpnext_fields: dict,
    supplier: Any,
    excluded_fields: set[str],
) -> list[dict]:
    """Compare translated EE payload against the existing Supplier +
    its linked Billing/Shipping Addresses. Returns structured diff
    dicts shaped like EasyEcom Drift Field child rows
    (field/erpnext_value/ee_value).

    Address sourcing: EE getVendors returns dual billing+dispatch
    envelopes (flattened by _flatten_vendor_row); the ERPNext side
    stores them as two separate Address rows linked via Address.links.
    We look up Billing once and Shipping once; reuse for all rows
    under each side.
    """
    diffs: list[dict] = []

    # Supplier-level fields.
    for fld in SUPPLIER_DRIFT_COMPARABLE_SUPPLIER_FIELDS:
        if fld in excluded_fields:
            continue
        if fld not in erpnext_fields:
            continue
        ee_value = erpnext_fields.get(fld)
        # Supplier.email_id / mobile_no are read-only fetched from the
        # linked Contact — for drift, read them via the same Contact
        # lookup the push uses (single source of truth).
        if fld == "email_id":
            en_value = (
                supplier.get("email_id")
                or _primary_contact_field_for_drift(supplier.name, "email_id")
            )
        elif fld == "mobile_no":
            en_value = (
                supplier.get("mobile_no")
                or _primary_contact_field_for_drift(supplier.name, "mobile_no")
                or _primary_contact_field_for_drift(supplier.name, "phone")
            )
        else:
            en_value = supplier.get(fld)
        if _values_differ(ee_value, en_value):
            diffs.append(
                {
                    "field": fld,
                    "erpnext_value": _stringify(en_value),
                    "ee_value": _stringify(ee_value),
                }
            )

    # Address-level fields. Billing + Shipping looked up once each.
    billing = _find_address_for_supplier_drift(
        supplier.name, address_type="Billing"
    )
    shipping = _find_address_for_supplier_drift(
        supplier.name, address_type="Shipping"
    )
    for label, payload_key, address_field in SUPPLIER_DRIFT_COMPARABLE_ADDRESS_FIELDS:
        if label in excluded_fields:
            continue
        if payload_key not in erpnext_fields:
            continue
        ee_value = erpnext_fields.get(payload_key)
        address_doc = billing if label.startswith("billing.") else shipping
        en_value = (address_doc or {}).get(address_field) if address_doc else None
        if _values_differ(ee_value, en_value):
            diffs.append(
                {
                    "field": label,
                    "erpnext_value": _stringify(en_value),
                    "ee_value": _stringify(ee_value),
                }
            )

    return diffs


def _find_address_for_supplier_drift(
    supplier_docname: str, *, address_type: str
) -> dict | None:
    """Read-only Address lookup for Supplier drift comparison."""
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.city, a.pincode, a.state, a.country
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
          AND a.address_type = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (supplier_docname, address_type),
        as_dict=True,
    )
    return rows[0] if rows else None


def _primary_contact_field_for_drift(
    supplier_docname: str, fieldname: str
) -> str | None:
    """Return the named field from the primary Contact linked to the
    Supplier. Same query shape as supplier_push's helper but inlined
    here to keep the drift module's dependencies symmetric (the push
    module imports from here, not the other way round)."""
    rows = frappe.db.sql(
        f"""
        SELECT c.{fieldname}
        FROM `tabContact` c
        JOIN `tabDynamic Link` dl ON dl.parent = c.name
        WHERE dl.parenttype = 'Contact'
          AND dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
          AND c.{fieldname} IS NOT NULL AND c.{fieldname} != ''
        ORDER BY c.is_primary_contact DESC, c.creation ASC
        LIMIT 1
        """,
        (supplier_docname,),
    )
    return rows[0][0] if rows else None


def _values_differ(a: Any, b: Any) -> bool:
    """Drift comparison with None / "" / 0 leniency. Same shape as
    customer_pull's helper. Treats None and "" as equal; coerces to
    float for numeric comparison; falls back to string equality."""

    def _norm(v: Any) -> Any:
        if v in (None, ""):
            return None
        if isinstance(v, str):
            return v.strip() or None
        return v

    na, nb = _norm(a), _norm(b)
    if na is None and nb is None:
        return False
    if na is None or nb is None:
        return True
    try:
        return abs(float(na) - float(nb)) > 0.0001
    except (TypeError, ValueError):
        return na != nb


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= 200 else (s[:197] + "...")


def _load_excluded_fields(map_name: str) -> set[str]:
    """Read FDE-marked exclude list off the Supplier Map row. Reuses
    the renamed entity-agnostic EasyEcom Exclude Field child DocType
    (see §8f Stage 1 rename — was EasyEcom Item Map Exclude Field)."""
    rows = frappe.db.get_all(
        "EasyEcom Exclude Field",
        filters={"parent": map_name, "parenttype": "EasyEcom Supplier Map"},
        fields=["field"],
    )
    return {r.field for r in rows if r.field}


def _upsert_drift_map_row(
    *,
    ee_vendor_c_id: str,
    ee_vendor_id: str | None,
    erpnext_name: str | None,
    reason: str,
) -> str:
    """Create or update a Supplier Map row in Drift status. Used for
    the EE-origin-new-vendor case (no prior map row).

    ee_vendor_c_id remains the unique key (autoname = ECS-SUPP-
    {ee_vendor_c_id}); ee_vendor_id may be empty if EE hasn't
    surfaced one for this c_id yet (a Drift on a never-pushed vendor
    is plausible)."""
    existing = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"ee_vendor_c_id": ee_vendor_c_id},
        "name",
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            existing,
            {"status": STATUS_DRIFT, "flag_reason": reason[:140]},
            update_modified=True,
        )
        return existing
    doc = frappe.new_doc("EasyEcom Supplier Map")
    doc.update(
        {
            "ee_vendor_c_id": ee_vendor_c_id,
            "ee_vendor_id": ee_vendor_id or "",
            "erpnext_doctype": "Supplier" if erpnext_name else None,
            "erpnext_name": erpnext_name,
            "status": STATUS_DRIFT,
            "flag_reason": reason[:140],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _mark_existing_map_drift(map_name: str, *, reasons: list[str]) -> None:
    """Flip an existing map row to Drift status with the diff list as
    the reason. NEVER mutates the linked Supplier — drift is read-only
    on the ERPNext side."""
    reason = " || ".join(reasons) if reasons else ""
    frappe.db.set_value(
        "EasyEcom Supplier Map",
        map_name,
        {"status": STATUS_DRIFT, "flag_reason": reason[:140]},
        update_modified=True,
    )


def _record_drift_with_table(map_name: str, *, diffs: list[dict]) -> None:
    """Write the structured diff to the Supplier Map's drift_fields
    child table + set status=Drift + drift_detected_at=now. Saves the
    parent map doc once (not per-row)."""
    map_doc = frappe.get_doc("EasyEcom Supplier Map", map_name)
    map_doc.set("drift_fields", [])
    now = frappe.utils.now_datetime()
    for d in diffs:
        map_doc.append(
            "drift_fields",
            {
                "field": d["field"],
                "erpnext_value": d["erpnext_value"],
                "ee_value": d["ee_value"],
                "detected_at": now,
            },
        )
    map_doc.status = STATUS_DRIFT
    map_doc.drift_detected_at = now
    map_doc.flag_reason = (
        f"{len(diffs)} field(s) drifted — see Drift Fields table below"
    )
    map_doc.save(ignore_permissions=True)


def _clear_drift_state(map_name: str) -> None:
    """Clean re-pull — clear the drift table + drift_detected_at so
    historical diffs don't linger when the EE-side state has been
    fixed upstream. Mirrors §8d/§8e EXACTLY: status is left untouched
    (FDE owns the Drift → Mapped transition via Dismiss). A Drift row
    whose diffs vanished still requires explicit FDE acknowledgement
    — that's the §8.3 audit-trail contract.

    Returns silently when there were no drift rows to clear (this is
    the steady-state quiet-re-pull case for a Mapped row)."""
    map_doc = frappe.get_doc("EasyEcom Supplier Map", map_name)
    if not map_doc.get("drift_fields"):
        return
    map_doc.set("drift_fields", [])
    map_doc.drift_detected_at = None
    map_doc.save(ignore_permissions=True)


# ----- Drift resolution actions (whitelisted) -----


@frappe.whitelist()
def dismiss_drift(supplier_map_name: str) -> dict[str, Any]:
    """FDE acknowledges the EE-side change is wrong or already-handled
    upstream. Returns the row to Mapped, clears the Drift Fields child
    table, leaves the underlying Supplier + Addresses untouched.

    The next pull will re-detect if the divergence still exists; to
    silence persistent intentional divergence, the FDE adds the field
    to ecs_drift_exclude_fields instead.

    Mirror of §8e Customer dismiss_drift."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Dismiss Drift requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )
    if not supplier_map_name:
        return {"ok": False, "message": "supplier_map_name required"}
    if not frappe.db.exists("EasyEcom Supplier Map", supplier_map_name):
        return {
            "ok": False,
            "message": f"Supplier Map {supplier_map_name!r} not found.",
        }
    doc = frappe.get_doc("EasyEcom Supplier Map", supplier_map_name)
    if doc.status != STATUS_DRIFT:
        return {
            "ok": False,
            "message": (
                f"Supplier Map {supplier_map_name!r} is not in Drift "
                f"status (current: {doc.status}); nothing to dismiss."
            ),
        }
    doc.status = STATUS_MAPPED
    doc.flag_reason = None
    doc.drift_detected_at = None
    doc.set("drift_fields", [])
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "ok": True,
        "supplier_map_name": supplier_map_name,
        "status": STATUS_MAPPED,
    }


@frappe.whitelist()
def push_to_ee_for_drift(supplier_map_name: str) -> dict[str, Any]:
    """FDE re-asserts ERPNext as SoT by pushing the current Supplier
    state to EE, overwriting the EE-side divergence. Dispatches to the
    Stage 4 push (push_one_supplier).

    On success the push flow writes a fresh snapshot + sets the Map
    row to Mapped; the next pull will see no drift.

    NO 'Accept EE Value' counterpart — §8.3 post-flip contract is
    'ERPNext wins; EE-side novelty/edits are not adopted'. The only
    paths out of Drift are dismiss-and-wait or push-to-overwrite."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Push to EasyEcom requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )
    if not supplier_map_name:
        return {"ok": False, "message": "supplier_map_name required"}
    if not frappe.db.exists("EasyEcom Supplier Map", supplier_map_name):
        return {
            "ok": False,
            "message": f"Supplier Map {supplier_map_name!r} not found.",
        }
    doc = frappe.get_doc("EasyEcom Supplier Map", supplier_map_name)
    if doc.status != STATUS_DRIFT:
        return {
            "ok": False,
            "message": (
                f"Supplier Map {supplier_map_name!r} is not in Drift "
                f"status (current: {doc.status}); use the Supplier-form "
                "Push button for non-drift pushes."
            ),
        }
    if not doc.erpnext_name:
        return {
            "ok": False,
            "message": (
                f"Supplier Map {supplier_map_name!r} has no linked "
                "Supplier; create one in ERPNext first, then re-pull "
                "to set the link."
            ),
        }

    from ecommerce_super.easyecom.flows.supplier_push import (
        push_one_supplier,
    )

    try:
        outcome = push_one_supplier(doc.erpnext_name)
    except Exception as exc:
        frappe.log_error(
            title=f"push_to_ee_for_drift failed: {supplier_map_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "supplier_map_name": supplier_map_name,
        "operation": outcome.operation,
        "pushed": outcome.pushed,
        "ee_vendor_c_id": outcome.ee_vendor_c_id,
        "ee_vendor_id": outcome.ee_vendor_id,
        "flag_reasons": outcome.flag_reasons,
    }


# ============================================================
# Stage 6 — Scheduler entry (delta-pull cron)
# ============================================================


def scheduled_discover_suppliers() -> None:
    """§8f Stage 6 daily supplier pull — wired in hooks.py
    scheduler_events at 06:00 IST (after §8d Items at 05:00 and §8e
    Customers at 05:30).

    DELTA semantics: unlike §8e Customer Pull which has no
    updated_after filter (forcing a full-pull every run), getVendors
    DOES accept `updated_after=YYYY-MM-DD` (verified live against
    Harmony 2026-05-27 — passing a future date returns
    `{"data": "No Data Found"}` envelope; passing an old date filters
    on EE's internal last-updated timestamp). The scheduler reads
    Account.supplier_pull_last_updated_at (set on clean-walk
    completion in _set_clean_completion) and passes it as
    updated_after. First-ever scheduled run with a blank high-water
    falls through to a full pull.

    Mode-aware: process_one_supplier's phase gate decides whether the
    pulled vendor is accepted-and-created (onboarding) or runs
    through drift detection (erpnext_mastered). No mode-specific
    branching here; same pull_suppliers call works for both phases.

    Quiet on no enabled Account (pre-onboarding state). Catches every
    exception so a transient EE outage doesn't fail the whole
    scheduler tick. Mirrors §8e scheduled_discover_customers exactly,
    plus the delta filter the customer endpoint doesn't expose."""
    account_row = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        ["name", "supplier_pull_last_updated_at"],
        as_dict=True,
        order_by="name asc",
    )
    if not account_row:
        return

    # Format the high-water as a date string for EE's filter. Falls
    # back to None (full pull) on the first-ever scheduled run.
    updated_after_param = None
    high_water = account_row.get("supplier_pull_last_updated_at")
    if high_water:
        try:
            updated_after_param = frappe.utils.getdate(high_water).isoformat()
        except Exception:
            updated_after_param = None

    try:
        pull_suppliers(
            account=account_row.name,
            start_fresh=True,
            updated_after=updated_after_param,
        )
    except Exception as exc:
        frappe.log_error(
            title="EasyEcom scheduled supplier discovery failed",
            message=f"{type(exc).__name__}: {exc}",
        )
