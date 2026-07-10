"""§8e Stage 4 — EN→EE Customer push.

Mirrors §8d Item Push: separate ruleset, sparse-update + snapshot,
enqueue-via-facade for the batch sweep, Sync Records per push,
auto-push gated by an Account checkbox defaulting OFF.

The flow handles the EE Create-vs-Update asymmetry at the wire
boundary (the ruleset emits names + flat fields):
  - Create: needs `password` (random; EE's portal is a dummy nobody
    logs into), `country` (name), `billingStateId`+`dispatchStateId`
    (int — resolved from name via Stage 2 state_resolver),
    `taxIdentificationNumber` (GSTIN or 'URP'), `currency`,
    `billingPostalCode`+`dispatchPostalCode` (int). EE responds with
    `data.customerId` which we write back to the Customer Map row
    (Stage 1 schema stores it separately from `ee_c_id`).
  - Update: keys on `customerId` (from the map), accepts the same
    fields BUT state as NAME (not id), and NO password. Sparse payload
    + snapshot mirrors §8d Item Push.

Address sourcing: ERPNext stores Addresses in a separate DocType
linked via Address.links Dynamic Link. The flow reads the Customer's
linked Billing + Shipping Address rows and surfaces them as flat
billing_*/dispatch_* fields onto a transient dict the engine consumes.

Triggers:
  - Individual push (Customer.on_update hook) — fires only when
    `auto_push_customers_on_save=1` on the (single) enabled EasyEcom
    Account. Ping-pong guard: the pull flow sets `frappe.flags
    .easyecom_customer_pull_in_flight=True` while it's creating
    Customers, so the auto-push hook short-circuits during pull.
  - Batch sweep (`push_all_pending_customers`) — enqueues one Queue
    Job per candidate. Candidates: Customer with customer_type=Company
    AND no EasyEcom Customer Map row yet AND non-empty email_id AND
    enabled. (Non-Company customers aren't in §8e scope.)

NOT in scope for Stage 4 (parked per packet):
  - Pricing & discounts (b2bDiscountScheme, pricingGroupCode,
    invoiceSeriesCode, salesmanUserId, customerAttributes).
  - Order-driven B2B-buyer GSTIN-reuse — §11/§12 order flows call
    push_one_customer when they need to create a customer at order
    time; this module provides the create mechanism.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    WHOLESALE_CUSTOMER_CREATE,
    WHOLESALE_CUSTOMER_UPDATE,
)
from ecommerce_super.easyecom.customer.state_resolver import (
    resolve_country,
    resolve_state,
)
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)
from ecommerce_super.easyecom.flows._customer_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_customer_push_sync_record,
)
from ecommerce_super.easyecom.flows._isolation import for_each_record


CUSTOMER_PUSH_RULESET: str = "EasyEcom-Customer-Push"
PING_PONG_FLAG = "easyecom_customer_pull_in_flight"


# ----- Outcome types -----


PushOp = Literal["create", "update", "skipped", "flagged", "error"]


@dataclass
class PushOutcome:
    customer_docname: str
    operation: PushOp
    pushed: bool
    ee_customer_id: str | None = None
    flag_reasons: list[str] = field(default_factory=list)
    ee_payload: dict[str, Any] | None = None


@dataclass
class SweepOutcome:
    total_considered: int = 0
    create_count: int = 0
    update_count: int = 0
    flagged_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    outcomes: list[PushOutcome] = field(default_factory=list)


# ----- Individual push -----


def push_one_customer(
    customer_docname: str,
    *,
    client: EasyEcomClient | None = None,
    account: Any | None = None,
    executor: FieldMappingExecutor | None = None,
) -> PushOutcome:
    """Push one ERPNext Customer to EE.

    Map row exists with ee_customer_id → /Wholesale/UpdateCustomer.
    Map row exists with empty ee_customer_id, OR no map row → Create.
    Customer's customer_type != Company → skipped (out of 8e scope).
    """
    item = _identity_check(customer_docname)
    if item == "non_company":
        return PushOutcome(
            customer_docname=customer_docname,
            operation="skipped",
            pushed=False,
            flag_reasons=["customer_type is not Company — §8e is wholesale only"],
        )

    if client is None:
        client = EasyEcomClient()
    if executor is None:
        executor = FieldMappingExecutor(CUSTOMER_PUSH_RULESET)

    customer = frappe.get_doc("Customer", customer_docname)

    # Pre-build the transient "flat" dict the engine + the wire-boundary
    # code both need. Addresses sourced via Dynamic Link.
    transient = _gather_customer_payload_dict(customer)
    erpnext_payload = executor.push(transient)

    map_row = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer", "erpnext_name": customer_docname},
        ["name", "ee_c_id", "ee_customer_id"],
        as_dict=True,
    )

    has_existing_id = bool(map_row and map_row.ee_customer_id)
    if has_existing_id:
        return _do_update(
            customer=customer,
            map_row=map_row,
            erpnext_payload=erpnext_payload,
            client=client,
        )
    return _do_create(
        customer=customer,
        map_row=map_row,
        erpnext_payload=erpnext_payload,
        client=client,
    )


def _identity_check(customer_docname: str) -> str:
    """Cheap pre-check that doesn't require loading the full doc."""
    customer_type = frappe.db.get_value("Customer", customer_docname, "customer_type")
    return "non_company" if customer_type != "Company" else "ok"


