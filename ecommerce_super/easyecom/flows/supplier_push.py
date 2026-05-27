"""§8f Stage 4 — EN→EE Supplier push.

Mirrors §8e Customer Push: separate ruleset, sparse-update + snapshot,
enqueue-via-facade for the batch sweep, Sync Records per push, auto-
push gated by an Account checkbox defaulting OFF.

§8.3 specifics vs §8e:
  - **CreateVendor returns BOTH ids in data**. `data.vendor_id` echoes
    the input `vendorCode` (the WRITE key); `data.vendor_c_id` is the
    NEWLY-assigned READ key (int). Both are written back to the
    Supplier Map after a successful create — so a Supplier that was
    born in ERPNext (no prior pull) ends up with the same dual-id
    population as one that was pulled from EE.
  - **EE field naming gotcha** — the mandatory tax field on
    CreateVendor is `taxIdentificationNum` (NOT
    `taxIdentificationNumber` as the EE docs / packet docs said). Live
    finding 2026-05-27.
  - **State is NAME everywhere on push.** No name→int resolution for
    `state` (unlike §8e Customer Push's `billingStateId`/`dispatchStateId`).
    Both Create and Update send the state name; EE resolves internally.
  - **No password** on Create. Vendors aren't portal logins (unlike
    customers).
  - **Single-address payload** on CreateVendor. The EE create wire
    accepts only one address (street/city/state/zip/country top-level);
    the FLOW prefers Billing → Shipping fallback.

UpdateVendor:
  - Request body keys `vendorId` to the WRITE key (vendor_code,
    string).
  - Response returns `data.vendorId` as the READ key (vendor_c_id,
    int). Confirmed live; resolves the §8f packet 58614 puzzle.
  - Sparse-diff against the snapshot stored on the Supplier Map
    (mirrors §8d Item Push and §8e Customer Push).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    WHOLESALE_VENDOR_CREATE,
    WHOLESALE_VENDOR_UPDATE,
)
from ecommerce_super.easyecom.customer.state_resolver import resolve_country
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)
from ecommerce_super.easyecom.flows._isolation import for_each_record
from ecommerce_super.easyecom.flows._supplier_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_supplier_push_sync_record,
)


SUPPLIER_PUSH_RULESET: str = "EasyEcom-Supplier-Push"
PING_PONG_FLAG = "easyecom_supplier_pull_in_flight"


PushOp = Literal["create", "update", "skipped", "flagged", "error"]


@dataclass
class PushOutcome:
    supplier_docname: str
    operation: PushOp
    pushed: bool
    ee_vendor_c_id: str | None = None  # the READ key (vendor_c_id)
    ee_vendor_id: str | None = None  # the WRITE key (vendor_code)
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


def push_one_supplier(
    supplier_docname: str,
    *,
    client: EasyEcomClient | None = None,
    account: Any | None = None,
    executor: FieldMappingExecutor | None = None,
) -> PushOutcome:
    """Push one ERPNext Supplier to EE.

    Map row exists with ee_vendor_id → /wms/UpdateVendor.
    Map row absent OR ee_vendor_id empty → /wms/CreateVendor.
    supplier_type != Company → skipped (§8f is wholesale).
    """
    if _identity_check(supplier_docname) == "non_company":
        return PushOutcome(
            supplier_docname=supplier_docname,
            operation="skipped",
            pushed=False,
            flag_reasons=["supplier_type is not Company — §8f is wholesale only"],
        )

    if client is None:
        client = EasyEcomClient()
    if executor is None:
        executor = FieldMappingExecutor(SUPPLIER_PUSH_RULESET)

    supplier = frappe.get_doc("Supplier", supplier_docname)

    transient = _gather_supplier_payload_dict(supplier)
    erpnext_payload = executor.push(transient)

    map_row = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": supplier_docname},
        ["name", "ee_vendor_c_id", "ee_vendor_id"],
        as_dict=True,
    )

    has_existing_write_key = bool(map_row and map_row.ee_vendor_id)
    if has_existing_write_key:
        return _do_update(
            supplier=supplier,
            map_row=map_row,
            erpnext_payload=erpnext_payload,
            client=client,
        )
    return _do_create(
        supplier=supplier,
        map_row=map_row,
        erpnext_payload=erpnext_payload,
        client=client,
    )


def _identity_check(supplier_docname: str) -> str:
    """Cheap pre-check that doesn't require loading the full doc."""
    supplier_type = frappe.db.get_value(
        "Supplier", supplier_docname, "supplier_type"
    )
    return "non_company" if supplier_type != "Company" else "ok"


