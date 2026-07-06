"""§10 Stage 2 — ERPNext → EE Stock Transfer outbound flow.

Triggered on Delivery Note.on_submit. Two branches per source-WH-EE-
mapping:

  STN BRANCH  (source EE-mapped)        → CREATE_ORDER /webhook/v2/createOrder
                                           with orderType=stocktransferorder
                                           per §10.G grounded payload.
  PO  BRANCH  (source NOT EE-mapped,    → §9 CreatePurchaseOrder, REUSED.
              target EE-mapped)          Vendor resolved via the source
                                         Company's Internal Supplier's
                                         Supplier Map row.

This module owns:
  - Gate-0 (Internal-Customer-DN + at-least-one-EE-warehouse) + the
    multi-warehouse-pair check (refuse the submit, don't auto-split).
  - The precondition chain (Internal Customer pair fabric, ee_customer_id,
    Item Maps, Company GSTINs, target Warehouse Address) — misses → Drift
    on Transfer Map with flag_reason, never throw through the hook.
  - Transfer Map row upsert (Mapped / Drift / SI-Pending / EE-Pushed).
  - SI auto-draft (different-GSTIN only) — DRAFT, never auto-submitted
    per the §10 invariant.
  - STN payload build per §10.G + EE response capture.
  - PO branch dispatch (reuses §9 push_one_po) + ee_po_id capture.
  - Pause-defer: when paused, lands ERPNext-side state but records
    ecs_pending_ee_push=1; un-pause runner fires.
  - Per-DN Sync Record + Line-child population.

What this module DOESN'T own (deferred):
  - Inbound (GRN-Complete → IPR + IPI + DN auto-creation) — Stage 3.
  - Cancel/amend of EE-pushed transfers — payload UNGROUNDED, deferred
    until §10.G grounds the cancelOrder endpoint. Stub-blocker only.
  - UI/workspace (Stage 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import frappe
from frappe.utils import flt, get_datetime, now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import CREATE_ORDER
from ecommerce_super.easyecom.exceptions import EasyEcomError
from ecommerce_super.easyecom.flows._transfer_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_transfer_push_sync_record,
)


PING_PONG_FLAG = "easyecom_transfer_push_in_flight"

# §10.G STN payment / shipping enum values. Account defaults sent as
# Select labels like "5 Prepaid" / "1 Standard COD" — the leading int
# is the wire value.
_DEFAULT_PAYMENT_MODE = 5
_DEFAULT_SHIPPING_METHOD = 1


TransferOp = Literal[
    "skipped",        # Gate-0 miss
    "drift",          # precondition fail
    "pending_pause",  # paused — pending recorded, no EE call
    "stn_pushed",     # STN createOrder fired + ids captured
    "po_pushed",      # PO branch via §9 machinery + ee_po_id captured
    "error",          # EE-side error or unexpected exception
]


@dataclass
class TransferPushOutcome:
    dn_name: str
    operation: TransferOp
    transfer_map: str | None = None
    sales_invoice: str | None = None
    ee_order_id: str | None = None
    ee_suborder_id: str | None = None
    ee_invoice_id: str | None = None
    ee_po_id: int | None = None
    ee_doctype: str | None = None  # "STN" | "PO" | None
    flag_reasons: list[str] = field(default_factory=list)
    ee_payload: dict[str, Any] | None = None
    sync_record_name: str | None = None
    status: str | None = None  # Transfer Map status set


# ============================================================
# Public API
# ============================================================


def push_one_transfer(
    dn_name: str,
    *,
    client: EasyEcomClient | None = None,
) -> TransferPushOutcome:
    """Push one §10 transfer to EE. Pure-ish: builds the Transfer Map
    row (or Drift state), drafts SI when different-GSTIN, fires the
    appropriate EE branch. Never raises through the hook boundary —
    failures land on Transfer Map + Failed Sync Record."""

    if not dn_name or not frappe.db.exists("Delivery Note", dn_name):
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="error",
            flag_reasons=[f"DN {dn_name!r} does not exist"],
        )

    dn = frappe.get_doc("Delivery Note", dn_name)

    # Gate 0 — Internal-Customer DN + at least one EE-mapped warehouse.
    if not int(dn.is_internal_customer or 0):
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="skipped",
            flag_reasons=["Gate-0: not an Internal-Customer DN"],
        )

    pair = _resolve_source_target_pair(dn)
    if pair is None:
        # Multi-warehouse pair refused → validation error at the
        # validate_pre_submit hook, NEVER lands here from on_submit.
        # Defensive fallback.
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="skipped",
            flag_reasons=[
                "Gate-0: DN has multiple distinct source/target warehouse "
                "pairs — split into separate DNs."
            ],
        )
    source_wh, target_wh = pair

    source_ee_mapped = _is_ee_mapped_warehouse(source_wh)
    target_ee_mapped = _is_ee_mapped_warehouse(target_wh)
    if not source_ee_mapped and not target_ee_mapped:
        # Both non-EE → silently inert (pure ERPNext stock movement).
        return TransferPushOutcome(
            dn_name=dn_name,
            operation="skipped",
            flag_reasons=["Gate-0: neither source nor target warehouse is EE-mapped"],
        )

    # Branch decision:
    #   source EE-mapped, target EE-mapped     → STN
    #   source NOT EE-mapped, target EE-mapped → PO  (vendor = Internal Supplier)
    #   source EE-mapped, target NOT EE-mapped → B2B (customer = Internal Customer)
    if source_ee_mapped and target_ee_mapped:
        branch = "STN"
    elif source_ee_mapped and not target_ee_mapped:
        branch = "B2B"
    else:
        branch = "PO"

    # Precondition chain. Misses → Drift; still upserts the Map row so
    # the FDE can see it on the worklist.
    precondition_errs = _run_preconditions(dn, source_wh, target_wh, branch)
    if precondition_errs:
        map_name = _upsert_transfer_map_drift(
            dn=dn,
            source_wh=source_wh,
            target_wh=target_wh,
            flag_reason=" || ".join(precondition_errs),
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=" || ".join(precondition_errs),
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="drift",
            transfer_map=map_name,
            flag_reasons=precondition_errs,
            sync_record_name=sr,
            status="Drift",
        )

    # Different-GSTIN → SI auto-draft (Draft, never auto-submit).
    # Resolve GSTIN per Warehouse (multi-state Companies can tag a
    # GSTIN on the Warehouse's Address; fall back to Company.gstin).
    src_gstin = _warehouse_gstin(source_wh)
    tgt_gstin = _warehouse_gstin(target_wh)
    gstin_different = bool(src_gstin and tgt_gstin and src_gstin != tgt_gstin)

    sales_invoice: str | None = None
    if gstin_different:
        # Idempotency: if a prior push attempt drafted an SI on the
        # Transfer Map for this DN, reuse it rather than minting a new
        # one. Without this, retries (e.g. after an EE-side payload
        # rejection like EXP-date-too-early) create duplicate SIs and
        # orphan the original.
        existing_tm = frappe.db.get_value(
            "EasyEcom Transfer Map",
            {"delivery_note": dn.name},
            ["name", "sales_invoice"],
            as_dict=True,
        )
        if existing_tm and existing_tm.get("sales_invoice"):
            sales_invoice = existing_tm["sales_invoice"]
        else:
            sales_invoice = _draft_internal_sales_invoice(
                dn=dn, source_wh=source_wh, target_wh=target_wh
            )

    map_name = _upsert_transfer_map(
        dn=dn,
        source_wh=source_wh,
        target_wh=target_wh,
        sales_invoice=sales_invoice,
        # Initial status before EE push lands. STN-Pending isn't a
        # state — when SI exists & not submitted, we use SI-Pending.
        # When same-GSTIN, status moves straight to EE-Pushed below.
        status="SI-Pending" if sales_invoice else "Mapped",
    )

    # Back-fill SI → TM link. The SI is drafted before the TM exists
    # (TM autoname keys on DN, but the SI is built mid-flow), so the
    # SI is born with ecs_section10_transfer_map=None. Without this
    # back-fill the on_sales_invoice_submit hook short-circuits at the
    # tm_name lookup and the TM never advances from SI-Pending.
    if sales_invoice:
        frappe.db.set_value(
            "Sales Invoice", sales_invoice,
            "ecs_section10_transfer_map", map_name,
            update_modified=False,
        )

    # Pause gate. The pause-pending behaviour is § identical to §9 FIX 2:
    # ERPNext-side state (Transfer Map + SI Draft) lands, but the EE
    # write is deferred. fire_pending_transfer_pushes() runs on
    # un-pause.
    if _is_paused():
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            "ecs_pending_ee_push",
            1,
            update_modified=False,
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status="Pending",
            last_error=None,
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="pending_pause",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            sync_record_name=sr,
            status="SI-Pending" if sales_invoice else "Mapped",
        )

    # EE push.
    if branch == "STN":
        return _do_stn_push(
            dn=dn, map_name=map_name,
            source_wh=source_wh, target_wh=target_wh,
            sales_invoice=sales_invoice, client=client,
        )
    elif branch == "B2B":
        return _do_b2b_branch_push(
            dn=dn, map_name=map_name,
            source_wh=source_wh, target_wh=target_wh,
            sales_invoice=sales_invoice, client=client,
        )
    else:
        return _do_po_branch_push(
            dn=dn, map_name=map_name,
            source_wh=source_wh, target_wh=target_wh,
            sales_invoice=sales_invoice, client=client,
        )


# ============================================================
# Gate 0 helpers
# ============================================================


def _resolve_source_target_pair(dn: Any) -> tuple[str, str] | None:
    """Resolve the (source_wh, target_wh) pair from DN-header fields.

    NOTE: also imported by easyecom.api.transfer_diagnostic.trace_dn
    (gh#26 diagnostic) so the FDE-facing trace walks the same gates the
    on_submit hook uses. Treat any signature/semantics change as a
    coordinated change with that endpoint.

    GROUNDING CORRECTION (live Harmony smoke 2026-05-30): routing is
    Customer-anchored — the FDE sets Transfer From + Transfer To
    Warehouses on the DN header. Items' warehouse/target_warehouse are
    derivative (validate sets them) and not consulted for routing.

    Backward-compat: if the new header fields aren't populated, fall
    back to the legacy `ecs_section10_target_warehouse` + items[].
    """
    transfer_from = (
        getattr(dn, "ecs_section10_transfer_from_warehouse", None) or ""
    ).strip()
    transfer_to = (
        getattr(dn, "ecs_section10_transfer_to_warehouse", None) or ""
    ).strip()
    if transfer_from and transfer_to:
        return (transfer_from, transfer_to)

    # Legacy back-compat path
    legacy_target = (
        getattr(dn, "ecs_section10_target_warehouse", None) or ""
    ).strip()
    if legacy_target:
        sources: set[str] = set()
        for line in dn.items or []:
            src = (line.warehouse or "").strip()
            if src:
                sources.add(src)
        if len(sources) != 1:
            return None
        return (next(iter(sources)), legacy_target)

    pairs: set[tuple[str, str]] = set()
    for line in dn.items or []:
        source = (line.warehouse or "").strip()
        target = (line.target_warehouse or "").strip()
        if source and target:
            pairs.add((source, target))
    if len(pairs) != 1:
        return None
    return next(iter(pairs))


def _is_ee_mapped_warehouse(warehouse: str) -> bool:
    """True iff this Warehouse is the mapped_warehouse of some Live +
    enabled EasyEcom Location.

    Also imported by easyecom.api.transfer_diagnostic.trace_dn (gh#26)
    so the FDE trace and the on_submit gate share one definition of
    "EE-mapped". Coordinate any change with that endpoint.
    """
    return bool(
        frappe.db.get_value(
            "EasyEcom Location",
            {
                "mapped_warehouse": warehouse,
                "workflow_state": "Live",
                "enabled": 1,
            },
            "name",
        )
    )


def _warehouse_company(warehouse: str) -> str:
    return frappe.db.get_value("Warehouse", warehouse, "company") or ""


def _company_gstin(company: str) -> str:
    if not company:
        return ""
    return (frappe.db.get_value("Company", company, "gstin") or "").strip().upper()


def _warehouse_gstin(warehouse: str) -> str:
    """Resolve the effective GSTIN for a Warehouse.

    GROUNDING CORRECTION (live Harmony smoke 2026-05-30): a single
    Company can register multiple GSTINs (one per state branch) and
    tag them to specific warehouse Addresses. The earlier substrate
    only read `Company.gstin`, which collapsed all branches of a
    multi-state Company into the same GSTIN — incorrect for the
    different-GSTIN STN/SI gating.

    Lookup order:
      1. Any Address linked to this Warehouse that carries a `gstin`
      2. Company.gstin (legacy / single-GSTIN sites)
    """
    if not warehouse:
        return ""
    addr_gstin = frappe.db.sql(
        """
        SELECT a.gstin
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl
          ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND dl.link_name = %s
          AND IFNULL(a.gstin, '') != ''
        LIMIT 1
        """,
        (warehouse,),
    )
    if addr_gstin and addr_gstin[0][0]:
        return addr_gstin[0][0].strip().upper()
    return _company_gstin(_warehouse_company(warehouse))


# ============================================================
# Preconditions
# ============================================================


def _unmapped_items_for_dn(dn: Any) -> list[str]:
    """Return the list of DN line `item_code`s that are not resolvable
    on EasyEcom. Empty list when every line is genuinely synced.

    gh#93: shared between the post-submit `_run_preconditions` check
    (accumulates to Drift / Failed Sync Record) and the pre-submit
    `validate_pre_submit` guard (throws to block the save). Keeps the
    two surfaces consistent — the FDE always sees the same set of
    unsynced items whether they hit the early guard or the late
    precondition.

    Original gh#93 fix caught only the "no Item Map row" case. The
    2026-07-04 reopener showed a case (DL-261309-3, line 5 SKU
    `FG06601-4-M`) where the local Item Map row existed but EE
    still returned "Unable to find the sku with provided parameters"
    — i.e. the map row was stale / partial and did not actually
    represent a live EE sku. We now also block when:

      - the map row's `status` is "Flagged-Not-Created" (map exists
        as a placeholder but the item was never created in EE), or
      - the map row's `status` is "Disabled" (explicitly excluded), or
      - the map row has no `ee_product_id` populated (map exists but
        no successful push has ever landed one).

    Statuses treated as OK-to-push:
      - "Mapped"          → clean state
      - "Created-Flagged" → created in EE, flagged for FDE cleanup
                           on a non-blocking field (e.g. missing HSN);
                           the push itself resolves
      - "Drift"           → EE has the item, data has diverged; push
                           resolves via re-sync path (blocking here
                           would strand every DN on the drift set)
    """
    unmapped: list[str] = []
    ok_statuses = {"Mapped", "Created-Flagged", "Drift"}
    for line in dn.items or []:
        map_row = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": line.item_code},
            ["name", "status", "ee_product_id"],
            as_dict=True,
        )
        if not map_row:
            unmapped.append(line.item_code)
            continue
        status = (map_row.get("status") or "").strip()
        if status not in ok_statuses:
            # Flagged-Not-Created / Disabled / anything else the FDE
            # hasn't reviewed — treat as unsynced. Item-specific
            # message so the FDE knows exactly what to fix.
            unmapped.append(
                f"{line.item_code} (Item Map status={status!r})"
            )
            continue
        ee_product_id = str(map_row.get("ee_product_id") or "").strip()
        if not ee_product_id:
            # Map row exists but no EE-side product ID ever landed —
            # the previous "push" never succeeded. This is the exact
            # stale-map class of failure gh#93's reopener flagged
            # (DL-261309-3 line 5 was rejected by EE with "sku not
            # found" for a SKU whose local map row lacked
            # ee_product_id).
            unmapped.append(
                f"{line.item_code} (Item Map exists but no ee_product_id "
                "— previous push never landed)"
            )
    return unmapped


def _run_preconditions(
    dn: Any, source_wh: str, target_wh: str, branch: str
) -> list[str]:
    """Returns [] on clear, or a list of human-readable reasons."""
    errs: list[str] = []

    src_company = _warehouse_company(source_wh)
    tgt_company = _warehouse_company(target_wh)

    # (1) Internal Customer pair fabric — lookup, refuse on miss.
    internal_customer = _find_internal_customer(
        target_company=tgt_company, source_company=src_company
    )
    if not internal_customer:
        errs.append(
            f"Internal Customer pair missing: no Customer with "
            f"is_internal_customer=1, represents_company={tgt_company!r}, "
            f"and {src_company!r} in companies[*].company. "
            "Run ensure_internal_party_pairs_for_account on the Account."
        )
    else:
        # Verify the DN's customer IS this Internal Customer. ERPNext
        # enforces this at validate, but defensive check here surfaces
        # the misconfiguration as a flag_reason rather than a hard throw.
        if dn.customer != internal_customer:
            errs.append(
                f"DN customer = {dn.customer!r} but the resolved "
                f"Internal Customer for this transfer pair is "
                f"{internal_customer!r}. Misconfigured DN."
            )
    # (2) GROUNDING CORRECTION (live Harmony smoke 2026-05-29): the
    # STN payload's `customer[].customerId` is the TARGET WAREHOUSE's
    # `company_id` from /getAllLocation — NOT the §8e Customer Map's
    # ee_customer_id. The Internal Customer pair is still required by
    # ERPNext to legitimise the inter-warehouse Delivery Note, but its
    # ee_customer_id plays no role in the §10 STN wire. Precondition:
    # target warehouse's EE Location row carries an ee_company_id.
    # STN branch needs target ee_company_id (customerId in payload).
    # B2B branch needs the Internal Customer's ee_customer_id (wholesale
    # customer record on EE) — target WH is intentionally not EE-mapped.
    if branch == "STN":
        target_ee_company_id = frappe.db.get_value(
            "EasyEcom Location",
            {"mapped_warehouse": target_wh},
            "ee_company_id",
        )
        if not target_ee_company_id:
            errs.append(
                f"Target warehouse {target_wh!r} → EE Location has no "
                "ee_company_id captured. The §10 STN payload uses this "
                "as customer[].customerId. Re-run discover_locations or "
                "set ee_company_id on the EE Location row."
            )
    elif branch == "B2B" and internal_customer:
        ic_ee_id = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"erpnext_doctype": "Customer",
             "erpnext_name": internal_customer},
            "ee_customer_id",
        )
        if not ic_ee_id:
            errs.append(
                f"Internal Customer {internal_customer!r} has no "
                "ee_customer_id captured on Customer Map. The §10 B2B "
                "branch (source EE-mapped, target NOT EE-mapped) needs "
                "the Internal Customer pushed to EE first via §8e "
                "CreateCustomer so its c_id can drive the B2B order's "
                "customer[].customerId."
            )

    # (3) Item Map for every DN line.
    unmapped = _unmapped_items_for_dn(dn)
    if unmapped:
        errs.append(
            "DN line(s) reference Items without an EasyEcom Item Map: "
            + ", ".join(repr(s) for s in unmapped)
            + ". Run §8d Item Push for these Items."
        )

    # (4) Source + target Company GSTINs configured.
    if not _company_gstin(src_company):
        errs.append(
            f"Source Company {src_company!r} has no GSTIN configured. "
            "Set Company.gstin on the Company form (India Compliance)."
        )
    if not _company_gstin(tgt_company):
        errs.append(
            f"Target Company {tgt_company!r} has no GSTIN configured."
        )

    # (5) Target warehouse Address (used in STN shipping block).
    target_addr = _resolve_warehouse_address(target_wh)
    if not _addr_has_line(target_addr):
        errs.append(
            f"Target warehouse {target_wh!r} has no resolvable Address "
            "(needs address_line1 or city). The STN payload's shipping "
            "block requires this. Link an Address to the Warehouse via "
            "Address.links, then re-submit."
        )

    # PO branch — additional precondition: vendor_id resolvable for
    # the source Company. This means the source Company's Internal
    # Supplier must have an EE-side vendor representation.
    if branch == "PO":
        vendor_resolution = _resolve_po_branch_vendor(src_company)
        if not vendor_resolution.get("vendor_id"):
            errs.append(
                "PO branch requires an EE-side vendor for source Company "
                f"{src_company!r} — not configured. Lookup path: Internal "
                "Supplier (is_internal_supplier=1, represents_company="
                f"{src_company!r}) → its Supplier Map row → ee_vendor_id. "
                f"{vendor_resolution.get('reason', '')}"
            )

    return errs


def _find_internal_customer(
    *, target_company: str, source_company: str
) -> str | None:
    """Lookup the internal customer that can serve a (source → target)
    transfer.

    GROUNDING CORRECTION (live Harmony smoke 2026-05-30): the
    single-"Internal Customer" model has ONE customer that serves
    transfers to any destination Company. Its represents_company is a
    default; the per-DN destination Company is set on
    `DN.represents_company` by before_validate. Lookup chain:
      1. Strict match (legacy): represents_company=target AND source in
         companies. Returns customer dedicated to this destination.
      2. Loose match (single-customer model): any enabled internal
         customer whose companies include source AND not disabled.
    """
    if not target_company or not source_company:
        return None
    # 1. Strict legacy match
    strict = frappe.db.sql(
        """
        SELECT c.name
        FROM `tabCustomer` c
        JOIN `tabAllowed To Transact With` atw
          ON atw.parent = c.name
        WHERE c.is_internal_customer = 1
          AND IFNULL(c.disabled, 0) = 0
          AND c.represents_company = %s
          AND atw.company = %s
        LIMIT 1
        """,
        (target_company, source_company),
        as_dict=True,
    )
    if strict:
        return strict[0]["name"]
    # 2. Loose single-customer match — any enabled internal customer
    #    whose companies list includes source_company.
    loose = frappe.db.sql(
        """
        SELECT c.name
        FROM `tabCustomer` c
        JOIN `tabAllowed To Transact With` atw
          ON atw.parent = c.name
        WHERE c.is_internal_customer = 1
          AND IFNULL(c.disabled, 0) = 0
          AND atw.company = %s
        LIMIT 1
        """,
        (source_company,),
        as_dict=True,
    )
    return loose[0]["name"] if loose else None


def _find_internal_supplier(*, source_company: str) -> str | None:
    if not source_company:
        return None
    return frappe.db.get_value(
        "Supplier",
        {
            "is_internal_supplier": 1,
            "represents_company": source_company,
        },
        "name",
    )


def _resolve_po_branch_vendor(source_company: str) -> dict[str, Any]:
    """For PO branch: the source Company is NOT EE-mapped. EE sees the
    PO as "incoming from outside its universe" and needs a vendorId.
    Lookup path: Internal Supplier representing the source Company →
    its Supplier Map → ee_vendor_id.

    Returns {"vendor_id": str|None, "reason": str}.
    """
    internal_supplier = _find_internal_supplier(source_company=source_company)
    if not internal_supplier:
        return {
            "vendor_id": None,
            "reason": (
                "No Internal Supplier found. Stage 1's "
                "ensure_internal_party_pairs_for_account should have "
                "created one — re-run it."
            ),
        }
    ee_vendor_id = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {
            "erpnext_doctype": "Supplier",
            "erpnext_name": internal_supplier,
        },
        "ee_vendor_id",
    )
    if not ee_vendor_id:
        return {
            "vendor_id": None,
            "reason": (
                f"Internal Supplier {internal_supplier!r} has no "
                "Supplier Map ee_vendor_id captured. The §10 PO branch "
                "needs the source Company to be pushed to EE as a "
                "Vendor first. Either configure manually via §8f "
                "machinery, or use STN-only deployments (every source "
                "EE-mapped)."
            ),
        }
    return {"vendor_id": ee_vendor_id, "reason": ""}


def _resolve_warehouse_address(warehouse: str) -> dict | None:
    if not warehouse:
        return None
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.address_line2, a.city, a.state,
               a.pincode, a.country, a.email_id, a.phone
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (warehouse,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _resolve_company_primary_address(company: str) -> dict | None:
    if not company:
        return None
    rows = frappe.db.sql(
        """
        SELECT a.name, a.address_line1, a.address_line2, a.city, a.state,
               a.pincode, a.country, a.email_id, a.phone
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Company'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (company,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _addr_has_line(addr: dict | None) -> bool:
    if not addr:
        return False
    return bool(
        (addr.get("address_line1") or "").strip()
        or (addr.get("city") or "").strip()
    )


# ============================================================
# Transfer Map upsert
# ============================================================


def _upsert_transfer_map(
    *,
    dn: Any,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None,
    status: str,
) -> str:
    """Insert or update the Transfer Map row keyed on dn.name."""
    existing = frappe.db.get_value(
        "EasyEcom Transfer Map", {"delivery_note": dn.name}, "name"
    )
    fields = {
        "source_warehouse": source_wh,
        "target_warehouse": target_wh,
        "sales_invoice": sales_invoice,
        "status": status,
        "last_observed_at": now_datetime(),
    }
    if existing:
        frappe.db.set_value(
            "EasyEcom Transfer Map", existing, fields, update_modified=True
        )
        return existing
    doc = frappe.new_doc("EasyEcom Transfer Map")
    doc.update(
        {
            "delivery_note": dn.name,
            **fields,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_transfer_map_drift(
    *,
    dn: Any,
    source_wh: str,
    target_wh: str,
    flag_reason: str,
) -> str:
    """Drift state — Map row exists with flag_reason; SI / EE-push
    skipped."""
    existing = frappe.db.get_value(
        "EasyEcom Transfer Map", {"delivery_note": dn.name}, "name"
    )
    fields = {
        "source_warehouse": source_wh,
        "target_warehouse": target_wh,
        "status": "Drift",
        "flag_reason": flag_reason[:1000],
        "last_observed_at": now_datetime(),
    }
    if existing:
        frappe.db.set_value(
            "EasyEcom Transfer Map", existing, fields, update_modified=True
        )
        return existing
    doc = frappe.new_doc("EasyEcom Transfer Map")
    doc.update({"delivery_note": dn.name, **fields})
    doc.insert(ignore_permissions=True)
    return doc.name


# ============================================================
# SI auto-draft (different-GSTIN only)
# ============================================================


def _draft_internal_sales_invoice(
    *, dn: Any, source_wh: str, target_wh: str
) -> str:
    """Auto-create the Internal SI in DRAFT, sized to DN dispatched qty.
    update_stock=0 (the DN handled stock-out). Never auto-submit — the
    §10 invariant says ERP user submits financial documents."""
    # Resolve the Company's default Cost Center so we can stamp it on
    # the SI header + every item line + every tax row. ERPNext's
    # Unrealized P/L posting on internal-transfer SIs requires a CC on
    # the GL entry; missing it throws "Cost Center is required for
    # 'Profit and Loss' account Unrealized Profit and Loss Account".
    default_cc = (
        frappe.db.get_value("Company", dn.company, "cost_center") or ""
    )
    si = frappe.new_doc("Sales Invoice")
    si.update(
        {
            "customer": dn.customer,
            "company": dn.company,
            "posting_date": dn.posting_date,
            "due_date": dn.posting_date,
            "cost_center": default_cc,
            "is_internal_customer": 1,
            "update_stock": 0,
            "currency": dn.currency or "INR",
            "conversion_rate": dn.conversion_rate or 1,
            "selling_price_list": dn.selling_price_list,
            "price_list_currency": dn.price_list_currency or "INR",
            "plc_conversion_rate": dn.plc_conversion_rate or 1,
            # Address inheritance from the DN — drives the seller/buyer
            # GSTIN pair on India Compliance's SI tax computation.
            # Without this, ERPNext defaults to the Company's primary
            # Address (gst_category mismatch on multi-state setups).
            "company_address": getattr(dn, "company_address", None),
            "dispatch_address_name": getattr(dn, "dispatch_address_name", None),
            "customer_address": getattr(dn, "customer_address", None),
            "shipping_address_name": getattr(dn, "shipping_address_name", None),
            "company_gstin": getattr(dn, "company_gstin", None),
            "billing_address_gstin": getattr(dn, "billing_address_gstin", None),
            "ecs_section10_transfer_map": None,  # back-fill below
        }
    )
    for line in dn.items or []:
        si.append(
            "items",
            {
                "item_code": line.item_code,
                "qty": line.qty,
                "rate": line.rate,
                "warehouse": line.warehouse,
                "item_tax_template": line.item_tax_template,
                "delivery_note": dn.name,
                "dn_detail": line.name,
                "cost_center": line.cost_center or default_cc,
            },
        )
    # Copy the DN's Sales Taxes and Charges table onto the SI so the
    # SI reflects the same IGST/CGST/SGST shape as the DN. Without
    # this, the SI is born with no tax rows → IGST output on GST
    # return is missing for inter-state internal transfers.
    if getattr(dn, "taxes_and_charges", None):
        si.taxes_and_charges = dn.taxes_and_charges
    for tax in dn.taxes or []:
        si.append(
            "taxes",
            {
                "charge_type": tax.charge_type,
                "account_head": tax.account_head,
                "rate": tax.rate,
                "tax_amount": tax.tax_amount,
                "description": tax.description,
                "cost_center": tax.cost_center or default_cc,
                "included_in_print_rate": tax.included_in_print_rate,
            },
        )
    # Insert in Draft (docstatus=0). Submit is the ERP user's call.
    si.insert(ignore_permissions=True)
    return si.name


# ============================================================
# B2B branch — source EE-mapped, target NOT EE-mapped
# Stock leaves EE's universe; modelled on EE side as a B2B (wholesale)
# order sold to the Internal Customer. Mirrors the PO branch's symmetry
# from the other side: PO = incoming party (vendor), B2B = outgoing
# party (customer).
# ============================================================


def _build_b2b_payload(
    *, dn: Any, source_wh: str, target_wh: str
) -> dict[str, Any]:
    """B2B order payload — same shape as STN but driven by the Internal
    Customer's wholesale c_id (ee_customer_id from Customer Map), with
    addresses pointing at the (non-EE) destination warehouse so EE's
    record shows where the stock conceptually went."""
    account = _get_account_settings()
    items_payload: list[dict[str, Any]] = []
    total_weight_grams = 0.0
    for idx, line in enumerate(dn.items or [], start=1):
        sku = _resolve_sku_via_item_map(line.item_code)
        qty = flt(line.qty)
        item_weight = flt(
            frappe.db.get_value("Item", line.item_code, "weight_per_unit") or 0
        )
        total_weight_grams += qty * item_weight
        items_payload.append({
            "OrderItemId": f"{dn.name}-L{idx}",
            "Sku": sku,
            "Quantity": str(int(qty)) if qty.is_integer() else str(qty),
            "Price": flt(line.rate),
            "itemDiscount": 0,
        })

    destination_addr = _resolve_warehouse_address(target_wh)

    internal_customer = _find_internal_customer(
        target_company=_warehouse_company(target_wh),
        source_company=_warehouse_company(source_wh),
    )
    ee_customer_id = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer",
         "erpnext_name": internal_customer},
        "ee_customer_id",
    )
    customer_name = (
        frappe.db.get_value("Customer", internal_customer, "customer_name")
        or internal_customer
    )

    payload: dict[str, Any] = {
        # EE's B2B order type is "businessorder" on the createOrder
        # endpoint (live Harmony grounded 2026-05-30 — not "b2border" /
        # "wholesaleorder" / "B2B" as initially guessed).
        "orderType": "businessorder",
        "orderNumber": dn.name,
        "orderDate": _fmt_utc(dn.posting_date, getattr(dn, "posting_time", None)),
        "expDeliveryDate": _fmt_ist(getattr(dn, "delivery_date", None)),
        "shippingCost": 0,
        "paymentMode": _extract_int_prefix(
            account.get("stn_default_payment_mode"), _DEFAULT_PAYMENT_MODE
        ),
        "shippingMethod": _extract_int_prefix(
            account.get("stn_default_shipping_method"),
            _DEFAULT_SHIPPING_METHOD,
        ),
        "packageWeight": int(round(total_weight_grams)),
        "packageHeight": 0, "packageWidth": 0, "packageLength": 0,
        "items": items_payload,
        "customer": [{
            "customerId": int(ee_customer_id) if ee_customer_id and str(
                ee_customer_id).isdigit() else (ee_customer_id or 0),
            "name": customer_name,
            "billing": _addr_to_payload(
                destination_addr, name_override=customer_name
            ),
            "shipping": _addr_to_payload(
                destination_addr, name_override=customer_name
            ),
        }],
    }
    return {k: v for k, v in payload.items() if v is not None}


def _do_b2b_branch_push(
    *, dn: Any, map_name: str, source_wh: str, target_wh: str,
    sales_invoice: str | None, client: EasyEcomClient | None,
) -> TransferPushOutcome:
    """Fire B2B order on EE for source-mapped → target-not-mapped DNs."""
    payload = _build_b2b_payload(
        dn=dn, source_wh=source_wh, target_wh=target_wh,
    )
    if client is None:
        location_key = _location_key_for_warehouse(source_wh)
        client = EasyEcomClient(
            company=dn.company, location_key=location_key
        )
    try:
        response = client.post(CREATE_ORDER, payload=payload)
    except EasyEcomError as exc:
        sr = write_transfer_push_sync_record(
            dn_name=dn.name, company=dn.company, status=STATUS_FAILED,
            last_error=f"B2B createOrder: {type(exc).__name__}: {exc}",
        )
        frappe.db.set_value("EasyEcom Transfer Map", map_name, {
            "status": "Drift",
            "flag_reason": f"EE B2B createOrder error: {exc}"[:1000],
        }, update_modified=True)
        return TransferPushOutcome(
            dn_name=dn.name, operation="error", transfer_map=map_name,
            sales_invoice=sales_invoice,
            flag_reasons=[f"{type(exc).__name__}: {exc}"],
            ee_payload=payload, sync_record_name=sr, status="Drift",
        )
    data = (response or {}).get("data") or {}
    ee_order_id = str(data.get("OrderID") or "")
    ee_suborder_id = str(data.get("SuborderID") or "")
    ee_invoice_id = str(data.get("InvoiceID") or "")
    new_status = "SI-Pending" if sales_invoice else "EE-Pushed"
    frappe.db.set_value("EasyEcom Transfer Map", map_name, {
        "ee_doctype": "B2B",
        "ee_order_id": ee_order_id,
        "ee_suborder_id": ee_suborder_id,
        "ee_invoice_id": ee_invoice_id,
        "status": new_status,
        "ecs_pending_ee_push": 0,
    }, update_modified=True)
    sr = write_transfer_push_sync_record(
        dn_name=dn.name, company=dn.company, status=STATUS_SUCCESS,
        last_error=None,
    )
    return TransferPushOutcome(
        dn_name=dn.name, operation="b2b_pushed", transfer_map=map_name,
        sales_invoice=sales_invoice, ee_order_id=ee_order_id,
        ee_suborder_id=ee_suborder_id, ee_invoice_id=ee_invoice_id,
        ee_doctype="B2B", ee_payload=payload,
        sync_record_name=sr, status=new_status,
    )


# ============================================================
# STN branch
# ============================================================


def _do_stn_push(
    *,
    dn: Any,
    map_name: str,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None,
    client: EasyEcomClient | None,
) -> TransferPushOutcome:
    """Build the §10.G STN payload, POST it, capture the three EE ids."""
    payload = _build_stn_payload(
        dn=dn, source_wh=source_wh, target_wh=target_wh
    )

    if client is None:
        location_key = _location_key_for_warehouse(source_wh)
        client = EasyEcomClient(
            company=dn.company, location_key=location_key
        )

    try:
        response = client.post(CREATE_ORDER, payload=payload)
    except EasyEcomError as exc:
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=f"createOrder: {type(exc).__name__}: {exc}",
        )
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            {
                "status": "Drift",
                "flag_reason": f"EE createOrder error: {exc}"[:1000],
            },
            update_modified=True,
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="error",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            flag_reasons=[f"{type(exc).__name__}: {exc}"],
            ee_payload=payload,
            sync_record_name=sr,
            status="Drift",
        )

    # Capture all three EE ids as strings (§10.G locked).
    data = (response or {}).get("data") or {}
    ee_order_id = str(data.get("OrderID") or "")
    ee_suborder_id = str(data.get("SuborderID") or "")
    ee_invoice_id = str(data.get("InvoiceID") or "")

    # Status decision: when sales_invoice is set + still in Draft (the
    # auto-draft we just landed is Draft by default), Transfer Map
    # stays in SI-Pending. The presence of ee_order_id signals EE-side
    # push happened. When same-GSTIN (no SI), status moves to EE-Pushed.
    # Reported in build report — overloaded SI-Pending rather than
    # introducing a SI-Pending-EE-Pushed transition state.
    new_status = "SI-Pending" if sales_invoice else "EE-Pushed"
    frappe.db.set_value(
        "EasyEcom Transfer Map",
        map_name,
        {
            "ee_doctype": "STN",
            "ee_order_id": ee_order_id,
            "ee_suborder_id": ee_suborder_id,
            "ee_invoice_id": ee_invoice_id,
            "status": new_status,
            "ecs_pending_ee_push": 0,
        },
        update_modified=True,
    )
    # Back-fill the DN's §10 back-ref custom field.
    if frappe.get_meta("Delivery Note").get_field("ecs_section10_transfer_map"):
        frappe.db.set_value(
            "Delivery Note",
            dn.name,
            "ecs_section10_transfer_map",
            map_name,
            update_modified=False,
        )
    if sales_invoice and frappe.get_meta("Sales Invoice").get_field(
        "ecs_section10_transfer_map"
    ):
        frappe.db.set_value(
            "Sales Invoice",
            sales_invoice,
            "ecs_section10_transfer_map",
            map_name,
            update_modified=False,
        )

    sr = write_transfer_push_sync_record(
        dn_name=dn.name,
        company=dn.company,
        status=STATUS_SUCCESS,
        last_error=None,
        line_outcomes=[
            {
                "source_line_ref": line.item_code,
                "source_line_number": idx,
                "target_field": "Sku",
                "line_status": "OK",
            }
            for idx, line in enumerate(dn.items or [], start=1)
        ],
    )

    return TransferPushOutcome(
        dn_name=dn.name,
        operation="stn_pushed",
        transfer_map=map_name,
        sales_invoice=sales_invoice,
        ee_order_id=ee_order_id,
        ee_suborder_id=ee_suborder_id,
        ee_invoice_id=ee_invoice_id,
        ee_doctype="STN",
        ee_payload=payload,
        sync_record_name=sr,
        status=new_status,
    )