def _gather_customer_payload_dict(customer: Any) -> dict[str, Any]:
    """Collect Customer + linked Billing/Shipping Address fields into
    a single flat dict the EasyEcom-Customer-Push ruleset can consume.

    The ruleset is a generic erpnext_path→easyecom_path mapper — it
    can't traverse Frappe's Address.links Dynamic Link itself. This
    helper does the lookup once and presents both addresses as
    billing_*/dispatch_* keys on the same dict.
    """
    out: dict[str, Any] = {
        "customer_name": customer.customer_name,
        "email_id": customer.email_id or "",
        "mobile_no": customer.mobile_no or "",
        "gstin": (customer.gstin or "").upper(),
        "gst_category": customer.gst_category or "",
        "default_currency": (customer.default_currency or "INR").upper(),
    }

    billing = _find_address(customer.name, address_type="Billing")
    # gh#60 — fall back to the Billing address when no Shipping-typed
    # address is linked. EE's CreateCustomer requires dispatchState +
    # dispatchPostalCode as mandatory; customers that ship to the
    # same address they bill to (the common SME / B2B-large case)
    # only carry a Billing-typed Address. Pre-fix that flagged with
    # "missing dispatchState name for CreateCustomer" despite the
    # billing address having all the required fields. The §10 Internal
    # Customer bootstrap (PR #68) sidesteps this by minting BOTH
    # address rows; this fallback makes the gate forgive customers
    # the FDE didn't create via that bootstrap.
    shipping = (
        _find_address(customer.name, address_type="Shipping")
        or billing
    )

    out.update(
        {
            "billing_street": (billing or {}).get("address_line1") or "",
            "billing_city": (billing or {}).get("city") or "",
            "billing_postal_code": (billing or {}).get("pincode") or "",
            "billing_state_name": (billing or {}).get("state") or "",
            "billing_country_name": (billing or {}).get("country") or "India",
            "dispatch_street": (shipping or {}).get("address_line1") or "",
            "dispatch_city": (shipping or {}).get("city") or "",
            "dispatch_postal_code": (shipping or {}).get("pincode") or "",
            "dispatch_state_name": (shipping or {}).get("state") or "",
            "dispatch_country_name": (shipping or {}).get("country") or "India",
        }
    )
    return out


def _find_address(customer_docname: str, *, address_type: str) -> dict | None:
    """Return the first Address of the given type linked to the Customer
    via Address.links Dynamic Link. None if no such Address exists."""
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


# ----- Create -----