def _gather_supplier_payload_dict(supplier: Any) -> dict[str, Any]:
    """Collect Supplier + linked Address fields into a flat dict the
    ruleset can consume.

    Supplier.email_id / .mobile_no are read-only fetched from the
    linked Contact; we read them via the source Contact when present
    and fall back to the Supplier fields when not.
    """
    out: dict[str, Any] = {
        "supplier_name": supplier.supplier_name,
        "gstin": (supplier.gstin or "").upper(),
        "pan": (supplier.pan or "").upper(),
        "gst_category": supplier.gst_category or "",
        "default_currency": (supplier.default_currency or "INR").upper(),
    }

    # Manufactured: vendor_code derived from supplier name (sanitised).
    out["ee_vendor_id_seed"] = _sanitise_vendor_code(supplier.name)

    # Name split (fallback when no Contact link).
    fn, ln = _split_supplier_name(supplier.supplier_name)
    out["firstname"] = fn
    out["lastname"] = ln

    # Primary email / phone — Supplier has these fetched from the
    # linked Contact. If the doc has them resolved, use them; otherwise
    # query the linked Contact directly.
    primary_email = (
        supplier.get("email_id")
        or _primary_contact_field(supplier.name, "email_id")
        or ""
    )
    primary_phone = (
        supplier.get("mobile_no")
        or _primary_contact_field(supplier.name, "mobile_no")
        or _primary_contact_field(supplier.name, "phone")
        or ""
    )
    out["supplier_primary_email"] = primary_email.lower()
    out["supplier_primary_contact_number"] = primary_phone

    # Single-address payload — prefer Billing, fall back to Shipping.
    billing = _find_address(supplier.name, address_type="Billing")
    shipping = _find_address(supplier.name, address_type="Shipping")
    addr = billing or shipping or {}
    out.update(
        {
            "billing_street": addr.get("address_line1") or "",
            "billing_city": addr.get("city") or "",
            "billing_postal_code": addr.get("pincode") or "",
            "billing_state_name": addr.get("state") or "",
            "billing_country_name": (
                addr.get("country") or supplier.country or "India"
            ),
        }
    )

    # Lead-time fields (parked custom fields — Supplier doesn't have
    # them natively; if a client has added them via custom_field, the
    # FLOW reads them off the doc, otherwise omits).
    out["prep_days"] = supplier.get("prep_days") or ""
    out["shipment_intransit_days"] = supplier.get("shipment_intransit_days") or ""

    return out


_VENDOR_CODE_SANITISE_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _sanitise_vendor_code(supplier_name: str) -> str:
    """EE's vendor_code is a free-text string but practical limits +
    portal display friendliness mean we strip non-ASCII-alnum chars.
    Caps length at 40 to stay safely inside any undocumented EE limit."""
    if not supplier_name:
        return ""
    s = _VENDOR_CODE_SANITISE_RE.sub("-", supplier_name).strip("-")
    return s[:40] or supplier_name[:40]


def _split_supplier_name(supplier_name: str | None) -> tuple[str, str]:
    """Best-effort first/last split for EE's firstName/lastName fields.
    Just splits on first whitespace — sufficient for our use case where
    EE's display only ever joins them back together."""
    if not supplier_name:
        return ("", "")
    parts = supplier_name.strip().split(None, 1)
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[1])


def _primary_contact_field(
    supplier_docname: str, fieldname: str
) -> str | None:
    """Return the named field from the primary Contact linked to this
    Supplier via Dynamic Link."""
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


def _find_address(supplier_docname: str, *, address_type: str) -> dict | None:
    """Return the first Address of the given type linked to the
    Supplier via Address.links Dynamic Link."""
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


# ----- Create -----