def _build_stn_payload(
    *, dn: Any, source_wh: str, target_wh: str
) -> dict[str, Any]:
    """The §10.G wire contract — exact field set, OMITTED fields
    truly omitted (no null placeholders)."""
    account = _get_account_settings()

    items_payload: list[dict[str, Any]] = []
    total_weight_grams = 0.0
    for idx, line in enumerate(dn.items or [], start=1):
        sku = _resolve_sku_via_item_map(line.item_code)
        qty = flt(line.qty)
        item_weight = flt(
            frappe.db.get_value("Item", line.item_code, "weight_per_unit") or 0
        )
        total_weight_grams += qty * item_weight
        items_payload.append(
            {
                "OrderItemId": f"{dn.name}-L{idx}",
                "Sku": sku,
                "Quantity": str(int(qty)) if qty.is_integer() else str(qty),
                "Price": flt(line.rate),
                "itemDiscount": 0,
            }
        )

    tgt_company = _warehouse_company(target_wh)
    # GROUNDING CORRECTION (live Harmony smoke 2026-05-29): both
    # billing and shipping blocks mirror the DESTINATION warehouse's
    # address. The earlier read (billing = target Company's primary
    # Address, shipping = target warehouse Address) was wrong for STN
    # semantics — the receiver invoice + the physical ship-to both
    # point to the same destination warehouse.
    destination_addr = _resolve_warehouse_address(target_wh)
    billing_addr = destination_addr
    shipping_addr = destination_addr
    # GROUNDING CORRECTION (live Harmony smoke 2026-05-29): the STN
    # payload's customer[].customerId is the TARGET WAREHOUSE's EE
    # `company_id` from /getAllLocation — NOT the §8e Customer Map's
    # ee_customer_id. The earlier §10.G grounding read this as the
    # wholesale-customer c_id; that was wrong. With c_id, EE returns
    # 400 "Location does not exist". With company_id, EE returns 200
    # with SuborderID/OrderID/InvoiceID.
    ee_customer_id = frappe.db.get_value(
        "EasyEcom Location",
        {"mapped_warehouse": target_wh},
        "ee_company_id",
    )
    # Both billing.name and shipping.name reflect the destination
    # warehouse's EE-side bare WH name. EE's location_name carries the
    # tenant prefix (e.g. "Harmony Consumer Co. (B2C WH - Delhi)"); EE's
    # internal lookup keys on the part inside the parens
    # ("B2C WH - Delhi"). When the location has no paren suffix
    # (the tenant root location), we send the bare location_name as-is.
    tgt_ee_loc_name = frappe.db.get_value(
        "EasyEcom Location", {"mapped_warehouse": target_wh}, "location_name"
    )
    destination_name = _strip_tenant_prefix(
        tgt_ee_loc_name
        or frappe.db.get_value("Warehouse", target_wh, "warehouse_name")
        or target_wh
    )
    billing_name = destination_name
    shipping_name = destination_name

    payload: dict[str, Any] = {
        "orderType": "stocktransferorder",
        "orderNumber": dn.name,
        "orderDate": _fmt_utc(dn.posting_date, getattr(dn, "posting_time", None)),
        "expDeliveryDate": _fmt_ist(getattr(dn, "delivery_date", None)),
        "shippingCost": 0,
        "paymentMode": _extract_int_prefix(
            account.get("stn_default_payment_mode"), _DEFAULT_PAYMENT_MODE
        ),
        "shippingMethod": _extract_int_prefix(
            account.get("stn_default_shipping_method"),
            _DEFAULT_SHIPPING_METHOD,
        ),
        "packageWeight": int(round(total_weight_grams)),
        "packageHeight": 0,
        "packageWidth": 0,
        "packageLength": 0,
        "items": items_payload,
        "customer": [
            {
                "customerId": int(ee_customer_id or 0),
                "name": shipping_name,  # EE expects the target EE
                # location_name here — that's how EE resolves the STN's
                # destination warehouse. customerId alone is insufficient.
                "billing": _addr_to_payload(
                    billing_addr,
                    name_override=billing_name,
                ),
                "shipping": _addr_to_payload(
                    shipping_addr,
                    name_override=shipping_name,
                ),
            }
        ],
    }
    # Strip any None-valued top-level keys (defensive — EE rejects nulls
    # in some paths). The §10.G OMITTED set is enforced by not assigning
    # those keys in the first place.
    return {k: v for k, v in payload.items() if v is not None}