def _do_create(
    *,
    customer: Any,
    map_row: dict | None,
    erpnext_payload: dict[str, Any],
    client: EasyEcomClient,
) -> PushOutcome:
    """Build the CreateCustomer payload (with manufactured password +
    resolved stateIds) and POST."""
    payload = dict(erpnext_payload)
    flag_reasons: list[str] = []

    # Manufactured: random password. EE's portal is dummy — nobody logs
    # in via password. Generate; don't flag the FDE.
    payload["password"] = secrets.token_urlsafe(16)

    # taxIdentificationNumber: URP substitution when Unregistered.
    if customer.gst_category == "Unregistered" and not payload.get(
        "taxIdentificationNumber"
    ):
        payload["taxIdentificationNumber"] = "URP"

    # Required-presence checks BEFORE state resolution (cheap wins first).
    # `contactNumber` is required by EE in practice (verified live against
    # Harmony 2026-05-27 — EE rejects with "Missing contact number at row 1"
    # when omitted) even though the packet listed it as optional.
    for field_, label in (
        ("companyName", "customer_name"),
        ("email", "email_id"),
        ("contactNumber", "mobile_no"),
        ("currency", "default_currency"),
    ):
        if not payload.get(field_):
            flag_reasons.append(f"missing required {label} for CreateCustomer")
    if not payload.get("taxIdentificationNumber"):
        flag_reasons.append(
            "missing taxIdentificationNumber (no gstin AND not Unregistered)"
        )

    # State resolution (name → int id) for Create's billingStateId /
    # dispatchStateId. The ruleset emits state NAMES under billingState /
    # dispatchState; we rename + resolve at the wire boundary.
    country_name = payload.get("country") or "India"
    country = resolve_country(country_name)
    if country is None:
        flag_reasons.append(
            f"country {country_name!r} not in EasyEcom Country cache "
            "(run Refresh States/Countries)"
        )
        country_id = 1  # India fallback for state resolution attempt; doesn't change the flag
    else:
        country_id = country.country_id

    for side, ee_name_key, ee_id_key in (
        ("billing", "billingState", "billingStateId"),
        ("dispatch", "dispatchState", "dispatchStateId"),
    ):
        state_name = payload.pop(ee_name_key, None)
        if not state_name:
            flag_reasons.append(
                f"missing {side}State name for CreateCustomer"
            )
            continue
        state_id = resolve_state(state_name, country_id=country_id)
        if state_id is None:
            flag_reasons.append(
                f"{side}State {state_name!r} not in EasyEcom State cache "
                "(run Refresh States/Countries)"
            )
            continue
        payload[ee_id_key] = state_id

    # Postal codes need to be present + numeric (the ruleset's
    # str_to_int handles the cast but only if the source is digit-only).
    for side, key, src in (
        ("billing", "billingPostalCode", "billing_postal_code"),
        ("dispatch", "dispatchPostalCode", "dispatch_postal_code"),
    ):
        if not payload.get(key):
            flag_reasons.append(
                f"missing or non-numeric {side}PostalCode for CreateCustomer"
            )

    if flag_reasons:
        # Don't build a broken EE payload. Customer Map row gets a
        # flag-not-pushed entry; the FDE fixes the source then re-pushes.
        _upsert_map_row_flagged(
            customer_docname=customer.name,
            existing_map=map_row,
            reasons=flag_reasons,
        )
        _write_push_sync_record(
            entity_name=customer.name,
            ee_c_id=str((map_row or {}).get("ee_c_id") or customer.name),
            status=STATUS_FAILED,
            last_error=" || ".join(flag_reasons),
        )
        return PushOutcome(
            customer_docname=customer.name,
            operation="flagged",
            pushed=False,
            flag_reasons=flag_reasons,
            ee_payload=payload,
        )

    # Strip None/empty before sending — EE rejects null fields.
    payload = {k: v for k, v in payload.items() if v not in (None, "")}

    response = client.post(WHOLESALE_CUSTOMER_CREATE, payload=payload)
    # EE's CreateCustomer response carries `data.c_id` (the read-side
    # identifier) — NOT `data.customerId` as the packet design assumed.
    # Verified live 2026-05-27 against Harmony: c_id from Create equals
    # the c_id returned by /Wholesale/v2/UserManagement reads for the
    # same customer; the packet's "customerId" is just the write-side
    # name for the same value. Decision #3 RESOLVED.
    customer_id = ((response or {}).get("data") or {}).get("c_id")
    if customer_id is None:
        # Fall back to legacy `customerId` key in case EE ever renames.
        customer_id = ((response or {}).get("data") or {}).get("customerId")
    if customer_id is None:
        reasons = [
            f"CreateCustomer returned no c_id (response: {response!r})"
        ]
        _upsert_map_row_flagged(
            customer_docname=customer.name,
            existing_map=map_row,
            reasons=reasons,
        )
        _write_push_sync_record(
            entity_name=customer.name,
            ee_c_id=str((map_row or {}).get("ee_c_id") or customer.name),
            status=STATUS_FAILED,
            last_error=" || ".join(reasons),
        )
        return PushOutcome(
            customer_docname=customer.name,
            operation="flagged",
            pushed=False,
            flag_reasons=reasons,
            ee_payload=payload,
        )

    customer_id_str = str(customer_id)
    # Writeback: customerId on the map row. Stage 3 stored ee_c_id =
    # ee_customer_id as a placeholder; Stage 4's first Create on this
    # row REVEALS the actual customerId. If it equals the c_id we got
    # at pull time, the assumption holds; if not, the map row now
    # carries both and downstream uses ee_customer_id for updates.
    _upsert_map_row_after_create(
        customer_docname=customer.name,
        existing_map=map_row,
        ee_customer_id=customer_id_str,
    )
    _save_push_snapshot(customer_docname=customer.name, payload=payload)
    _write_push_sync_record(
        entity_name=customer.name,
        ee_c_id=customer_id_str,
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return PushOutcome(
        customer_docname=customer.name,
        operation="create",
        pushed=True,
        ee_customer_id=customer_id_str,
        ee_payload=payload,
    )


# ----- Update -----


def _do_update(
    *,
    customer: Any,
    map_row: dict,
    erpnext_payload: dict[str, Any],
    client: EasyEcomClient,
) -> PushOutcome:
    """Build the UpdateCustomer payload (state as NAME, no password)
    and POST sparse diff vs the prior snapshot."""
    full_payload = dict(erpnext_payload)
    # State stays as NAME on update. The flow doesn't resolve to int.
    # Strip any Create-only id fields if the ruleset ever produces them.
    full_payload.pop("password", None)
    full_payload.pop("billingStateId", None)
    full_payload.pop("dispatchStateId", None)

    # taxIdentificationNumber URP substitution (same as create).
    if customer.gst_category == "Unregistered" and not full_payload.get(
        "taxIdentificationNumber"
    ):
        full_payload["taxIdentificationNumber"] = "URP"

    full_payload["customerId"] = int(map_row["ee_customer_id"])

    sparse = _build_sparse_update_payload(
        full_payload=full_payload, customer_docname=customer.name
    )

    # Strip None/empty.
    sparse = {k: v for k, v in sparse.items() if v not in (None, "")}

    client.post(WHOLESALE_CUSTOMER_UPDATE, payload=sparse)

    # gh#144: heal a stale "flagged-<docname>" placeholder in ee_c_id
    # left over from the pre-fix _upsert_map_row_after_create path.
    # We already know the real EE c_id (== ee_customer_id, verified
    # 2026-05-27). Only touch rows whose ee_c_id still looks like a
    # placeholder — real c_ids are numeric strings and never start
    # with "flagged-".
    current_ee_c_id = str(map_row.get("ee_c_id") or "")
    real_customer_id = str(map_row["ee_customer_id"])
    if current_ee_c_id.startswith("flagged-") and real_customer_id:
        frappe.db.set_value(
            "EasyEcom Customer Map",
            map_row["name"],
            "ee_c_id",
            real_customer_id,
            update_modified=False,
        )
        map_row["ee_c_id"] = real_customer_id

    _save_push_snapshot(customer_docname=customer.name, payload=full_payload)
    _write_push_sync_record(
        entity_name=customer.name,
        ee_c_id=str(map_row["ee_c_id"]),
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return PushOutcome(
        customer_docname=customer.name,
        operation="update",
        pushed=True,
        ee_customer_id=str(map_row["ee_customer_id"]),
        ee_payload=sparse,
    )


def _build_sparse_update_payload(
    *, full_payload: dict, customer_docname: str
) -> dict:
    """Read the prior push snapshot from the Customer Map; return
    customerId + changed fields only. Mirrors §8d Item Push's sparse-
    update builder."""
    snap_text = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer", "erpnext_name": customer_docname},
        "ecs_last_pushed_payload",
    )
    if not snap_text:
        return dict(full_payload)
    try:
        prior = json.loads(snap_text)
    except Exception:  # noqa: BLE001
        return dict(full_payload)
    if not isinstance(prior, dict):
        return dict(full_payload)

    delta = {"customerId": full_payload.get("customerId")}
    for k, v in full_payload.items():
        if k == "customerId":
            continue
        if prior.get(k) != v:
            delta[k] = v
    return delta


def _save_push_snapshot(*, customer_docname: str, payload: dict) -> None:
    """Persist the just-sent full payload on the Customer Map."""
    map_name = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer", "erpnext_name": customer_docname},
        "name",
    )
    if not map_name:
        return
    frappe.db.set_value(
        "EasyEcom Customer Map",
        map_name,
        "ecs_last_pushed_payload",
        json.dumps(payload, sort_keys=True, default=str),
        update_modified=False,
    )