def _do_create(
    *,
    supplier: Any,
    map_row: dict | None,
    erpnext_payload: dict[str, Any],
    client: EasyEcomClient,
) -> PushOutcome:
    """Build the CreateVendor payload (single-address, name-state, no
    password) and POST. Both ids in the response (`data.vendor_id` +
    `data.vendor_c_id`) are captured to the Supplier Map."""
    payload = dict(erpnext_payload)
    flag_reasons: list[str] = []

    # Country-aware tax gating: foreign supplier doesn't need GSTIN/PAN.
    is_overseas = (supplier.gst_category or "").lower() == "overseas"
    country_name = payload.get("country") or supplier.country or "India"
    classified = _classify_country(country_name)

    if not is_overseas and classified != "foreign":
        # Indian path: GSTIN + PAN required. URP substitution for
        # Unregistered.
        if (supplier.gst_category or "").lower() == "unregistered" and not payload.get(
            "taxIdentificationNum"
        ):
            payload["taxIdentificationNum"] = "URP"
        # If GSTIN present but PAN missing — auto-extract.
        gstin = (payload.get("taxIdentificationNum") or "").strip()
        if gstin and gstin != "URP" and not payload.get("PAN"):
            extracted = gstin[2:12].upper()
            payload["PAN"] = extracted
    else:
        # Foreign path: GSTIN/PAN optional. Drop them if blank rather
        # than sending empty strings.
        if not (payload.get("taxIdentificationNum") or "").strip():
            payload.pop("taxIdentificationNum", None)
        if not (payload.get("PAN") or "").strip():
            payload.pop("PAN", None)

    # Required-presence checks BEFORE sending. Indian path requires
    # all 7 mandatories; foreign drops the tax pair.
    required_keys: list[tuple[str, str]]
    if is_overseas or classified == "foreign":
        required_keys = [
            ("companyName", "supplier_name"),
            ("emailId", "primary email"),
            ("state", "billing state name"),
            ("country", "billing country"),
            ("currency", "default_currency"),
            ("zip", "billing zip"),
        ]
    else:
        required_keys = [
            ("companyName", "supplier_name"),
            ("emailId", "primary email"),
            ("state", "billing state name"),
            ("country", "billing country"),
            ("currency", "default_currency"),
            ("zip", "billing zip"),
            ("taxIdentificationNum", "GSTIN"),
            ("PAN", "PAN"),
        ]

    for ee_field, label in required_keys:
        if not str(payload.get(ee_field) or "").strip():
            flag_reasons.append(
                f"missing required {label} for CreateVendor"
            )

    # Country must be in the Stage 2 cache (so it canonicalises).
    if not flag_reasons and classified == "unknown":
        flag_reasons.append(
            f"country {country_name!r} not in EasyEcom Country cache "
            "(run Refresh States/Countries)"
        )

    if flag_reasons:
        _upsert_map_row_flagged(
            supplier_docname=supplier.name,
            existing_map=map_row,
            reasons=flag_reasons,
        )
        _write_push_sync_record(
            entity_name=supplier.name,
            ee_vendor_c_id=str(
                (map_row or {}).get("ee_vendor_c_id") or supplier.name
            ),
            status=STATUS_FAILED,
            last_error=" || ".join(flag_reasons),
        )
        return PushOutcome(
            supplier_docname=supplier.name,
            operation="flagged",
            pushed=False,
            flag_reasons=flag_reasons,
            ee_payload=payload,
        )

    # Strip None/empty before sending.
    payload = {k: v for k, v in payload.items() if v not in (None, "")}

    response = client.post(WHOLESALE_VENDOR_CREATE, payload=payload)
    data = (response or {}).get("data") or {}

    # CreateVendor returns BOTH ids:
    #   data.vendor_id    = vendorCode echo (the WRITE key, string)
    #   data.vendor_c_id  = newly-assigned read key (int)
    # We capture both. (Live finding 2026-05-27 — the packet's earlier
    # design assumed only vendor_id came back; vendor_c_id arrived
    # too.)
    new_vendor_id = str(data.get("vendor_id") or "").strip() or None
    new_vendor_c_id_raw = data.get("vendor_c_id")
    new_vendor_c_id = (
        str(new_vendor_c_id_raw).strip()
        if new_vendor_c_id_raw is not None
        else None
    )

    if not new_vendor_id and not new_vendor_c_id:
        reasons = [
            f"CreateVendor returned neither vendor_id nor vendor_c_id (response: {response!r})"
        ]
        _upsert_map_row_flagged(
            supplier_docname=supplier.name,
            existing_map=map_row,
            reasons=reasons,
        )
        _write_push_sync_record(
            entity_name=supplier.name,
            ee_vendor_c_id=str(supplier.name),
            status=STATUS_FAILED,
            last_error=" || ".join(reasons),
        )
        return PushOutcome(
            supplier_docname=supplier.name,
            operation="flagged",
            pushed=False,
            flag_reasons=reasons,
            ee_payload=payload,
        )

    _upsert_map_row_after_create(
        supplier_docname=supplier.name,
        existing_map=map_row,
        ee_vendor_c_id=new_vendor_c_id,
        ee_vendor_id=new_vendor_id,
    )
    _save_push_snapshot(supplier_docname=supplier.name, payload=payload)
    _write_push_sync_record(
        entity_name=supplier.name,
        ee_vendor_c_id=str(new_vendor_c_id or new_vendor_id),
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return PushOutcome(
        supplier_docname=supplier.name,
        operation="create",
        pushed=True,
        ee_vendor_c_id=new_vendor_c_id,
        ee_vendor_id=new_vendor_id,
        ee_payload=payload,
    )


# ----- Update -----


def _do_update(
    *,
    supplier: Any,
    map_row: dict,
    erpnext_payload: dict[str, Any],
    client: EasyEcomClient,
) -> PushOutcome:
    """Build the UpdateVendor payload (state as NAME, no password, no
    vendorCode — vendorId is the write key) and POST sparse diff vs
    snapshot. Capture data.vendorId from response as the READ key
    (vendor_c_id) — useful when the create-time row only had vendor_id."""
    full_payload = dict(erpnext_payload)
    # Strip create-only fields.
    full_payload.pop("vendorCode", None)
    full_payload["vendorId"] = map_row["ee_vendor_id"]  # WRITE key

    # Foreign vs Indian tax handling — same as create.
    is_overseas = (supplier.gst_category or "").lower() == "overseas"
    country_name = full_payload.get("country") or supplier.country or "India"
    classified = _classify_country(country_name)

    if not is_overseas and classified != "foreign":
        if (supplier.gst_category or "").lower() == "unregistered" and not full_payload.get(
            "taxIdentificationNum"
        ):
            full_payload["taxIdentificationNum"] = "URP"
        gstin = (full_payload.get("taxIdentificationNum") or "").strip()
        if gstin and gstin != "URP" and not full_payload.get("PAN"):
            full_payload["PAN"] = gstin[2:12].upper()
    else:
        if not (full_payload.get("taxIdentificationNum") or "").strip():
            full_payload.pop("taxIdentificationNum", None)
        if not (full_payload.get("PAN") or "").strip():
            full_payload.pop("PAN", None)

    sparse = _build_sparse_update_payload(
        full_payload=full_payload, supplier_docname=supplier.name
    )
    sparse = {k: v for k, v in sparse.items() if v not in (None, "")}

    response = client.post(WHOLESALE_VENDOR_UPDATE, payload=sparse)

    # UpdateVendor's response data.vendorId is the READ key (vendor_c_id).
    # Useful when the original create didn't capture it (older client
    # rows might be missing); refresh-on-update closes that gap.
    data = (response or {}).get("data") or {}
    response_read_key = data.get("vendorId")
    if response_read_key is not None and not map_row.get("ee_vendor_c_id"):
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            map_row["name"],
            "ee_vendor_c_id",
            str(response_read_key),
            update_modified=True,
        )

    _save_push_snapshot(supplier_docname=supplier.name, payload=full_payload)
    _write_push_sync_record(
        entity_name=supplier.name,
        ee_vendor_c_id=str(
            map_row.get("ee_vendor_c_id") or response_read_key or map_row.get("ee_vendor_id")
        ),
        status=STATUS_SUCCESS,
        last_error=None,
    )
    return PushOutcome(
        supplier_docname=supplier.name,
        operation="update",
        pushed=True,
        ee_vendor_c_id=(
            str(response_read_key)
            if response_read_key is not None
            else map_row.get("ee_vendor_c_id")
        ),
        ee_vendor_id=map_row["ee_vendor_id"],
        ee_payload=sparse,
    )