def _addr_to_payload(
    addr: dict | None, *, name_override: str | None = None
) -> dict[str, Any]:
    out = {
        "name": name_override or "",
        "addressLine1": (addr or {}).get("address_line1") or "",
        "addressLine2": (addr or {}).get("address_line2") or "",
        "postalCode": (addr or {}).get("pincode") or "",
        "city": (addr or {}).get("city") or "",
        "state": (addr or {}).get("state") or "",
        "country": (addr or {}).get("country") or "India",
        "contact": (addr or {}).get("phone") or "",
        "email": (addr or {}).get("email_id") or "",
    }
    return out


def _extract_int_prefix(label: str | None, fallback: int) -> int:
    """Account.stn_default_* fields are Select labels like '5 Prepaid'
    or '1 Standard COD'. Extract the leading int — that's the wire
    value EE expects."""
    if not label:
        return fallback
    head = (label or "").strip().split(" ", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        return fallback


def _fmt_utc(date_value: Any, posting_time: Any) -> str:
    """`YYYY-MM-DD HH:MM:SS` UTC per §10.G orderDate spec.

    Frappe stores DN posting_date + posting_time as naive values in the
    site's timezone. We must convert site_tz → UTC before formatting;
    otherwise EE interprets the IST clock as UTC and the displayed time
    drifts by the site_tz offset (smoke 2026-05-29: site IST 22:32:03
    rendered on EE as 2026-05-30 04:02:03 IST — exactly +5:30 ahead).
    """
    if not date_value:
        return ""
    from datetime import date as _date, datetime as _datetime, time as _time, timedelta as _td
    from zoneinfo import ZoneInfo

    site_tz = ZoneInfo(frappe.utils.get_system_timezone() or "Asia/Kolkata")

    if isinstance(date_value, _datetime):
        local_dt = date_value
    else:
        if isinstance(date_value, str):
            date_value = frappe.utils.getdate(date_value)
        if posting_time is None:
            t = _time(0, 0, 0)
        elif isinstance(posting_time, _td):
            # Frappe Time fields come back as timedelta from MariaDB
            total = int(posting_time.total_seconds())
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            t = _time(h % 24, m, s)
        elif isinstance(posting_time, _time):
            t = posting_time
        else:
            t = frappe.utils.get_time(str(posting_time).split(".")[0])
        local_dt = _datetime.combine(date_value, t)

    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=site_tz)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ist(date_value: Any) -> str:
    """`YYYY-MM-DD HH:MM:SS` IST per §10.G expDeliveryDate spec."""
    if not date_value:
        return ""
    return f"{date_value} 23:59:59"