# ----- Map row helpers -----


def _upsert_map_row_after_create(
    *,
    customer_docname: str,
    existing_map: dict | None,
    ee_customer_id: str,
) -> str:
    """After a successful CreateCustomer, ensure the Customer Map row
    carries the returned customerId. Stage 3 may have left a map row
    with ee_customer_id = ee_c_id (placeholder); overwrite cleanly."""
    if existing_map:
        # gh#144: also overwrite ee_c_id. Pre-fix, this write only
        # updated ee_customer_id — so an existing Flagged-Not-Created
        # row that had `ee_c_id = "flagged-<docname>"` as a
        # unique-constraint placeholder kept the placeholder forever,
        # and the inbound resolver (invoice_mirror._resolve_customer)
        # queries by ee_c_id → never matched. Both ids are the SAME
        # value on EE (per the 2026-05-27 verification comment near
        # customer_id extraction above), so overwriting is safe.
        frappe.db.set_value(
            "EasyEcom Customer Map",
            existing_map["name"],
            {
                "ee_c_id": ee_customer_id,
                "ee_customer_id": ee_customer_id,
                "status": "Mapped",
                "flag_reason": "",
            },
            update_modified=True,
        )
        return existing_map["name"]
    doc = frappe.new_doc("EasyEcom Customer Map")
    doc.update(
        {
            # No EE c_id from a pull yet (this is an EN→EE create with
            # no prior pull). Set both ids to the returned customerId
            # so the map row identifies the customer on both axes;
            # if a future pull comes through with the same c_id, the
            # values will line up.
            "ee_c_id": ee_customer_id,
            "ee_customer_id": ee_customer_id,
            "erpnext_doctype": "Customer",
            "erpnext_name": customer_docname,
            "status": "Mapped",
            "flag_reason": "",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_map_row_flagged(
    *,
    customer_docname: str,
    existing_map: dict | None,
    reasons: list[str],
) -> str:
    """Flag-not-pushed: Customer exists in ERPNext but couldn't be
    pushed. Create / update the map row with status =
    Flagged-Not-Created (no EE id yet) so the FDE worklist surfaces it.
    """
    flag_reason = " || ".join(reasons)[:140] if reasons else ""
    if existing_map:
        frappe.db.set_value(
            "EasyEcom Customer Map",
            existing_map["name"],
            {"status": "Flagged-Not-Created", "flag_reason": flag_reason},
            update_modified=True,
        )
        return existing_map["name"]
    # No prior map. Use customer_docname as the placeholder ee_c_id —
    # ee_c_id is reqd+unique. A future Discover pull may override.
    doc = frappe.new_doc("EasyEcom Customer Map")
    doc.update(
        {
            "ee_c_id": f"flagged-{customer_docname}",
            "ee_customer_id": "",
            "erpnext_doctype": "Customer",
            "erpnext_name": customer_docname,
            "status": "Flagged-Not-Created",
            "flag_reason": flag_reason,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _write_push_sync_record(
    *,
    entity_name: str,
    ee_c_id: str,
    status: str,
    last_error: str | None,
) -> None:
    """Best-effort SR write."""
    try:
        write_customer_push_sync_record(
            entity_name=entity_name,
            ee_c_id=ee_c_id,
            status=status,
            last_error=last_error,
        )
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=f"customer_push SR write failed for {entity_name}",
            message=f"{type(exc).__name__}: {exc}",
        )


# ----- Batch sweep -----


def candidate_customers_for_sweep(limit: int | None = None) -> list[str]:
    """Customers eligible for the onboarding push sweep.

    Policy (Stage 4):
      - customer_type = Company (§8e is wholesale)
      - disabled = 0
      - email_id is non-empty (CreateCustomer mandatory; pre-filtering
        skips the obvious 'missing email' flag-not-pushed cases at the
        query level so the sweep stays efficient)
      - no EasyEcom Customer Map row exists yet (re-push of existing
        mapped customers goes through the individual-push trigger,
        not the sweep)

    Items already in the system with a Map row but no ee_customer_id
    (e.g. Flagged-Not-Created from a prior push) are NOT swept —
    they're FDE-visible failures; re-attempting via sweep would just
    re-fail until the FDE fixes the source.
    """
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    rows = frappe.db.sql(
        f"""
        SELECT c.name
        FROM `tabCustomer` c
        LEFT JOIN `tabEasyEcom Customer Map` m
            ON m.erpnext_doctype = 'Customer'
            AND m.erpnext_name = c.name
        WHERE c.customer_type = 'Company'
          AND c.disabled = 0
          AND c.email_id IS NOT NULL AND c.email_id != ''
          AND m.name IS NULL
        ORDER BY c.creation ASC
        {limit_clause}
        """,
        as_dict=True,
    )
    return [r.name for r in rows]


def push_all_pending(
    *,
    account: Any,
    client: EasyEcomClient | None = None,
    limit: int | None = None,
) -> SweepOutcome:
    """Batch sweep — INLINE variant (mostly for tests). Production uses
    enqueue_push_all_pending to spread the work across queue workers
    (one Queue Job per customer)."""
    if client is None:
        client = EasyEcomClient()
    executor = FieldMappingExecutor(CUSTOMER_PUSH_RULESET)

    codes = candidate_customers_for_sweep(limit=limit)
    outcome = SweepOutcome(total_considered=len(codes))

    def _handle(customer_docname: str) -> None:
        po = push_one_customer(
            customer_docname,
            client=client,
            account=account,
            executor=executor,
        )
        outcome.outcomes.append(po)

    def _on_failure(customer_docname: str, exc: BaseException) -> None:
        outcome.outcomes.append(
            PushOutcome(
                customer_docname=customer_docname,
                operation="error",
                pushed=False,
                flag_reasons=[f"{type(exc).__name__}: {exc}"],
            )
        )
        frappe.log_error(
            title=f"customer_push sweep failed: {customer_docname}",
            message=f"{type(exc).__name__}: {exc}",
        )

    for_each_record(
        codes,
        handler=_handle,
        on_failure=_on_failure,
        flow_name="customer_push_sweep",
    )

    for po in outcome.outcomes:
        if po.operation == "create":
            outcome.create_count += 1
        elif po.operation == "update":
            outcome.update_count += 1
        elif po.operation == "skipped":
            outcome.skipped_count += 1
        elif po.operation == "error":
            outcome.error_count += 1
        else:  # flagged
            outcome.flagged_count += 1
    return outcome


def enqueue_push_all_pending(
    *, account_name: str, limit: int | None = None
) -> dict[str, Any]:
    """Production batch entry — enqueues one push job per candidate via
    the §6.3.1 facade. Returns immediately with counts AND a per-failure
    diagnostic so the FDE isn't left guessing why a candidate didn't
    enqueue (gh#27 sibling fix — customer push had the same bug as
    supplier push).

    Prior versions called enqueue_easyecom_job with stale kwargs
    (`method=` / `kwargs=`) and without the required `company` and
    `idempotency_key` arguments. Every call raised TypeError or
    ValueError, was caught by the broad `except`, and logged silently
    to Error Log — surfacing only as "Considered: N, Enqueued: 0" in
    the FDE button response with no clue what went wrong.
    """
    from ecommerce_super.easyecom.queue import enqueue_easyecom_job

    codes = candidate_customers_for_sweep(limit=limit)
    enqueued: list[str] = []
    failures: list[dict[str, str]] = []
    company = _company_for_customer_push()
    for customer_docname in codes:
        try:
            payload = {
                "customer_docname": customer_docname,
                "account_name": account_name,
            }
            idem_key = _customer_push_queue_idempotency_key(
                customer_docname=customer_docname,
                account_name=account_name,
                company=company,
            )
            qj_name = enqueue_easyecom_job(
                job_type="Customer Push",
                company=company,
                target_doctype="Customer",
                target_name=customer_docname,
                payload=payload,
                idempotency_key=idem_key,
            )
            enqueued.append(qj_name)
        except Exception as exc:  # noqa: BLE001 — surface every failure
            error_summary = f"{type(exc).__name__}: {exc}"
            failures.append(
                {"customer_docname": customer_docname, "error": error_summary}
            )
            frappe.log_error(
                title=f"enqueue_customer_push failed for {customer_docname}",
                message=error_summary,
            )
    return {
        "total_considered": len(codes),
        "enqueued_count": len(enqueued),
        "queue_job_names_sample": enqueued[:10],
        "failures_sample": failures[:10],
        "failed_count": len(failures),
    }


def customer_push_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['Customer Push'] dispatch — workers.execute_job
    calls this with the loaded Queue Job doc (gh#27).

    Reads target_name (customer_docname) + payload.account_name and
    invokes push_one_customer. Raises on EE error so the worker's
    retry/back-off disposition fires per §6.3.8.
    """
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    customer_docname = qj.target_name or payload.get("customer_docname")
    account_name = payload.get("account_name")
    if not customer_docname or not account_name:
        raise ValueError(
            f"Customer Push job {qj.name} missing customer_docname or "
            "account_name in payload"
        )
    account = frappe.get_doc("EasyEcom Account", account_name)
    client = EasyEcomClient()
    push_one_customer(customer_docname, client=client, account=account)


# ----- Doc-event hook (auto-push) -----


def enqueue_on_customer_change(
    doc: Any, method: str | None = None, **_kwargs
) -> None:
    """Customer.on_update hook. Fires only when the account has
    auto_push_customers_on_save=1. Ping-pong guard: skip when the pull
    flow is mid-flight (it sets frappe.flags.easyecom_customer_pull_in_flight).
    """
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return  # avoid pull-then-push echo
    if doc.doctype != "Customer":
        return
    if doc.customer_type != "Company":
        return  # §8e is wholesale only
    if doc.disabled:
        return

    account_name = _account_with_auto_push_enabled()
    if not account_name:
        return

    # Skip if Customer was just-created BY the pull (extra ping-pong
    # belt-and-braces; the flag handles the common case but a pull that
    # commits + a separate Customer.save in the same RQ worker might
    # not see the flag).
    map_exists = frappe.db.exists(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer", "erpnext_name": doc.name},
    )
    # If a map row exists with ee_customer_id, it's an update path —
    # OK to enqueue. If no map row, it's a create path — also OK.
    # Skip only when the doc was just pulled (no good signal here other
    # than the flag we already checked above).
    _ = map_exists

    from ecommerce_super.easyecom.queue import enqueue_easyecom_job

    company = _company_for_customer_push()
    enqueue_easyecom_job(
        job_type="Customer Push",
        company=company,
        target_doctype="Customer",
        target_name=doc.name,
        payload={"customer_docname": doc.name, "account_name": account_name},
        idempotency_key=_customer_push_queue_idempotency_key(
            customer_docname=doc.name,
            account_name=account_name,
            company=company,
        ),
    )


def _account_with_auto_push_enabled() -> str | None:
    """First enabled EasyEcom Account with auto_push_customers_on_save=1.
    None when no account opts in (auto-push is off everywhere)."""
    return frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1, "auto_push_customers_on_save": 1},
        "name",
    )


def _company_for_customer_push() -> str:
    """Pick a Company for the Queue Job row (gh#27 sibling fix).

    §8e customers are account-wide (not per-Company), but EasyEcom
    Queue Job's `company` field is a required Link to Company. Mirror
    the item_push helper: prefer the first enabled EasyEcom Company
    Settings, fall back to the first Company on the site, raise if no
    Company exists (pre-onboarding state).
    """
    row = frappe.db.get_value(
        "EasyEcom Company Settings",
        {"enabled": 1},
        "company",
        order_by="company asc",
    )
    if row:
        return row
    fallback = frappe.db.get_value(
        "Company", filters={}, fieldname="name", order_by="creation asc"
    )
    if not fallback:
        raise RuntimeError(
            "Cannot enqueue Customer Push: no Company exists on this site "
            "(pre-onboarding state). Create a Company first."
        )
    return fallback


def _customer_push_queue_idempotency_key(
    *, customer_docname: str, account_name: str, company: str
) -> str:
    """Queue Job idempotency key for Customer Push (gh#27 sibling fix).

    Dedupes Frappe-side queue work: a duplicate hook fire (rapid
    double-save on the Customer form, or the batch sweep racing with
    auto-push) collapses to one Queue Job. The inner EE call has its
    OWN idempotency_key built by `customer_push_key` inside
    push_one_customer.
    """
    from ecommerce_super.easyecom.utils.hashing import sha256_idempotency

    return sha256_idempotency(
        "customer_push_queue", company, customer_docname, account_name, "v1"
    )