def _build_sparse_update_payload(
    *, full_payload: dict, supplier_docname: str
) -> dict:
    """Read the prior push snapshot from the Supplier Map; return
    vendorId + changed fields only."""
    snap_text = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": supplier_docname},
        "ecs_last_pushed_payload",
    )
    if not snap_text:
        return dict(full_payload)
    try:
        prior = json.loads(snap_text)
    except Exception:
        return dict(full_payload)
    if not isinstance(prior, dict):
        return dict(full_payload)

    delta = {"vendorId": full_payload.get("vendorId")}
    for k, v in full_payload.items():
        if k == "vendorId":
            continue
        if prior.get(k) != v:
            delta[k] = v
    return delta


def _save_push_snapshot(*, supplier_docname: str, payload: dict) -> None:
    map_name = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": supplier_docname},
        "name",
    )
    if not map_name:
        return
    frappe.db.set_value(
        "EasyEcom Supplier Map",
        map_name,
        "ecs_last_pushed_payload",
        json.dumps(payload, sort_keys=True, default=str),
        update_modified=False,
    )


def _classify_country(country_name: str | None) -> Literal["india", "foreign", "unknown"]:
    """Same 3-way bucket as supplier_pull. Drives the tax-required
    branches on both create and update."""
    if not country_name:
        return "unknown"
    needle = country_name.strip().lower()
    if needle in ("india", "in", "ind", "bharat"):
        return "india"
    resolved = resolve_country(country_name)
    if resolved is None:
        return "unknown"
    if resolved.country_id == 1:
        return "india"
    return "foreign"