def _resolve_sku_via_item_map(item_code: str) -> str:
    """Sku-first priority (§9 convention). Item Map.ee_sku is the
    write key."""
    return (
        frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": item_code},
            "ee_sku",
        )
        or item_code
    )


def _location_key_for_warehouse(warehouse: str) -> str | None:
    return frappe.db.get_value(
        "EasyEcom Location",
        {"mapped_warehouse": warehouse, "workflow_state": "Live", "enabled": 1},
        "location_key",
    )


def _strip_tenant_prefix(location_name: str) -> str:
    """Extract the bare WH name from an EE location_name.

    EE returns location names as "<tenant> (<WH name>)" for branch
    warehouses and "<tenant>" alone for the tenant root. STN createOrder
    expects the bare WH name (the part inside the parens). For the
    tenant root, the bare name IS the location_name.
    """
    import re
    if not location_name:
        return ""
    m = re.search(r"\(([^)]+)\)\s*$", location_name)
    return m.group(1).strip() if m else location_name.strip()


def _get_account_settings() -> dict[str, Any]:
    """Return the §10 settings from the (single, enabled) Account row.
    Falls back to packaged defaults if no account is enabled (test
    paths that don't enable the account)."""
    row = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        ["stn_default_payment_mode", "stn_default_shipping_method"],
        as_dict=True,
    )
    return row or {}


