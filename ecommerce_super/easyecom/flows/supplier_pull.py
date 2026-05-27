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
) -> PullOutcome:
    """Fetch /wms/V2/getVendors page by page and process each row.

    `start_fresh=True` clears the persisted cursor and pulls from the
    top; `False` resumes from the persisted cursor (for restarting
    after a partial failure without re-processing the early pages).

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
    # creation so post-flip pulls NEVER mutate ERPNext. Stage 5 wires
    # _detect_drift_one_supplier; Stage 3 raises so a misconfigured
    # post-flip site doesn't silently dual-master. ===
    if account_mode == MODE_ERPNEXT_MASTERED:
        # Stage 3 doesn't implement drift detection — that's Stage 5.
        # We refuse to mutate; the FDE either does NOT flip yet, or
        # waits for Stage 5 to ship.
        raise NotImplementedError(
            "supplier_pull in erpnext_mastered mode requires Stage 5 drift "
            "detection — not implemented yet. Either delay the flip or "
            "build Stage 5 first."
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

    Caller is responsible for catching frappe.ValidationError from
    India Compliance — we don't swallow here so the caller can decide
    between FNC (validate failure) vs raise (infra failure).
    """
    supplier = frappe.new_doc("Supplier")
    payload: dict[str, Any] = {
        "supplier_name": (erpnext_fields.get("supplier_name") or "").strip()
        or f"EE Supplier {ee_vendor_c_id}",
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

    supplier.update(payload)
    supplier.insert(ignore_permissions=True)
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