# ----- Map row helpers -----


def _upsert_map_row_after_create(
    *,
    supplier_docname: str,
    existing_map: dict | None,
    ee_vendor_c_id: str | None,
    ee_vendor_id: str | None,
) -> str:
    """After a successful CreateVendor, ensure the Supplier Map row
    carries BOTH ids returned by EE. Stage 1 made ee_vendor_c_id reqd,
    so when no map row exists yet we MUST have one of the ids to
    persist (the unique key)."""
    if existing_map:
        updates: dict[str, Any] = {
            "status": "Mapped",
            "flag_reason": "",
        }
        if ee_vendor_c_id:
            updates["ee_vendor_c_id"] = ee_vendor_c_id
        if ee_vendor_id:
            updates["ee_vendor_id"] = ee_vendor_id
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            existing_map["name"],
            updates,
            update_modified=True,
        )
        return existing_map["name"]

    doc = frappe.new_doc("EasyEcom Supplier Map")
    doc.update(
        {
            # Use ee_vendor_c_id if present (matches the autoname
            # format ECS-SUPP-{ee_vendor_c_id}); fall back to a
            # synthetic placeholder derived from vendor_id (in the
            # unusual case where EE returns only vendor_id).
            "ee_vendor_c_id": ee_vendor_c_id
            or f"vid-{ee_vendor_id}",
            "ee_vendor_id": ee_vendor_id or "",
            "erpnext_doctype": "Supplier",
            "erpnext_name": supplier_docname,
            "status": "Mapped",
            "flag_reason": "",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_map_row_flagged(
    *,
    supplier_docname: str,
    existing_map: dict | None,
    reasons: list[str],
) -> str:
    """Flag-not-pushed: Supplier exists in ERPNext but couldn't be
    pushed. Create / update the Map row with status =
    Flagged-Not-Created (no EE id yet)."""
    flag_reason = " || ".join(reasons)[:140] if reasons else ""
    if existing_map:
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            existing_map["name"],
            {"status": "Flagged-Not-Created", "flag_reason": flag_reason},
            update_modified=True,
        )
        return existing_map["name"]
    doc = frappe.new_doc("EasyEcom Supplier Map")
    doc.update(
        {
            "ee_vendor_c_id": f"flagged-{supplier_docname}",
            "ee_vendor_id": "",
            "erpnext_doctype": "Supplier",
            "erpnext_name": supplier_docname,
            "status": "Flagged-Not-Created",
            "flag_reason": flag_reason,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _write_push_sync_record(
    *,
    entity_name: str,
    ee_vendor_c_id: str,
    status: str,
    last_error: str | None,
) -> None:
    """Best-effort SR write."""
    try:
        write_supplier_push_sync_record(
            entity_name=entity_name,
            ee_vendor_c_id=ee_vendor_c_id,
            status=status,
            last_error=last_error,
        )
    except Exception as exc:
        frappe.log_error(
            title=f"supplier_push SR write failed for {entity_name}",
            message=f"{type(exc).__name__}: {exc}",
        )


# ----- Batch sweep -----


def candidate_suppliers_for_sweep(limit: int | None = None) -> list[str]:
    """Suppliers eligible for the onboarding push sweep.

    Policy (Stage 4):
      - supplier_type = Company (§8f is wholesale; individual / proprietorship
        types are out of scope for this stage)
      - disabled = 0
      - no EasyEcom Supplier Map row exists yet (re-push of existing
        mapped suppliers goes through the individual-push trigger,
        not the sweep)

    Suppliers with a Map row but no ee_vendor_id (e.g. Flagged-Not-Created
    from a prior push) are NOT swept — they're FDE-visible failures;
    re-attempting via sweep would just re-fail until the FDE fixes
    the source.
    """
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    rows = frappe.db.sql(
        f"""
        SELECT s.name
        FROM `tabSupplier` s
        LEFT JOIN `tabEasyEcom Supplier Map` m
            ON m.erpnext_doctype = 'Supplier'
            AND m.erpnext_name = s.name
        WHERE s.supplier_type = 'Company'
          AND s.disabled = 0
          AND m.name IS NULL
        ORDER BY s.creation ASC
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
    enqueue_push_all_pending."""
    if client is None:
        client = EasyEcomClient()
    executor = FieldMappingExecutor(SUPPLIER_PUSH_RULESET)

    codes = candidate_suppliers_for_sweep(limit=limit)
    outcome = SweepOutcome(total_considered=len(codes))

    def _handle(supplier_docname: str) -> None:
        po = push_one_supplier(
            supplier_docname,
            client=client,
            account=account,
            executor=executor,
        )
        outcome.outcomes.append(po)

    def _on_failure(supplier_docname: str, exc: BaseException) -> None:
        outcome.outcomes.append(
            PushOutcome(
                supplier_docname=supplier_docname,
                operation="error",
                pushed=False,
                flag_reasons=[f"{type(exc).__name__}: {exc}"],
            )
        )
        frappe.log_error(
            title=f"supplier_push sweep failed: {supplier_docname}",
            message=f"{type(exc).__name__}: {exc}",
        )

    for_each_record(
        codes,
        handler=_handle,
        on_failure=_on_failure,
        flow_name="supplier_push_sweep",
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
        else:
            outcome.flagged_count += 1
    return outcome


def enqueue_push_all_pending(
    *, account_name: str, limit: int | None = None
) -> dict[str, Any]:
    """Production batch entry — enqueues one push job per candidate via
    the §6.3.1 facade. Returns immediately with counts."""
    from ecommerce_super.easyecom.queue import enqueue_easyecom_job

    codes = candidate_suppliers_for_sweep(limit=limit)
    enqueued: list[str] = []
    for supplier_docname in codes:
        try:
            qj_name = enqueue_easyecom_job(
                job_type="Supplier Push",
                target_doctype="Supplier",
                target_name=supplier_docname,
                method="ecommerce_super.easyecom.flows.supplier_push.enqueue_supplier_push",
                kwargs={
                    "supplier_docname": supplier_docname,
                    "account_name": account_name,
                },
            )
            enqueued.append(qj_name)
        except Exception as exc:
            frappe.log_error(
                title=f"enqueue_supplier_push failed for {supplier_docname}",
                message=f"{type(exc).__name__}: {exc}",
            )
    return {
        "total_considered": len(codes),
        "enqueued_count": len(enqueued),
        "queue_job_names_sample": enqueued[:10],
    }


def enqueue_supplier_push(
    supplier_docname: str, *, account_name: str
) -> str:
    """Queue worker entry — pushes a single supplier."""
    account = frappe.get_doc("EasyEcom Account", account_name)
    client = EasyEcomClient()
    outcome = push_one_supplier(
        supplier_docname, client=client, account=account
    )
    return f"{outcome.operation}:{outcome.ee_vendor_id or '-'}"


# ----- Doc-event hook (auto-push) -----


def enqueue_on_supplier_change(
    doc: Any, method: str | None = None, **_kwargs
) -> None:
    """Supplier.on_update hook. Fires only when the account has
    auto_push_suppliers_on_save=1. Ping-pong guard: skip when the pull
    flow is mid-flight (it sets frappe.flags.easyecom_supplier_pull_in_flight).
    """
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return  # avoid pull-then-push echo
    if doc.doctype != "Supplier":
        return
    if doc.supplier_type != "Company":
        return  # §8f is wholesale only
    if doc.disabled:
        return

    account_name = _account_with_auto_push_enabled()
    if not account_name:
        return

    from ecommerce_super.easyecom.queue import enqueue_easyecom_job

    enqueue_easyecom_job(
        job_type="Supplier Push",
        target_doctype="Supplier",
        target_name=doc.name,
        method="ecommerce_super.easyecom.flows.supplier_push.enqueue_supplier_push",
        kwargs={"supplier_docname": doc.name, "account_name": account_name},
    )


def _account_with_auto_push_enabled() -> str | None:
    """First enabled EasyEcom Account with auto_push_suppliers_on_save=1."""
    return frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1, "auto_push_suppliers_on_save": 1},
        "name",
    )