# ============================================================
# PO branch (reuses §9 push_one_po)
# ============================================================


def _do_po_branch_push(
    *,
    dn: Any,
    map_name: str,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None,
    client: EasyEcomClient | None,
) -> TransferPushOutcome:
    """PO branch: source NOT EE-mapped, target EE-mapped. EE sees the
    transfer as a PO arriving from outside its universe; the source-
    Company vendor (the Internal Supplier's EE-side representation)
    is the `vendorId`. referenceCode is the DN name — single-key
    strategy parity with the STN branch.

    Stage 4 §1 lifts the Stage 2 deferral: this function now fires
    `/WMS/Cart/CreatePurchaseOrder` directly (parity with §9's
    `_do_content_push` payload shape, reading from the DN instead of a
    PO doc). The §9 helpers (`PURCHASE_ORDER_CREATE`, `compute_tax_type`)
    are reused; only the source-doc-traversal differs.
    """
    from ecommerce_super.easyecom.client.endpoints import (
        PURCHASE_ORDER_CREATE,
    )
    from ecommerce_super.easyecom.exceptions import EasyEcomError
    from ecommerce_super.easyecom.flows.po_push import (
        _content_tax_signature,
        _extract_ee_po_id,
        _find_supplier_state,
        _fmt_date,
        _resolve_line_tax_rate,
    )
    from ecommerce_super.easyecom.tax.place_of_supply import (
        compute_tax_type,
    )

    src_company = _warehouse_company(source_wh)
    vendor_resolution = _resolve_po_branch_vendor(src_company)
    if not vendor_resolution.get("vendor_id"):
        # Should have been caught in preconditions; defensive fallback.
        reason = (
            "PO branch invoked but vendor unresolvable: "
            + vendor_resolution.get("reason", "")
        )
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            {"status": "Drift", "flag_reason": reason[:1000]},
            update_modified=True,
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=reason,
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="drift",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            flag_reasons=[reason],
            sync_record_name=sr,
            status="Drift",
        )

    vendor_id = vendor_resolution["vendor_id"]
    target_address = _resolve_warehouse_address(target_wh) or {}

    # Tax setup. The PO is "incoming from outside EE's universe"; for
    # tax purposes the source Company's GSTIN drives the supplier
    # state. The Internal Supplier representing source carries the
    # canonical state via India Compliance's Address linkage.
    from ecommerce_super.easyecom.flows.transfer_inbound import (
        _find_internal_supplier,
    )
    internal_supplier = _find_internal_supplier(
        source_company=src_company,
        target_company=_warehouse_company(target_wh),
    )
    supplier_state = (
        _find_supplier_state(internal_supplier)
        if internal_supplier
        else None
    )
    supplier_country = "India"
    warehouse_state = target_address.get("gst_state")

    line_items: list[dict[str, Any]] = []
    line_outcomes: list[dict[str, Any]] = []
    for line in dn.items or []:
        item_map = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": line.item_code},
            ["ee_sku"],
            as_dict=True,
        )
        sku = item_map.ee_sku if item_map else line.item_code
        tax_rate_pct, _decimal = _resolve_line_tax_rate(line)
        tax_inclusive_unit_price = float(line.rate or 0) * (
            1.0 + (tax_rate_pct / 100.0)
        )
        tax_value = (
            float(line.rate or 0)
            * (tax_rate_pct / 100.0)
            * float(line.qty or 0)
        )
        tax_type = compute_tax_type(
            supplier_state=supplier_state,
            warehouse_state=warehouse_state,
            supplier_country=supplier_country,
        )
        line_item: dict[str, Any] = {
            "lineItemNumber": int(line.idx or 0),
            "sku": sku,
            "quantity": float(line.qty or 0),
            "unitPrice": round(tax_inclusive_unit_price, 4),
            "taxRate": tax_rate_pct,
            "taxValue": round(tax_value, 4),
            "taxType": int(tax_type),
        }
        line_items.append(line_item)
        line_outcomes.append(
            {
                "source_line_ref": line.item_code,
                "source_line_number": int(line.idx or 0),
                "target_field": "sku",
                "line_status": "OK",
                "reason": None,
            }
        )

    # EE's CreatePurchaseOrder requires expDeliveryDate STRICTLY after
    # today (it rejects today's date). Use DN.delivery_date when the
    # FDE set it explicitly; otherwise default to tomorrow.
    exp_delivery_raw = (
        getattr(dn, "delivery_date", None)
        or frappe.utils.add_days(frappe.utils.today(), 1)
    )
    if frappe.utils.getdate(exp_delivery_raw) <= frappe.utils.getdate(
        frappe.utils.today()
    ):
        exp_delivery_raw = frappe.utils.add_days(frappe.utils.today(), 1)
    payload: dict[str, Any] = {
        "vendorId": vendor_id,
        "referenceCode": dn.name,  # parity with STN's orderNumber=DN
        "expDeliveryDate": _fmt_date(exp_delivery_raw),
        "createOrUpdate": "I",  # §10 outbound is always create-only
        "isCancel": 0,
        "items": line_items,
        "address": (
            target_address.get("address_line1")
            or target_address.get("city")
        ),
    }

    if client is None:
        location_key = _location_key_for_warehouse(target_wh)
        client = EasyEcomClient(
            company=dn.company, location_key=location_key
        )

    try:
        response = client.post(PURCHASE_ORDER_CREATE, payload=payload)
    except EasyEcomError as exc:
        flag_text = (
            f"CreatePurchaseOrder rejected by EE: "
            f"{type(exc).__name__}: {exc}"
        )
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            map_name,
            {"status": "Drift", "flag_reason": flag_text[:1000]},
            update_modified=True,
        )
        sr = write_transfer_push_sync_record(
            dn_name=dn.name,
            company=dn.company,
            status=STATUS_FAILED,
            last_error=flag_text,
            line_outcomes=[
                {
                    **lo,
                    "line_status": "Failed",
                    "reason": f"{type(exc).__name__}",
                }
                for lo in line_outcomes
            ],
        )
        return TransferPushOutcome(
            dn_name=dn.name,
            operation="error",
            transfer_map=map_name,
            sales_invoice=sales_invoice,
            ee_doctype="PO",
            flag_reasons=[f"{type(exc).__name__}: {exc}"],
            ee_payload=payload,
            sync_record_name=sr,
            status="Drift",
        )

    ee_po_id = _extract_ee_po_id(response)

    # Same status decision as STN branch: SI-Pending if SI exists (the
    # ee_po_id presence signals EE-pushed); EE-Pushed otherwise.
    new_status = "SI-Pending" if sales_invoice else "EE-Pushed"
    frappe.db.set_value(
        "EasyEcom Transfer Map",
        map_name,
        {
            "ee_doctype": "PO",
            "ee_po_id": int(ee_po_id or 0),
            "status": new_status,
            "ecs_pending_ee_push": 0,
            "flag_reason": None,
        },
        update_modified=True,
    )
    # Back-fill the DN's §10 back-ref.
    if frappe.get_meta("Delivery Note").get_field(
        "ecs_section10_transfer_map"
    ):
        frappe.db.set_value(
            "Delivery Note",
            dn.name,
            "ecs_section10_transfer_map",
            map_name,
            update_modified=False,
        )
    if sales_invoice and frappe.get_meta("Sales Invoice").get_field(
        "ecs_section10_transfer_map"
    ):
        frappe.db.set_value(
            "Sales Invoice",
            sales_invoice,
            "ecs_section10_transfer_map",
            map_name,
            update_modified=False,
        )

    sr = write_transfer_push_sync_record(
        dn_name=dn.name,
        company=dn.company,
        status=STATUS_SUCCESS,
        last_error=None,
        line_outcomes=line_outcomes,
    )

    return TransferPushOutcome(
        dn_name=dn.name,
        operation="po_pushed",
        transfer_map=map_name,
        sales_invoice=sales_invoice,
        ee_po_id=int(ee_po_id or 0) or None,
        ee_doctype="PO",
        ee_payload=payload,
        sync_record_name=sr,
        status=new_status,
    )


