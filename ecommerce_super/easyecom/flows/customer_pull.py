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

    Sets `frappe.flags.easyecom_customer_pull_in_flight=True` while
    processing — the Stage 4 push hook checks this flag to avoid
    re-pushing a customer that was just pulled (ping-pong guard).
    """
    from ecommerce_super.easyecom.flows.customer_push import PING_PONG_FLAG

    executor = FieldMappingExecutor(CUSTOMER_PULL_RULESET)
    aggregate = PullOutcome(total=len(rows))
    frappe.flags.__setattr__(PING_PONG_FLAG, True)

    def _handle(row: dict) -> None:
        outcome = process_one_customer(row, executor=executor)
        aggregate.outcomes.append(outcome)
        if outcome.status == "Mapped" and outcome.operation == "created":
            aggregate.created += 1
        elif outcome.operation == "skipped":
            aggregate.skipped += 1
        if outcome.status == "Created-Flagged":
            aggregate.created_flagged += 1
        elif outcome.status == "Flagged-Not-Created":
            aggregate.flagged_not_created += 1

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
    row: dict, *, executor: FieldMappingExecutor
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
