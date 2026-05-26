"""§8e Stage 3 — EE→EN Customer pull.

Foundational at the API Call layer (account-wide; no Company tag),
entity-sync at the flow layer (one Customer Map row + one Sync Record
per customer). Mirrors §8d Item Pull's shape: ruleset translation →
flow-level decisions → savepoint-isolated per-record processing.

The 4 packet decisions, resolved live against the Harmony fixture:
  1. Matching — NO natural-key auto-match. Map row exists → reuse;
     else → CREATE NEW Customer + map row. The real Harmony sample
     has 3 duplicated gstNums (4/7/3 occurrences) and 2 duplicated
     companynames; auto-matching on either would silently wrongly-link
     the wrong wholesale partners. 'Never wrongly link > never
     duplicate' (§8e packet).
  2. Pagination — /Wholesale/v2/UserManagement returns a flat data[]
     with no cursor markers; simple full pull.
  3. c_id == customerId — confirmed expected by the packet design;
     Stage 4 will verify on a write. Stage 3 stores both fields on
     the Customer Map (Stage 1 schema accommodates this).
  4. GSTIN gating — empty/URP → gst_category='Unregistered', empty
     gstin; valid GSTIN → set gstin, India Compliance derives
     gst_category; invalid GSTIN (India Compliance throws) → catch →
     Flagged-Not-Created (no placeholder).

Pincode-state validation uses Stage 2's validate_pincode_state — a
mismatch becomes Created-Flagged (Customer exists, FDE reviews); never
hard-blocks.

Mode handling: Stage 3 pull is the onboarding-mode behaviour
(bidirectional, supervised). Post-flip (erpnext_mastered) the pull
becomes drift-detection only — Stage 5 will branch on
customer_master_mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    WHOLESALE_USER_MANAGEMENT,
)
# Stage 2 resolvers are unused in Stage 3 pull (India Compliance is
# authoritative for tax-relevant validation during pull). Stage 4 push
# imports them directly from the state_resolver module.
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)
from ecommerce_super.easyecom.flows._customer_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_customer_pull_sync_record,
)
from ecommerce_super.easyecom.flows._isolation import (
    BatchOutcome,
    for_each_record,
)


CUSTOMER_PULL_RULESET: str = "EasyEcom-Customer-Pull"
RESPONSE_DATA_KEY: str = "data"

# Stage 5 — Map row status values + customer_master_mode constants.
# Mirrors §8d Stage 5 (item_pull.py). STATUS_DRIFT is what the
# post-flip pull writes when it detects EE-side divergence; ERPNext
# is NOT touched in that case (§8.2 / §7.3 — divergence is not failure).
STATUS_MAPPED: str = "Mapped"
STATUS_DRIFT: str = "Drift"
STATUS_FLAGGED_NOT_CREATED: str = "Flagged-Not-Created"

MODE_ONBOARDING: str = "onboarding"
MODE_ERPNEXT_MASTERED: str = "erpnext_mastered"

# Stage 5 — fields the drift detector compares between the freshly-
# translated EE payload and the existing ERPNext Customer + its linked
# Addresses. Split into Customer-level and Address-level so the FDE
# can tell from a drift_fields child row WHICH doc differed (the field
# label carries the prefix).
#
# Internal IDs (ee_c_id / ee_customer_id, c_id, customerId) are
# deliberately NOT compared — they're identity-management state, not
# user-visible content. A change there would be a remap operation,
# not drift in the §8.2 sense.
CUSTOMER_DRIFT_COMPARABLE_CUSTOMER_FIELDS: tuple[str, ...] = (
    "customer_name",
    "email_id",
    "mobile_no",
    "gstin",
    "default_currency",
)

# (drift label, payload_key from ruleset, ERPNext Address field).
# "billing." / "dispatch." prefix on the label is what the FDE sees
# in the drift_fields child table.
CUSTOMER_DRIFT_COMPARABLE_ADDRESS_FIELDS: tuple[tuple[str, str, str], ...] = (
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


# ----- Outcome types -----


CustomerStatus = Literal[
    "Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"
]


@dataclass
class CustomerOutcome:
    """Per-customer result returned from the per-record handler.

    `customer_docname` is None for FNC outcomes (no Customer was
    created); the Customer Map row's flag_reason carries the FDE-facing
    explanation."""

    ee_c_id: str
    customer_docname: str | None
    status: CustomerStatus
    operation: Literal["created", "skipped", "flagged"]
    flag_reasons: list[str] = field(default_factory=list)


@dataclass
class PullOutcome:
    """Aggregate result of a discover_customers run."""

    total: int = 0
    created: int = 0
    skipped: int = 0
    created_flagged: int = 0
    flagged_not_created: int = 0
    drift_count: int = 0  # Stage 5: post-flip detection
    failed: int = 0
    outcomes: list[CustomerOutcome] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


# ----- Top-level flow -----


def pull_customers(
    *,
    client: EasyEcomClient | None = None,
) -> PullOutcome:
    """Fetch /Wholesale/v2/UserManagement?type=b2b and process each row.

    Returns PullOutcome — used by the whitelisted endpoint to build the
    FDE-facing summary. No real EE writes — the endpoint is read-only.
    """
    if client is None:
        client = EasyEcomClient()

    response = client.get(WHOLESALE_USER_MANAGEMENT, params={"type": "b2b"})
    rows = (response or {}).get(RESPONSE_DATA_KEY) or []
    if not isinstance(rows, list):
        frappe.log_error(
            title="EasyEcom /Wholesale/v2/UserManagement: unexpected shape",
            message=(
                f"Expected dict with '{RESPONSE_DATA_KEY}' list; got "
                f"{type(response).__name__} with '{RESPONSE_DATA_KEY}'="
                f"{type(rows).__name__}"
            ),
        )
        return PullOutcome()

    return process_customer_rows(rows)


def process_customer_rows(rows: list[dict]) -> PullOutcome:
    """Drive the per-row processing loop with savepoint isolation.

    Separated from pull_customers so tests can feed fixture payloads
    directly without mocking the HTTP layer. The Field Mapping executor
    is instantiated ONCE for the whole batch — compilation queries the
    DB, doing it per row would be wasted work.

    **Phase-governed direction (Stage 5):** before dispatching, look up
    customer_master_mode on the enabled EasyEcom Account. In
    `erpnext_mastered` mode, every row routes to drift detection
    instead of accept-and-create. The branch happens INSIDE
    process_one_customer per the §8d pattern.

    Sets `frappe.flags.easyecom_customer_pull_in_flight=True` while
    processing — the Stage 4 push hook checks this flag to avoid
    re-pushing a customer that was just pulled (ping-pong guard).
    """
    from ecommerce_super.easyecom.flows.customer_push import PING_PONG_FLAG

    executor = FieldMappingExecutor(CUSTOMER_PULL_RULESET)
    aggregate = PullOutcome(total=len(rows))
    account_mode = _resolve_customer_master_mode()
    frappe.flags.__setattr__(PING_PONG_FLAG, True)

    def _handle(row: dict) -> None:
        outcome = process_one_customer(
            row, executor=executor, account_mode=account_mode
        )
        aggregate.outcomes.append(outcome)
        if outcome.status == "Mapped" and outcome.operation == "created":
            aggregate.created += 1
        elif outcome.operation == "skipped":
            aggregate.skipped += 1
        if outcome.status == "Created-Flagged":
            aggregate.created_flagged += 1
        elif outcome.status == "Flagged-Not-Created":
            aggregate.flagged_not_created += 1
        elif outcome.status == "Drift":
            aggregate.drift_count += 1

    def _on_failure(row: dict, exc: BaseException) -> None:
        ee_c_id = str((row or {}).get("c_id") or "<unknown>")
        aggregate.failed += 1
        aggregate.failures.append(
            {
                "ee_c_id": ee_c_id,
                "companyname": (row or {}).get("companyname"),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        # Best-effort Sync Record write OUTSIDE the savepoint so the
        # failure is observable on the FDE worklist. No entity to link
        # to (Customer wasn't created), so the SR write returns None;
        # the failure is still recorded via Error Log + the FDE-facing
        # aggregate summary.
        frappe.log_error(
            title=f"EasyEcom Customer pull failed: c_id={ee_c_id}",
            message=(
                f"{type(exc).__name__}: {exc}\nRow: {frappe.as_json(row)}"
            ),
        )

    try:
        for_each_record(
            rows,
            handler=_handle,
            on_failure=_on_failure,
            flow_name="customer_pull",
        )
    finally:
        # Reset the ping-pong guard even on exception. frappe.flags is
        # request-local but a stale True would suppress legitimate
        # auto-pushes within the same request.
        frappe.flags.__setattr__(PING_PONG_FLAG, False)
    return aggregate


# ----- Per-record processing -----


def process_one_customer(
    row: dict,
    *,
    executor: FieldMappingExecutor,
    account_mode: str = MODE_ONBOARDING,
) -> CustomerOutcome:
    """Translate one /Wholesale/v2/UserManagement row through the engine
    and create the ERPNext Customer + Billing/Shipping Address + Map.

    Contract (Stage 3 corrected 2026-05-27):
      - Map row exists for this c_id → reuse (no-op). Stage 5 will add
        refresh-on-re-pull and drift detection.
      - Else → attempt to create Customer + 2 Address docs + Map row.
      - GSTIN gating:
          empty / "URP"        → gst_category='Unregistered', empty gstin
          present + valid      → set gstin; India Compliance derives
                                 gst_category
          present + invalid    → India Compliance throws → catch → FNC
      - **India Compliance is the authority on tax-relevant validation.**
        Any IC ValidationError on Customer.validate OR Address.insert
        (invalid gstin, gstin-state-code ≠ address-state, pincode-prefix
        ≠ state, etc.) → the WHOLE Customer goes Flagged-Not-Created.
        Partial inserts are rolled back (delete Customer + any inserted
        Address). NO degraded data lands.
      - Only NON-tax-relevant dirt (empty optional fields handled by
        the ruleset's Permissive missing_field_policy) survives to
        Mapped status.

    Removed (Stage 3 correction): the prior 3-level address fallback
    ladder + Stage 2 pre-check soft-flag for pincode-state. Both
    contradicted the packet's "tax dirt is held, not soft-degraded"
    rule. The Stage 2 state_resolver remains — Stage 4 push uses it
    for name→id resolution.
    """
    erpnext_fields = executor.pull(row)
    ee_c_id = (erpnext_fields.get("ee_c_id") or "").strip()
    if not ee_c_id:
        raise ValueError(
            "Field Mapping engine returned no ee_c_id for /Wholesale row "
            "(check EasyEcom-Customer-Pull ruleset)."
        )

    # === Stage 5 phase gate: post-flip → drift detection only ===
    # Mirrors §8d Stage 5: branch BEFORE GSTIN gating / address creation
    # so post-flip pulls NEVER mutate ERPNext, regardless of what EE
    # sent. (§8.2 contract: in steady state, ERPNext is the source of
    # truth; EE-side change → Drift map row + Discrepancy SR.)
    if account_mode == MODE_ERPNEXT_MASTERED:
        return _detect_drift_one_customer(
            row=row, erpnext_fields=erpnext_fields, ee_c_id=ee_c_id
        )

    # === 1) Map-row match (no natural-key auto-match) ===
    existing_map = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"ee_c_id": ee_c_id},
        ["name", "erpnext_name", "status"],
        as_dict=True,
    )
    if existing_map:
        _write_pull_sync_record(
            entity_name=existing_map.erpnext_name,
            ee_c_id=ee_c_id,
            status=STATUS_SUCCESS,
            last_error=None,
        )
        return CustomerOutcome(
            ee_c_id=ee_c_id,
            customer_docname=existing_map.erpnext_name,
            status=existing_map.status or "Mapped",
            operation="skipped",
        )

    # === 2) GSTIN gating ===
    gstin_raw = (erpnext_fields.get("gstin") or "").strip().upper()
    is_urp = gstin_raw in ("", "URP")
    gst_category = "Unregistered" if is_urp else None
    gstin = "" if is_urp else gstin_raw

    # === 3-4) Atomic insert: Customer + both Addresses. Any India
    #         Compliance throw aborts the whole transaction and FNCs. ===
    inserted_for_cleanup: list[tuple[str, str]] = []  # [(doctype, name)]
    try:
        customer = _create_customer(
            ee_c_id=ee_c_id,
            erpnext_fields=erpnext_fields,
            gstin=gstin,
            gst_category=gst_category,
        )
        inserted_for_cleanup.append(("Customer", customer.name))

        billing_name = _create_address_strict(
            customer_docname=customer.name,
            address_type="Billing",
            street=erpnext_fields.get("billing_street") or "",
            city=erpnext_fields.get("billing_city") or "",
            zipcode=erpnext_fields.get("billing_zipcode") or "",
            state_name=erpnext_fields.get("billing_state_name") or "",
            country_name=erpnext_fields.get("billing_country_name") or "",
            gstin=gstin or None,
        )
        if billing_name:
            inserted_for_cleanup.append(("Address", billing_name))

        dispatch_name = _create_address_strict(
            customer_docname=customer.name,
            address_type="Shipping",
            street=erpnext_fields.get("dispatch_street") or "",
            city=erpnext_fields.get("dispatch_city") or "",
            zipcode=erpnext_fields.get("dispatch_zipcode") or "",
            state_name=erpnext_fields.get("dispatch_state_name") or "",
            country_name=erpnext_fields.get("dispatch_country_name") or "",
            gstin=gstin or None,
        )
        if dispatch_name:
            inserted_for_cleanup.append(("Address", dispatch_name))

    except frappe.ValidationError as exc:
        # Tax-relevant India Compliance throw → roll back partial
        # inserts (no degraded data lands). Identify WHICH validator
        # in the FNC flag_reason so the FDE can fix the source.
        for doctype, name in reversed(inserted_for_cleanup):
            try:
                frappe.delete_doc(
                    doctype, name, force=True, ignore_permissions=True
                )
            except Exception:  # noqa: BLE001
                # Best-effort cleanup; log but don't compound the failure.
                frappe.log_error(
                    title=f"EasyEcom customer_pull: cleanup failed for {doctype} {name}",
                    message=f"{type(exc).__name__}: {exc}",
                )

        validator = _identify_validator(str(exc))
        reason = f"{validator}: {exc}"
        _create_fnc_map_row(
            ee_c_id=ee_c_id,
            companyname=erpnext_fields.get("customer_name"),
            reason=reason,
        )
        return CustomerOutcome(
            ee_c_id=ee_c_id,
            customer_docname=None,
            status="Flagged-Not-Created",
            operation="flagged",
            flag_reasons=[reason],
        )

    # === 5) All inserts clean — Mapped status. (No Created-Flagged in
    #         Stage 3 today; Stage 5 may surface drift / staleness
    #         that uses Created-Flagged.) ===
    _create_mapped_row(
        ee_c_id=ee_c_id,
        customer_docname=customer.name,
        status="Mapped",
        flag_reasons=[],
    )
    _write_pull_sync_record(
        entity_name=customer.name,
        ee_c_id=ee_c_id,
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return CustomerOutcome(
        ee_c_id=ee_c_id,
        customer_docname=customer.name,
        status="Mapped",
        operation="created",
        flag_reasons=[],
    )


def _identify_validator(error_message: str) -> str:
    """Best-effort tag for the FNC flag_reason so the FDE knows which
    India Compliance check failed (invalid GSTIN format vs state-code
    mismatch vs pincode-prefix mismatch). Lookup is a substring match
    against the message strings IC actually emits."""
    msg = error_message.lower()
    if "check digit" in msg or "invalid gstin" in msg:
        return "ic_gstin_check_digit"
    if "first 2 digits of gstin" in msg or "state number" in msg:
        return "ic_gstin_state_code_mismatch"
    if "postal code" in msg and "not associated" in msg:
        return "ic_pincode_state_mismatch"
    if "gstin format" in msg or "invalid format" in msg:
        return "ic_gstin_format"
    return "ic_validate_failed"


# ----- Helpers (split for testability + readability) -----


def _create_customer(
    *,
    ee_c_id: str,
    erpnext_fields: dict[str, Any],
    gstin: str,
    gst_category: str | None,
) -> Any:
    """Insert a new Customer. Frappe auto-names (CUST-YYYY-NNNNN); the
    docname is NOT the customer_name. Caller captures customer.name.

    Caller is responsible for catching frappe.ValidationError from
    India Compliance — we don't swallow here so the caller can decide
    between FNC (validate failure) vs raise (infra failure)."""
    customer = frappe.new_doc("Customer")
    payload: dict[str, Any] = {
        "customer_name": (erpnext_fields.get("customer_name") or "").strip()
        or f"EE Customer {ee_c_id}",
        "customer_type": "Company",  # §8e wholesale is B2B/Company
        "customer_group": _default_customer_group(),
        "territory": _default_territory(),
    }
    if erpnext_fields.get("email_id"):
        payload["email_id"] = erpnext_fields["email_id"]
    if erpnext_fields.get("mobile_no"):
        payload["mobile_no"] = erpnext_fields["mobile_no"]
    if erpnext_fields.get("default_currency"):
        payload["default_currency"] = erpnext_fields["default_currency"]
    if gstin:
        payload["gstin"] = gstin
    if gst_category:
        payload["gst_category"] = gst_category

    customer.update(payload)
    customer.insert(ignore_permissions=True)
    return customer


def _create_address_strict(
    *,
    customer_docname: str,
    address_type: Literal["Billing", "Shipping"],
    street: str,
    city: str,
    zipcode: str,
    state_name: str,
    country_name: str,
    gstin: str | None,
) -> str | None:
    """Insert an Address linked to the Customer via the standard
    Address.links Dynamic Link child table.

    Strict: NO fallback ladder. India Compliance ValidationError
    propagates up so the caller can roll back the Customer + FNC the
    whole row. (Stage 3 correction 2026-05-27: tax-relevant dirt is
    held, not soft-degraded.)

    Returns the Address docname, or None when there's no content to
    write at all (EE sometimes returns customers with empty dispatch
    fields). Missing-address is a downstream concern for the FDE; it
    isn't a tax-validator failure and doesn't trigger FNC.
    """
    if not (street or city or zipcode or state_name):
        return None

    address = frappe.new_doc("Address")
    address.append(
        "links",
        {"link_doctype": "Customer", "link_name": customer_docname},
    )
    address.update(
        {
            "address_title": customer_docname,
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
    ee_c_id: str,
    customer_docname: str,
    status: CustomerStatus,
    flag_reasons: list[str],
) -> str:
    """Insert the Customer Map row that links the EE c_id to the
    ERPNext Customer. flag_reasons is joined with `||` into a single
    flag_reason string (mirrors §8d's Item Map pattern)."""
    flag_reason = " || ".join(flag_reasons) if flag_reasons else ""
    doc = frappe.new_doc("EasyEcom Customer Map")
    doc.update(
        {
            "ee_c_id": ee_c_id,
            "ee_customer_id": ee_c_id,  # Stage 4 will overwrite from CreateCustomer response if c_id != customerId
            "erpnext_doctype": "Customer",
            "erpnext_name": customer_docname,
            "status": status,
            "flag_reason": flag_reason,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _create_fnc_map_row(
    *, ee_c_id: str, companyname: str | None, reason: str
) -> str:
    """Flagged-Not-Created: no Customer exists on the ERPNext side;
    only the Map row carries the EE-side identity + reason."""
    doc = frappe.new_doc("EasyEcom Customer Map")
    doc.update(
        {
            "ee_c_id": ee_c_id,
            "ee_customer_id": ee_c_id,
            "status": "Flagged-Not-Created",
            "flag_reason": (
                f"{reason} (EE companyname: {companyname or '?'})"
            )[:140],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _write_pull_sync_record(
    *, entity_name: str | None, ee_c_id: str, status: str, last_error: str | None
) -> None:
    """Best-effort Sync Record write — never blocks the flow on a SR
    insert failure. Mirrors §8d's _write_pull_sync_record."""
    try:
        write_customer_pull_sync_record(
            entity_name=entity_name,
            ee_c_id=ee_c_id,
            status=status,
            last_error=last_error,
        )
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=(
                f"EasyEcom Customer pull: Sync Record write failed for "
                f"c_id={ee_c_id}"
            ),
            message=f"{type(exc).__name__}: {exc}",
        )


# ----- Stage 5 — drift detection -----


def _resolve_customer_master_mode() -> str:
    """Look up customer_master_mode on the first enabled EasyEcom
    Account. Defaults to onboarding (pre-flip behavior) when no
    account is configured. The pull flow is account-wide so this
    works as a single read at the top of the batch."""
    mode = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        "customer_master_mode",
        order_by="creation asc",
    )
    return mode or MODE_ONBOARDING


def _detect_drift_one_customer(
    *, row: dict, erpnext_fields: dict, ee_c_id: str
) -> CustomerOutcome:
    """Post-flip pull — DETECTS drift, never accepts or overwrites.

    Three outcomes (mirror §8d Stage 5):
      1. EE-origin NEW customer (no Customer Map row for the c_id):
         Drift map row is created so the c_id is FDE-visible; NO
         Customer / Address rows are created. In steady state ERPNext
         owns the customer master; EE novelty is flagged, not adopted.
      2. EE-side EDIT to a mapped customer (existing map → existing
         Customer; payload differs from Customer + linked Addresses
         on a comparable field): Drift; structured per-field diffs
         in drift_fields child table. Nothing on ERPNext is touched.
      3. NO drift (EE payload matches): map row left as-is; any prior
         drift_fields child rows cleared (the divergence was resolved
         upstream). Status stays Mapped — no flap.

    Lifecycle: EE exposes NO active/disabled signal on customers (the
    captured Harmony fixture only has `is_b2b_new` and it's false on
    all 23 records). So there's no pull-side lifecycle drift entry
    to emit (unlike §8d Item which flags EE active→0 disable).
    """
    map_row = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"ee_c_id": ee_c_id},
        ["name", "erpnext_name", "status"],
        as_dict=True,
    )

    # === Case 1: EE-origin new customer post-flip ===
    if not map_row:
        reason = (
            f"EE-origin new customer c_id={ee_c_id} appeared post-flip "
            f"(customer_master_mode=erpnext_mastered); not created in "
            "ERPNext because ERPNext is the source of truth in steady "
            "state. FDE: (a) create the Customer in ERPNext and push it "
            "(Stage 4 — but no auto-map exists for Customer; the FDE "
            "must explicitly create a map row to link), or (b) ignore "
            "EE-side novelty by marking this row Disabled."
        )
        map_name = _upsert_drift_map_row(
            ee_c_id=ee_c_id,
            erpnext_name=None,
            reason=reason,
        )
        _write_pull_sync_record(
            entity_name=None,  # no Customer to link to
            ee_c_id=ee_c_id,
            status=STATUS_DISCREPANCY,
            last_error=reason,
        )
        return CustomerOutcome(
            ee_c_id=ee_c_id,
            customer_docname=None,
            status="Drift",
            operation="flagged",
            flag_reasons=[reason],
        )

    # === Case 2/3: existing mapping — diff payload vs Customer + Addresses ===
    if not map_row.erpnext_name or not frappe.db.exists(
        "Customer", map_row.erpnext_name
    ):
        reason = (
            f"Map row {map_row.name} has no linked Customer (or the "
            "Customer was deleted); cannot compare for drift. FDE: "
            "investigate the map row's link target."
        )
        _mark_existing_map_drift(map_row.name, reasons=[reason])
        _write_pull_sync_record(
            entity_name=None,
            ee_c_id=ee_c_id,
            status=STATUS_DISCREPANCY,
            last_error=reason,
        )
        return CustomerOutcome(
            ee_c_id=ee_c_id,
            customer_docname=map_row.erpnext_name,
            status="Drift",
            operation="flagged",
            flag_reasons=[reason],
        )

    customer = frappe.get_doc("Customer", map_row.erpnext_name)
    excluded_fields = _load_excluded_fields(map_row.name)
    diffs = _diff_customer_payload_vs_docs(
        erpnext_fields=erpnext_fields,
        customer=customer,
        excluded_fields=excluded_fields,
    )

    if not diffs:
        # Clean re-pull — clear any stale drift child rows so the FDE
        # doesn't see ghost diffs. Status is preserved (§8d parity):
        # a previously-Drift row stays Drift until the FDE Dismisses;
        # a Mapped row stays Mapped.
        _clear_drift_state(map_row.name)
        _write_pull_sync_record(
            entity_name=customer.name,
            ee_c_id=ee_c_id,
            status=STATUS_SUCCESS,
            last_error=None,
        )
        return CustomerOutcome(
            ee_c_id=ee_c_id,
            customer_docname=customer.name,
            status=map_row.status or "Mapped",
            operation="skipped",
        )

    _record_drift_with_table(map_row.name, diffs=diffs)
    reason_strings = [
        f"{d['field']}: ERPNext={d['erpnext_value']!r} EE→{d['ee_value']!r}"
        for d in diffs
    ]
    _write_pull_sync_record(
        entity_name=customer.name,
        ee_c_id=ee_c_id,
        status=STATUS_DISCREPANCY,
        last_error=" || ".join(reason_strings),
    )
    return CustomerOutcome(
        ee_c_id=ee_c_id,
        customer_docname=customer.name,
        status="Drift",
        operation="flagged",
        flag_reasons=reason_strings,
    )


def _diff_customer_payload_vs_docs(
    *, erpnext_fields: dict, customer: Any, excluded_fields: set[str]
) -> list[dict]:
    """Compare translated EE payload against the existing Customer +
    its linked Billing/Shipping Addresses. Returns structured diff
    dicts shaped like EasyEcom Item Map Drift Field child rows
    (field/erpnext_value/ee_value)."""
    diffs: list[dict] = []

    # Customer-level fields.
    for fld in CUSTOMER_DRIFT_COMPARABLE_CUSTOMER_FIELDS:
        if fld in excluded_fields:
            continue
        if fld not in erpnext_fields:
            continue
        ee_value = erpnext_fields.get(fld)
        en_value = customer.get(fld)
        if _values_differ(ee_value, en_value):
            diffs.append(
                {
                    "field": fld,
                    "erpnext_value": _stringify(en_value),
                    "ee_value": _stringify(ee_value),
                }
            )

    # Address-level fields. We look up both Billing and Shipping
    # Addresses once and reuse for all rows under each side.
    billing = _find_address_for_drift(customer.name, address_type="Billing")
    shipping = _find_address_for_drift(customer.name, address_type="Shipping")
    for label, payload_key, address_field in CUSTOMER_DRIFT_COMPARABLE_ADDRESS_FIELDS:
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


def _find_address_for_drift(
    customer_docname: str, *, address_type: str
) -> dict | None:
    """Read-only Address lookup for drift comparison. Mirrors the
    push-flow helper but with a different field list (drift cares
    about the user-visible fields)."""
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.city, a.pincode, a.state, a.country
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Customer'
          AND dl.link_name = %s
          AND a.address_type = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (customer_docname, address_type),
        as_dict=True,
    )
    return rows[0] if rows else None


def _values_differ(a: Any, b: Any) -> bool:
    """Drift comparison with None / "" / 0 leniency. Identical to
    §8d's _values_differ — copied (not imported) to keep the
    Customer flow's drift logic self-contained."""

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
    """Read FDE-marked exclude list off the Customer Map row. Reuses
    §8d's EasyEcom Item Map Exclude Field child DocType (the child
    schema is entity-agnostic — see Stage 1 inventory finding)."""
    rows = frappe.db.get_all(
        "EasyEcom Item Map Exclude Field",
        filters={"parent": map_name, "parenttype": "EasyEcom Customer Map"},
        fields=["field"],
    )
    return {r.field for r in rows if r.field}


def _upsert_drift_map_row(
    *,
    ee_c_id: str,
    erpnext_name: str | None,
    reason: str,
) -> str:
    """Create or update a Customer Map row in Drift status. Used for
    the EE-origin-new-customer case (no prior map row)."""
    existing = frappe.db.get_value(
        "EasyEcom Customer Map", {"ee_c_id": ee_c_id}, "name"
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Customer Map",
            existing,
            {"status": STATUS_DRIFT, "flag_reason": reason[:140]},
            update_modified=True,
        )
        return existing
    doc = frappe.new_doc("EasyEcom Customer Map")
    doc.update(
        {
            "ee_c_id": ee_c_id,
            "ee_customer_id": ee_c_id,
            "erpnext_doctype": "Customer" if erpnext_name else None,
            "erpnext_name": erpnext_name,
            "status": STATUS_DRIFT,
            "flag_reason": reason[:140],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _mark_existing_map_drift(map_name: str, *, reasons: list[str]) -> None:
    """Flip an existing map row to Drift status with the diff list as
    the reason. NEVER mutates the linked Customer — drift is read-only
    on the ERPNext side."""
    reason = " || ".join(reasons) if reasons else ""
    frappe.db.set_value(
        "EasyEcom Customer Map",
        map_name,
        {"status": STATUS_DRIFT, "flag_reason": reason[:140]},
        update_modified=True,
    )


def _record_drift_with_table(map_name: str, *, diffs: list[dict]) -> None:
    """Write the structured diff to the Customer Map's drift_fields
    child table + set status=Drift + drift_detected_at=now."""
    map_doc = frappe.get_doc("EasyEcom Customer Map", map_name)
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
    fixed upstream. Mirrors §8d EXACTLY: status is left untouched
    (FDE owns the Drift → Mapped transition via Dismiss). A Drift
    row whose diffs vanished still requires explicit FDE acknowledgement
    — that's the §8.2 audit-trail contract.
    """
    map_doc = frappe.get_doc("EasyEcom Customer Map", map_name)
    if not map_doc.get("drift_fields"):
        return
    map_doc.set("drift_fields", [])
    map_doc.drift_detected_at = None
    map_doc.save(ignore_permissions=True)


# Sync Record Discrepancy status — for the post-flip drift outcomes.
# §7.3: divergence is NOT failure; Drift outcome → Discrepancy SR.
from ecommerce_super.easyecom.flows._customer_sync_records import (  # noqa: E402
    STATUS_DISCREPANCY,
)


# ----- Drift resolution actions (whitelisted) -----


@frappe.whitelist()
def dismiss_drift(customer_map_name: str) -> dict[str, Any]:
    """FDE acknowledges the EE-side change is wrong or already-handled
    upstream. Returns the row to Mapped, clears the Drift Fields table,
    leaves the underlying Customer + Addresses untouched.

    The next pull will re-detect if the divergence still exists; to
    silence persistent intentional divergence, the FDE adds the field
    to ecs_drift_exclude_fields (audit #10) instead.

    Mirror of §8d's dismiss_drift (item_pull.py).
    """
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
    if not customer_map_name:
        return {"ok": False, "message": "customer_map_name required"}
    if not frappe.db.exists("EasyEcom Customer Map", customer_map_name):
        return {
            "ok": False,
            "message": f"Customer Map {customer_map_name!r} not found.",
        }
    doc = frappe.get_doc("EasyEcom Customer Map", customer_map_name)
    if doc.status != STATUS_DRIFT:
        return {
            "ok": False,
            "message": (
                f"Customer Map {customer_map_name!r} is not in Drift "
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
        "customer_map_name": customer_map_name,
        "status": STATUS_MAPPED,
    }


@frappe.whitelist()
def push_to_ee_for_drift(customer_map_name: str) -> dict[str, Any]:
    """FDE re-asserts ERPNext as SoT by pushing the current Customer
    state to EE, overwriting the EE-side divergence. Dispatches to the
    Stage 4 push (push_one_customer).

    On success the push flow writes a fresh snapshot + sets the map
    row to Mapped; the next pull will see no drift.

    NO 'Accept EE Value' counterpart — §8.2 post-flip contract is
    'ERPNext wins; EE-side novelty/edits are not adopted'. The only
    paths out of Drift are dismiss-and-wait or push-to-overwrite.
    """
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
    if not customer_map_name:
        return {"ok": False, "message": "customer_map_name required"}
    if not frappe.db.exists("EasyEcom Customer Map", customer_map_name):
        return {
            "ok": False,
            "message": f"Customer Map {customer_map_name!r} not found.",
        }
    doc = frappe.get_doc("EasyEcom Customer Map", customer_map_name)
    if doc.status != STATUS_DRIFT:
        return {
            "ok": False,
            "message": (
                f"Customer Map {customer_map_name!r} is not in Drift "
                f"status (current: {doc.status}); use the Customer-form "
                "Push button for non-drift pushes."
            ),
        }
    if not doc.erpnext_name:
        return {
            "ok": False,
            "message": (
                f"Customer Map {customer_map_name!r} has no linked "
                "Customer; create one in ERPNext first, then re-pull "
                "to set the link."
            ),
        }

    from ecommerce_super.easyecom.flows.customer_push import (
        push_one_customer,
    )

    try:
        outcome = push_one_customer(doc.erpnext_name)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=f"push_to_ee_for_drift failed: {customer_map_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "customer_map_name": customer_map_name,
        "operation": outcome.operation,
        "pushed": outcome.pushed,
        "ee_customer_id": outcome.ee_customer_id,
        "flag_reasons": outcome.flag_reasons,
    }


# ----- Scheduler entry -----


def scheduled_discover_customers() -> None:
    """§8e Stage 6 daily customer pull — wired in hooks.py
    scheduler_events at 05:30 IST (after §8d Items at 05:00).

    NOTE on delta semantics: unlike §8d Item Pull which uses
    Account.item_pull_last_updated_at as an updated_after delta
    cursor, /Wholesale/v2/UserManagement has NO updated_after filter
    (verified against the captured Harmony fixture — no cursor / no
    timestamp / no high-water field exists on the response shape).
    This is therefore a FULL pull every run. The wholesale customer
    master is small (Harmony's sample has 23) so a daily full-pull
    is acceptable; if a future deployment grows to N>>thousand
    customers, EE would need to expose an incremental endpoint or
    we'd need to switch to webhook-driven sync.

    Mode-aware: process_one_customer's phase gate decides whether the
    pulled customer is accepted-and-created (onboarding) or runs
    through drift detection (erpnext_mastered). No mode-specific
    branching here; same pull_customers call works for both phases.

    Quiet on no enabled Account (pre-onboarding state). Catches every
    exception so a transient EE outage doesn't fail the whole
    scheduler tick.
    """
    account_name = frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1}, "name", order_by="name asc"
    )
    if not account_name:
        return
    try:
        pull_customers()
    except Exception as exc:  # noqa: BLE001 — scheduler boundary
        frappe.log_error(
            title="EasyEcom scheduled customer discovery failed",
            message=f"{type(exc).__name__}: {exc}",
        )


# ----- Customer Group / Territory defaults -----


def _default_customer_group() -> str:
    """First available leaf Customer Group on the site, or a created
    one if there's nothing leaf-shaped. Onboarding-friendly default."""
    leaf = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    if leaf:
        return leaf
    # The site has only group-rows — create a leaf 'EE Wholesale' under
    # the root. (The customer test fixture path also does this; reuse
    # the same approach for prod-onboarding sanity.)
    if not frappe.db.exists("Customer Group", "All Customer Groups"):
        root = frappe.new_doc("Customer Group")
        root.update(
            {"customer_group_name": "All Customer Groups", "is_group": 1}
        )
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Customer Group")
    leaf_doc.update(
        {
            "customer_group_name": "EE Wholesale",
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name


def _default_territory() -> str:
    leaf = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if leaf:
        return leaf
    if not frappe.db.exists("Territory", "All Territories"):
        root = frappe.new_doc("Territory")
        root.update({"territory_name": "All Territories", "is_group": 1})
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Territory")
    leaf_doc.update(
        {
            "territory_name": "EE Wholesale",
            "parent_territory": "All Territories",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name