# ============================================================
# Pause-gate
# ============================================================


def _is_paused() -> bool:
    """Mirrors §9's `_is_paused`. §10 outbound is a PO-shaped write —
    gates on the same auto_push_pos_on_save flag that §9 corrected
    pause_all_auto_push to cover."""
    from ecommerce_super.easyecom.flows.po_push import _is_paused as po_paused

    return po_paused()


@frappe.whitelist()
def fire_pending_transfer_pushes() -> dict[str, Any]:
    """Un-pause runner for §10 outbound.

    Walks every Transfer Map row with `ecs_pending_ee_push=1` and
    pushes each via push_one_transfer. Clears the flag on success.
    Idempotent: a Map row already past Mapped/SI-Pending (e.g.
    EE-Pushed) is skipped by push_one_transfer's own idempotency
    check on ee_order_id presence.

    Symmetric with §9 FIX 2's fire_pending_po_status_pushes.
    """
    if _is_paused():
        return {
            "ok": False,
            "message": "Still paused — fire_pending_transfer_pushes no-ops.",
            "fired": 0,
        }
    fired = 0
    skipped: list[str] = []
    pending = frappe.db.get_all(
        "EasyEcom Transfer Map",
        filters={"ecs_pending_ee_push": 1},
        fields=["name", "delivery_note"],
    )
    for row in pending:
        if not row.delivery_note:
            continue
        try:
            outcome = push_one_transfer(row.delivery_note)
            if outcome.operation in (
                "stn_pushed",
                "po_pushed",
            ):
                fired += 1
            else:
                skipped.append(
                    f"{row.delivery_note}: {outcome.operation} "
                    f"({'; '.join(outcome.flag_reasons[:2])})"
                )
        except Exception as exc:
            skipped.append(
                f"{row.delivery_note}: "
                f"{type(exc).__name__}: {exc}"
            )
    return {"ok": True, "fired": fired, "skipped": skipped}


# ============================================================
# Doc-event hooks
# ============================================================


def section10_before_save(doc: Any, method: str | None = None) -> None:
    """DN.before_save hook — derives item warehouses + addresses from
    the §10 header fields BEFORE ERPNext's own validate runs (which
    would otherwise reject items without a warehouse).

    For internal-customer DNs (the §10 trigger):
      - DN.set_warehouse + items[].warehouse  ← Transfer From
      - items[].target_warehouse              ← destination Company GIT
      - DN.customer_address + shipping_address ← Address linked to
        Transfer To Warehouse
      - DN.ecs_section10_target_warehouse mirrored from Transfer To

    Returns silently for non-internal-customer DNs.
    """
    if doc.doctype != "Delivery Note":
        return
    # §10 trigger = user-toggled "Is Internal Transfer (§10)" checkbox
    # OR (fallback) the customer is an internal customer. Either way we
    # treat it as §10 routing.
    is_section10 = int(
        getattr(doc, "ecs_is_section10_transfer", 0) or 0
    ) or int(getattr(doc, "is_internal_customer", 0) or 0)
    if not is_section10:
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return

    transfer_from = (
        getattr(doc, "ecs_section10_transfer_from_warehouse", None) or ""
    ).strip()
    transfer_to = (
        getattr(doc, "ecs_section10_transfer_to_warehouse", None) or ""
    ).strip()
    if not transfer_from or not transfer_to:
        # Let validate_pre_submit produce the user-facing error.
        return

    from ecommerce_super.easyecom.flows.transfer_inbound import (
        _resolve_git_warehouse,
    )

    destination_company = _warehouse_company(transfer_to)
    git_warehouse = _resolve_git_warehouse(destination_company)
    if not git_warehouse:
        # Surface via validate_pre_submit so the throw is consistent.
        return

    doc.ecs_section10_target_warehouse = transfer_to
    doc.set_warehouse = transfer_from

    # Override DN.represents_company per-DN so a single "Internal
    # Customer" can serve transfers to any destination Company. The
    # customer's stored represents_company is a default that we
    # override here based on the FDE-picked Transfer To Warehouse. This
    # makes ERPNext's is_internal_transfer() return True for
    # same-Company multi-WH transfers (allowing GIT routing) and False
    # for inter-Company transfers (one-sided OUT on DN, IPR handles IN).
    doc.represents_company = destination_company

    source_company = _warehouse_company(transfer_from)
    if source_company == destination_company:
        # SAME-Company multi-warehouse transfer. ERPNext's
        # `is_internal_transfer()` returns True only when
        # `represents_company == company` — i.e. only this case. Route
        # stock through GIT via items[].target_warehouse; ERPNext will
        # keep the value and validate it.
        doc.set_target_warehouse = git_warehouse
        for line in doc.items or []:
            line.warehouse = transfer_from
            line.target_warehouse = git_warehouse
    else:
        # INTER-Company transfer (represents_company != company).
        # ERPNext's `validate_internal_transfer_warehouse` will clear
        # items[].target_warehouse + DN.set_target_warehouse — so we
        # don't set those. Stock moves one-sided OUT on DN; the IPR
        # on the destination Company performs the IN side after the EE
        # GRN-Complete event lands.
        for line in doc.items or []:
            line.warehouse = transfer_from

    # Clear any stale GST fields up-front so India Compliance's
    # update_gst_details (also a before_save hook) re-derives from
    # the addresses we're about to set (or from Company defaults when
    # we can't resolve an address). Without this clear, switching the
    # buyer warehouse leaves a stale billing_address_gstin from the
    # previous DN's address on the same browser tab.
    doc.billing_address_gstin = None
    doc.place_of_supply = None
    doc.company_gstin = None

    # Auto-fill the four address fields from the Transfer From / To
    # warehouses' linked Addresses. Each address carries its own GSTIN
    # (state-derived), so this is what drives India Compliance's
    # CGST/SGST/IGST routing on the SI later.
    #
    # Buyer side — customer_address + shipping_address_name:
    #   Find an Address linked to BOTH Transfer To Warehouse AND the
    #   customer. ERPNext requires customer_address to be linked to
    #   the customer; the warehouse link makes it the right destination
    #   identity.
    #
    # Seller side — company_address + dispatch_address_name:
    #   Find an Address linked to the Transfer From Warehouse. Drives
    #   the source GSTIN on the SI (e.g., 07 Delhi for an inter-state
    #   transfer out of a Delhi WH).
    # Strict: every warehouse must have its own Address. The substrate
    # picks the Address linked to the Transfer To Warehouse (preferring
    # one ALSO linked to the customer for the joint customer+WH model).
    buyer_addr = None
    if doc.customer:
        joint = frappe.db.sql(
            """
            SELECT dl1.parent AS addr_name
            FROM `tabDynamic Link` dl1
            INNER JOIN `tabDynamic Link` dl2
              ON dl1.parent = dl2.parent
            WHERE dl1.parenttype = 'Address'
              AND dl1.link_doctype = 'Warehouse'
              AND dl1.link_name = %s
              AND dl2.parenttype = 'Address'
              AND dl2.link_doctype = 'Customer'
              AND dl2.link_name = %s
            LIMIT 1
            """,
            (transfer_to, doc.customer),
            as_dict=True,
        )
        if joint:
            buyer_addr = joint[0]["addr_name"]
    if not buyer_addr:
        wh_only = frappe.db.sql(
            """
            SELECT dl.parent AS addr_name
            FROM `tabDynamic Link` dl
            WHERE dl.parenttype = 'Address'
              AND dl.link_doctype = 'Warehouse'
              AND dl.link_name = %s
            LIMIT 1
            """,
            (transfer_to,), as_dict=True,
        )
        if wh_only:
            buyer_addr = wh_only[0]["addr_name"]
    if buyer_addr:
        doc.customer_address = buyer_addr
        doc.shipping_address_name = buyer_addr

    seller_addr = frappe.db.sql(
        """
        SELECT dl.parent AS addr_name
        FROM `tabDynamic Link` dl
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND dl.link_name = %s
        LIMIT 1
        """,
        (transfer_from,),
        as_dict=True,
    )
    if seller_addr:
        addr_name = seller_addr[0]["addr_name"]
        doc.company_address = addr_name
        doc.dispatch_address_name = addr_name
        # (GSTIN clear happens up-front; no extra clear here.)

    # Explicit tax template selection based on the source/target
    # warehouse GSTINs (India Compliance's auto-apply only fires when
    # Customer.tax_category is set; our consolidated Internal Customer
    # ships to multiple destination states so its tax_category can't
    # be hard-coded — the substrate derives the right one per DN).
    src_gstin = _warehouse_gstin(transfer_from)
    tgt_gstin = _warehouse_gstin(transfer_to)
    if src_gstin and tgt_gstin and src_gstin != tgt_gstin:
        template = frappe.db.sql_list(
            """
            SELECT name FROM `tabSales Taxes and Charges Template`
            WHERE company = %s
              AND name LIKE %s
              AND name NOT LIKE %s
            LIMIT 1
            """,
            (doc.company, "%Out-state%", "%RCM%"),
        )
        template = template[0] if template else None
        if template and not doc.taxes_and_charges:
            doc.taxes_and_charges = template
            doc.set("taxes", [])
            tpl = frappe.get_doc(
                "Sales Taxes and Charges Template", template)
            default_cc = (
                frappe.db.get_value("Company", doc.company,
                                    "cost_center") or "")
            for t in tpl.taxes or []:
                doc.append("taxes", {
                    "charge_type": t.charge_type,
                    "account_head": t.account_head,
                    "rate": t.rate,
                    "description": t.description,
                    "cost_center": t.cost_center or default_cc,
                    "included_in_print_rate": t.included_in_print_rate,
                })


def validate_pre_submit(doc: Any, method: str | None = None) -> None:
    """DN.validate hook — enforces the §10 routing contract.

    For internal-customer DNs, requires Transfer From + Transfer To
    Warehouses on the DN header (the new Customer-anchored routing
    fields). Mutations happen in section10_before_save; this hook
    only validates that the contract is satisfied.

    Returns silently for non-internal-customer DNs.
    """
    if doc.doctype != "Delivery Note":
        return
    is_section10 = int(
        getattr(doc, "ecs_is_section10_transfer", 0) or 0
    ) or int(getattr(doc, "is_internal_customer", 0) or 0)
    if not is_section10:
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return

    transfer_from = (
        getattr(doc, "ecs_section10_transfer_from_warehouse", None) or ""
    ).strip()
    transfer_to = (
        getattr(doc, "ecs_section10_transfer_to_warehouse", None) or ""
    ).strip()
    if not transfer_from or not transfer_to:
        frappe.throw(
            frappe._(
                "§10 transfer requires both Transfer From Warehouse "
                "and Transfer To Warehouse to be set on the DN header. "
                "Tick 'Is Internal Transfer (§10)' and pick the two "
                "warehouses."
            )
        )
    if transfer_from == transfer_to:
        frappe.throw(
            frappe._(
                "§10 Transfer From Warehouse and Transfer To Warehouse "
                "must differ."
            )
        )

    from ecommerce_super.easyecom.flows.transfer_inbound import (
        _resolve_git_warehouse,
    )

    destination_company = _warehouse_company(transfer_to)
    git_warehouse = _resolve_git_warehouse(destination_company)
    if not git_warehouse:
        frappe.throw(
            frappe._(
                "§10 cannot route this transfer through GIT — no "
                "Goods-In-Transit warehouse resolved for destination "
                "Company {0!r}. Either set "
                "`Company.default_in_transit_warehouse` on the "
                "destination Company, or create a Warehouse named "
                "'Goods In Transit' under it."
            ).format(destination_company)
        )

    # gh#93: block the save when any line Item is not synced to
    # EasyEcom. Pre-fix, the same check existed only post-submit in
    # `_run_preconditions` — soft, lands on Drift / Failed Sync Record
    # *after* the DN submits. Per the reporter, the FDE wants the
    # save itself blocked with an actionable message.
    #
    # Scoped to internal-customer §10 DNs (the `is_section10` gate
    # above already ensured this). Normal external-customer
    # Delivery Notes return unaffected.
    unmapped = _unmapped_items_for_dn(doc)
    if unmapped:
        item_list = ", ".join(repr(s) for s in unmapped)
        frappe.throw(
            frappe._(
                "DN line(s) reference Item(s) not yet synced to "
                "EasyEcom: {0}. Run §8d Item Push (open each Item and "
                "click Push to EasyEcom, or use Push All Pending Items "
                "from the EasyEcom Account form) before saving this "
                "internal-transfer Delivery Note."
            ).format(item_list)
        )


def enqueue_on_dn_submit(doc: Any, method: str | None = None) -> None:
    """DN.on_submit hook — Gate-0 + enqueue the §10 outbound push.

    Ping-pong guard: skip when a §10 push is mid-flight (avoid hook
    re-firing on intra-flow saves)."""
    if doc.doctype != "Delivery Note":
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return
    if not int(getattr(doc, "is_internal_customer", 0) or 0):
        return
    pair = _resolve_source_target_pair(doc)
    if pair is None:
        return  # validate_pre_submit already refused multi-pair
    source_wh, target_wh = pair
    if not _is_ee_mapped_warehouse(source_wh) and not _is_ee_mapped_warehouse(target_wh):
        return  # Gate-0 inert

    frappe.flags[PING_PONG_FLAG] = True
    try:
        push_one_transfer(doc.name)
    finally:
        frappe.flags[PING_PONG_FLAG] = False


def block_dn_cancel(doc: Any, method: str | None = None) -> None:
    """DN.on_cancel hook — §10 cancel/amend deferred until §10.G
    grounds the cancelOrder endpoint. Refuse the cancel if the DN has
    a Transfer Map row in EE-Pushed (or beyond) state.

    DNs in Mapped/Drift/SI-Pending have no EE-side state to undo —
    cancel passes through (the Transfer Map row will go orphan but
    that's the FDE's clean-up call)."""
    if doc.doctype != "Delivery Note":
        return
    map_row = frappe.db.get_value(
        "EasyEcom Transfer Map",
        {"delivery_note": doc.name},
        ["name", "status", "ee_order_id"],
        as_dict=True,
    )
    if not map_row:
        return
    if not (map_row.ee_order_id or "").strip():
        return  # Nothing pushed to EE
    if map_row.status not in (
        "EE-Pushed",
        "Partial-Received",
        "Fully-Received",
        "DN-Submitted-Locked",
    ):
        return
    frappe.throw(
        frappe._(
            "§10 STN cancel/amend not yet implemented — EE cancelOrder "
            "endpoint payload ungrounded (§10.G). DN {0} has a Transfer "
            "Map row in status {1!r} with ee_order_id={2!r}. Cancelling "
            "would desync ERPNext from EE. Contact the integration team "
            "to schedule the cancel-payload grounding."
        ).format(doc.name, map_row.status, map_row.ee_order_id)
    )


def block_dn_amend_after_submit(doc: Any, method: str | None = None) -> None:
    """DN.on_update_after_submit hook — refuse amends on EE-pushed
    transfers. Same rationale as block_dn_cancel."""
    if doc.doctype != "Delivery Note":
        return
    map_row = frappe.db.get_value(
        "EasyEcom Transfer Map",
        {"delivery_note": doc.name},
        ["name", "status", "ee_order_id"],
        as_dict=True,
    )
    if not map_row:
        return
    if not (map_row.ee_order_id or "").strip():
        return
    if map_row.status not in (
        "EE-Pushed",
        "Partial-Received",
        "Fully-Received",
    ):
        return
    frappe.throw(
        frappe._(
            "§10 STN amend not yet implemented — EE updateOrder "
            "endpoint payload ungrounded (§10.G). DN {0} has an "
            "EE-Pushed Transfer Map. Amending would desync ERPNext "
            "from EE. Contact the integration team."
        ).format(doc.name)
    )


# ============================================================
# Batch sweep
# ============================================================


@frappe.whitelist()
def push_all_pending_transfers(
    inline: int | bool | str = False,
) -> dict[str, Any]:
    """Re-push candidates — DNs that should have a Transfer Map in
    EE-Pushed (or beyond) but don't. Useful after un-pause, after
    FDE clears precondition blockers, or after EE outage recovery.

    `inline=True` runs synchronously (test helper). Default enqueues
    each candidate.
    """
    inline_mode = str(inline).strip().lower() in {"1", "true", "yes"}

    candidates = frappe.db.sql(
        """
        SELECT dn.name AS dn_name, dn.company AS company
        FROM `tabDelivery Note` dn
        LEFT JOIN `tabEasyEcom Transfer Map` xm
          ON xm.delivery_note = dn.name
        WHERE dn.docstatus = 1
          AND dn.is_internal_customer = 1
          AND (
            xm.name IS NULL
            OR xm.status IN ('Mapped', 'SI-Pending', 'Drift')
          )
        """,
        as_dict=True,
    )
    enqueued = 0
    inline_results: list[dict] = []
    for c in candidates:
        if inline_mode:
            outcome = push_one_transfer(c.dn_name)
            inline_results.append(
                {
                    "dn": c.dn_name,
                    "operation": outcome.operation,
                    "status": outcome.status,
                }
            )
        else:
            # Defer to the queue facade — keeps the user's batch click
            # non-blocking.
            from ecommerce_super.easyecom.queue import enqueue_easyecom_job
            from ecommerce_super.easyecom.utils.idempotency import (
                internal_job_key,
            )

            enqueue_easyecom_job(
                job_type="Transfer Push",
                company=c.company,
                target_doctype="Delivery Note",
                target_name=c.dn_name,
                payload={"dn_name": c.dn_name},
                idempotency_key=internal_job_key(
                    job_type="transfer_push",
                    company=c.company,
                    target_doctype="Delivery Note",
                    target_name=c.dn_name,
                ),
            )
            enqueued += 1
    return {
        "ok": True,
        "candidates_total": len(candidates),
        "enqueued": enqueued,
        "inline_results": inline_results,
    }


def transfer_push_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['Transfer Push'] dispatch — workers.execute_job
    calls this with the loaded Queue Job. Reads dn_name from the
    payload (or qj.target_name as the fallback)."""
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    dn_name = qj.target_name or payload.get("dn_name")
    if not dn_name:
        raise ValueError(
            f"Transfer Push job {qj.name} missing dn_name in "
            "payload/target_name"
        )
    frappe.flags[PING_PONG_FLAG] = True
    try:
        push_one_transfer(dn_name)
    finally:
        frappe.flags[PING_PONG_FLAG] = False


__all__ = [
    "PING_PONG_FLAG",
    "TransferPushOutcome",
    "push_one_transfer",
    "validate_pre_submit",
    "enqueue_on_dn_submit",
    "block_dn_cancel",
    "block_dn_amend_after_submit",
    "fire_pending_transfer_pushes",
    "push_all_pending_transfers",
    "transfer_push_queue_handler",
]
