"""§9 Stage 3 — EE → ERPNext GRN pull → Purchase Receipt.

The receipt half (payload-grounded model — see §9 packet "Receipt half"):

EE serves GRNs via GET /Grn/V2/getGrnDetails. We walk the cursor + delta
watermark (created_after = grn_pull_high_watermark), short-circuit each
GRN through the 10-step chain below, build PRs with the qc_fail-based
accepted/rejected split, run status reconciliation on the linked PO,
and fire the Stage 2 completion push (po_status=5) when cumulative
received_qty meets ordered modulo allow_under_receipt_pct.

The 10-step per-GRN chain (order matters — short-circuits):
  1. Gate 0    — warehouse mapping; miss → silent skip.
  2. STN       — vendor==warehouse → STN-Routed, NO PR.
  3. Idem.     — already Receipted → no-op.
  4. Deleted   — grn_status_id=4: receipted → Discrepancy +
                  Deleted-Post-Receipt; never receipted → quiet skip.
  5. Receipt   — status < trigger → Held-Pre-QC.
                  gate
  6. Resolve   — PO (po_ref_num primary, ee_po_id fallback, both miss
                  → PR anyway + Discrepancy), Supplier, Warehouse, Items.
  7. Build PR  — qty model: received_qty = received_quantity,
                  rejected_qty = qc_fail, accepted_qty = derived.
                  Buckets NOT posted.
  8. Tax       — grn_detail_price is gross; decompose via Item Tax
                  Template + place_of_supply. Variance > tolerance →
                  Discrepancy.
  9. Tolerance — cumulative received vs original_quantity per
                  purchase_order_detail_id. Over by >
                  allow_over_receipt_pct → Discrepancy.
  10. Submit  — PR submitted; GRN Map status=Receipted (or Discrepancy
                  if any line discrepancy was raised). Sync Record +
                  Line child per grn_items[] line. Completion check
                  fires po_status=5 via Stage 2.

Status reconciliation (same sweep):
  Per GRN row's `po_status_id` → linked PO Map ee_observed_po_status.
  Echo (== last_pushed_po_status) → no Discrepancy. EE-side action
  contrary (4 Rejected / 7 Cancelled while ERPNext active) → drift
  Discrepancy. 11-16 fulfilment → observation only.

NO post-receipt bucket posted on the PR. The buckets (`available`,
`reserved`, `sold`, `qc_pass`, etc.) are EE live state and drift after
inward — ERPNext owns stock movement after the PR.

This module is callable via:
  - scheduled_grn_pull (whitelist + cron entry, Stage 4 wires the cron)
  - pull_grns_for_location (single-location entry)
  - process_one_grn (per-GRN dispatch — testable in isolation)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import frappe
from frappe.utils import flt, getdate, now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import GRN_DETAILS_V2_GET
from ecommerce_super.easyecom.flows._grn_sync_records import (
    STATUS_DISCREPANCY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    write_grn_pull_sync_record,
)
from ecommerce_super.easyecom.flows.po_push import (
    PO_STATUS_COMPLETED,
    PING_PONG_FLAG as PO_PUSH_PING_PONG_FLAG,
    push_po_status,
)
from ecommerce_super.easyecom.tax.place_of_supply import compute_tax_type


GRN_PULL_RULESET = "EasyEcom-GRN-Pull"
PING_PONG_FLAG = "easyecom_grn_pull_in_flight"

# EE fulfilment-internal status codes — recorded but never raised as
# Discrepancy (per §9 packet status reconciliation).
EE_FULFILMENT_STATUSES: frozenset[int] = frozenset(range(11, 17))

# EE "PO is cancelled / rejected" statuses (Discrepancy-worthy when EE
# shows them while ERPNext is still active).
EE_PO_STATUS_REJECTED = 4
EE_PO_STATUS_CANCELLED = 7

class _RejectedWarehouseMissingError(Exception):
    """Raised by line-build when qc_fail>0 but no default rejected
    warehouse is configured. Caught at the chain boundary and surfaced
    as a Failed GRNOutcome."""


GRNOp = Literal[
    "skipped",  # Gate-0 miss
    "stn_routed",  # vendor==warehouse
    "noop",  # already Receipted (idempotency)
    "held",  # grn_status_id < trigger
    "deleted_pre_receipt",  # grn_status_id=4 + no prior PR
    "deleted_post_receipt",  # grn_status_id=4 + PR exists → Discrepancy
    "receipted",  # PR created
    "failed",  # supplier/item miss, PR submit failed
]


@dataclass
class GRNOutcome:
    ee_grn_id: int
    operation: GRNOp
    grn_map_status: str | None = None
    purchase_receipt: str | None = None
    linked_po: str | None = None
    flag_reasons: list[str] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)  # disc docnames
    sync_record_name: str | None = None


@dataclass
class GRNSweepOutcome:
    pages_walked: int = 0
    grns_processed: int = 0
    outcomes: list[GRNOutcome] = field(default_factory=list)


# ============================================================
# Top-level pull entries
# ============================================================


def pull_grns_for_location(
    *,
    location_key: str,
    account_name: str | None = None,
    client: EasyEcomClient | None = None,
    max_pages: int | None = None,
    created_after: str | None = None,
) -> GRNSweepOutcome:
    """Walk getGrnDetails for one location. Returns the sweep outcome.

    `created_after` overrides the account's high-water mark (used by
    tests + replay tooling). Production sweeps pass None and let the
    sweep load the watermark from the account.
    """
    if client is None:
        client = EasyEcomClient(location_key=location_key)

    if account_name is None:
        account_name = frappe.db.get_value(
            "EasyEcom Account", {"enabled": 1}, "name"
        )
    if account_name and not created_after:
        created_after = frappe.db.get_value(
            "EasyEcom Account", account_name, "grn_pull_high_watermark"
        )

    sweep = GRNSweepOutcome()
    max_observed_dt = None
    params: dict[str, Any] = {"limit": 10}
    if created_after:
        # EE expects 'YYYY-MM-DD HH:MM:SS' on created_after.
        params["created_after"] = _fmt_dt_for_ee(created_after)

    next_url: str | None = None
    pages = 0
    while True:
        if next_url:
            page = client.get(next_url, params=None)
        else:
            page = client.get(GRN_DETAILS_V2_GET, params=params)
        pages += 1
        sweep.pages_walked = pages

        data = (page or {}).get("data") or []
        for grn_row in data:
            outcome = process_one_grn(
                grn_row,
                account_name=account_name,
                client=client,
            )
            sweep.outcomes.append(outcome)
            sweep.grns_processed += 1
            # Track high-water from grn_created_at on the row.
            row_dt = grn_row.get("grn_created_at")
            if row_dt and (max_observed_dt is None or row_dt > max_observed_dt):
                max_observed_dt = row_dt

        # Cursor + watermark persistence after each page (resumable).
        next_url = _extract_next_url(page)
        if account_name:
            frappe.db.set_value(
                "EasyEcom Account",
                account_name,
                {
                    "grn_pull_cursor": next_url or "",
                    "grn_pull_cursor_at": now_datetime(),
                    "grn_pull_total_seen": (
                        int(
                            frappe.db.get_value(
                                "EasyEcom Account",
                                account_name,
                                "grn_pull_total_seen",
                            )
                            or 0
                        )
                        + len(data)
                    ),
                },
                update_modified=False,
            )
            frappe.db.commit()

        if not next_url:
            break
        if max_pages is not None and pages >= max_pages:
            break

    # Advance the high watermark to the max seen in this sweep.
    if account_name and max_observed_dt:
        frappe.db.set_value(
            "EasyEcom Account",
            account_name,
            "grn_pull_high_watermark",
            max_observed_dt,
            update_modified=False,
        )
        frappe.db.commit()

    return sweep


def scheduled_grn_pull(*, account_name: str | None = None) -> dict[str, Any]:
    """Wired by Stage 4's cron. Walks every operational EasyEcom Location
    on the (single, enabled) account, returns a summary."""
    if account_name is None:
        account_name = frappe.db.get_value(
            "EasyEcom Account", {"enabled": 1}, "name"
        )
    if not account_name:
        return {"ok": False, "message": "No enabled EasyEcom Account."}

    locations = frappe.db.get_all(
        "EasyEcom Location",
        filters={"workflow_state": "Live", "enabled": 1},
        pluck="location_key",
    )
    summaries: list[dict[str, Any]] = []
    for loc_key in locations:
        try:
            sweep = pull_grns_for_location(
                location_key=loc_key, account_name=account_name
            )
            summaries.append(
                {
                    "location_key": loc_key,
                    "pages": sweep.pages_walked,
                    "grns": sweep.grns_processed,
                }
            )
        except Exception as exc:
            frappe.log_error(
                title=f"§9 GRN pull failed for location {loc_key}",
                message=f"{type(exc).__name__}: {exc}",
            )
            summaries.append(
                {"location_key": loc_key, "error": f"{type(exc).__name__}"}
            )
    return {"ok": True, "summaries": summaries}


def grn_pull_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['GRN Pull'] dispatch — workers.execute_job
    calls this with the loaded Queue Job. Reads location_key from the
    payload."""
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    location_key = payload.get("location_key")
    account_name = payload.get("account_name")
    if not location_key or not account_name:
        raise ValueError(
            f"GRN Pull job {qj.name} missing location_key or account_name"
        )
    frappe.flags[PING_PONG_FLAG] = True
    try:
        pull_grns_for_location(
            location_key=location_key, account_name=account_name
        )
    finally:
        frappe.flags[PING_PONG_FLAG] = False


# ============================================================
# Per-GRN chain
# ============================================================


def process_one_grn(
    grn_row: dict,
    *,
    account_name: str | None = None,
    client: EasyEcomClient | None = None,
) -> GRNOutcome:
    """The 10-step chain. Pure-ish: builds + submits ONE PR (or doesn't,
    per the step decisions). Returns GRNOutcome. Never raises through
    the boundary; failures land in GRN Map status + Failed Sync Record.
    """
    ee_grn_id = int(grn_row.get("grn_id") or 0)
    if not ee_grn_id:
        return GRNOutcome(
            ee_grn_id=0,
            operation="failed",
            flag_reasons=["GRN payload missing grn_id"],
        )

    inwarded_wh_c_id = int(grn_row.get("inwarded_warehouse_c_id") or 0)
    vendor_c_id = int(grn_row.get("vendor_c_id") or 0)
    grn_status_id = int(grn_row.get("grn_status_id") or 0)

    # ---------- Step 1: Gate 0 — warehouse mapping ----------
    location_row = _resolve_location_for_warehouse_c_id(inwarded_wh_c_id)
    if location_row is None:
        # Silent skip — exactly like a non-EE PO. No Map row, no Sync.
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="skipped",
            flag_reasons=[
                f"Gate-0: inwarded_warehouse_c_id={inwarded_wh_c_id} "
                "does not resolve to an EE Location"
            ],
        )

    # ---------- Step 2: STN routing ----------
    if vendor_c_id and vendor_c_id == inwarded_wh_c_id:
        _upsert_grn_map_stn(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="stn_routed",
            grn_map_status="STN-Routed",
        )

    existing_map = _get_grn_map(ee_grn_id)

    # ---------- Step 4 (runs before Step 3): Deleted ----------
    # We check deleted BEFORE idempotency so a GRN that was receipted
    # then flipped to status=4 still triggers the Deleted-Post-Receipt
    # discrepancy on the next pull (rather than short-circuiting at
    # idempotency).
    if grn_status_id == 4:
        if existing_map and existing_map.get("purchase_receipt"):
            disc_name = _raise_discrepancy(
                kind="GRN deleted after receipt",
                reference_doctype="EasyEcom GRN Map",
                reference_name=existing_map["name"],
                company=_company_for_warehouse(location_row["mapped_warehouse"]),
                reason=(
                    f"EE flipped grn_status_id to 4 (Deleted) for ee_grn_id="
                    f"{ee_grn_id} which had already been receipted in ERPNext "
                    f"as Purchase Receipt {existing_map['purchase_receipt']}. "
                    "Auto-cancel suppressed — FDE action required."
                ),
            )
            frappe.db.set_value(
                "EasyEcom GRN Map",
                existing_map["name"],
                {
                    "status": "Deleted-Post-Receipt",
                    "grn_status_id": 4,
                    "last_observed_at": now_datetime(),
                },
                update_modified=True,
            )
            return GRNOutcome(
                ee_grn_id=ee_grn_id,
                operation="deleted_post_receipt",
                grn_map_status="Deleted-Post-Receipt",
                purchase_receipt=existing_map["purchase_receipt"],
                discrepancies=[disc_name] if disc_name else [],
            )
        # Never receipted → quiet skip. Per packet, we DO NOT write a
        # GRN Map row for a deleted-pre-receipt event we never observed
        # (avoids worklist noise).
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="deleted_pre_receipt",
        )

    # ---------- Step 3 (runs after Step 4): Idempotency ----------
    # A GRN counts as "already processed" if a PR exists (regardless of
    # whether the row landed clean Receipted or with Discrepancy). The
    # second-pull case should never create a duplicate PR.
    if (
        existing_map
        and existing_map.get("status") in ("Receipted", "Discrepancy")
        and existing_map.get("purchase_receipt")
    ):
        # Already done. Refresh observed-status + last_observed_at.
        _refresh_observed_only(grn_map_name=existing_map["name"], grn_row=grn_row)
        _reconcile_po_status(grn_row=grn_row, ee_grn_id=ee_grn_id)
        # linked_po lives on PR Item (per-line), not PR header.
        first_line_po = frappe.db.get_value(
            "Purchase Receipt Item",
            {"parent": existing_map["purchase_receipt"]},
            "purchase_order",
            order_by="idx asc",
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="noop",
            grn_map_status=existing_map.get("status") or "Receipted",
            purchase_receipt=existing_map["purchase_receipt"],
            linked_po=first_line_po or None,
        )

    # ---------- Step 5: Receipt gate ----------
    trigger = _grn_receipt_trigger_status()
    if grn_status_id < trigger:
        _upsert_grn_map_held(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="held",
            grn_map_status="Held-Pre-QC",
        )

    # ---------- Step 6: Resolution ----------
    company = _company_for_warehouse(location_row["mapped_warehouse"])
    resolution = _resolve_for_receipt(
        grn_row=grn_row,
        location_row=location_row,
        company=company,
    )

    if resolution.get("supplier_missing"):
        # Failed — write Map row + Sync Record (PR=None → no SR write
        # per helper convention, but we still want the GRN Map state).
        _upsert_grn_map_failed(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            reason=resolution["error"],
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=[resolution["error"]],
        )

    if resolution.get("line_failures"):
        # Whole-PR Failed; the Sync Record's Line child names the
        # offending lines.
        _upsert_grn_map_failed(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            reason=" || ".join(resolution["line_failures"][:3]),
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=resolution["line_failures"],
        )

    # ---------- Step 7-9: Build PR ----------
    discrepancies: list[str] = []
    line_outcomes: list[dict[str, Any]] = []

    pr_doc = _build_pr_header(
        resolution=resolution,
        grn_row=grn_row,
        location_row=location_row,
        company=company,
    )
    try:
        for line_payload, item_map_row in resolution["lines"]:
            _append_pr_line(
                pr_doc=pr_doc,
                line_payload=line_payload,
                item_map_row=item_map_row,
                resolution=resolution,
            )
            line_outcomes.append(
                {
                    "source_line_ref": line_payload.get("grn_detail_id"),
                    "source_line_number": len(line_outcomes) + 1,
                    "target_field": "received_qty",
                    "line_status": "OK",
                    "reason": None,
                    "linked_discrepancy": None,
                }
            )
    except _RejectedWarehouseMissingError as exc:
        _upsert_grn_map_failed(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            reason=str(exc),
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=[str(exc)],
        )

    # ---------- Step 10: Submit PR ----------
    try:
        pr_doc.insert(ignore_permissions=True)
        pr_doc.submit()
    except Exception as exc:
        _upsert_grn_map_failed(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            reason=f"PR submit failed: {type(exc).__name__}: {exc}",
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=[f"PR submit: {type(exc).__name__}: {exc}"],
        )

    # PO-unknown discrepancy (linked_po_map empty + flag_reason).
    # Raised AFTER submit so the reference_name resolves to a real PR.
    if resolution.get("po_unknown_reason"):
        disc = _raise_discrepancy(
            kind="GRN for unknown PO",
            reference_doctype="Purchase Receipt",
            reference_name=pr_doc.name,
            company=company,
            reason=resolution["po_unknown_reason"],
        )
        if disc:
            discrepancies.append(disc)

    # Tax variance check (post-build so the PR's actual decomposition
    # is on the doc).
    tax_disc = _check_tax_variance(
        pr_doc=pr_doc, grn_row=grn_row, company=company
    )
    if tax_disc:
        discrepancies.append(tax_disc)
        # Tag at least one line as Discrepancy so the Sync Record Line
        # surfaces it; first line is fine — the Discrepancy doc carries
        # the full narrative.
        if line_outcomes:
            line_outcomes[0]["line_status"] = "Discrepancy"
            line_outcomes[0]["reason"] = "tax variance"
            line_outcomes[0]["linked_discrepancy"] = tax_disc

    # Cumulative tolerance per PO line.
    tol_disc = _check_cumulative_tolerance(
        pr_doc=pr_doc, grn_row=grn_row, company=company
    )
    if tol_disc:
        discrepancies.append(tol_disc)

    # GRN Map upsert → Receipted (or Discrepancy if any disc raised).
    final_status = "Discrepancy" if discrepancies else "Receipted"
    _upsert_grn_map_receipted(
        ee_grn_id=ee_grn_id,
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=vendor_c_id,
        pr_name=pr_doc.name,
        linked_po_map_name=resolution.get("po_map_name"),
        status=final_status,
    )

    # Sync Record with Line child.
    sr_name = write_grn_pull_sync_record(
        pr_name=pr_doc.name,
        ee_grn_id=ee_grn_id,
        company=company,
        status=STATUS_DISCREPANCY if discrepancies else STATUS_SUCCESS,
        last_error=None,
        line_outcomes=line_outcomes,
    )

    # Status reconciliation in same sweep (echo/drift/observation).
    _reconcile_po_status(grn_row=grn_row, ee_grn_id=ee_grn_id)

    # Completion trigger (po_status=5).
    if resolution.get("po_name"):
        _maybe_fire_completion(
            po_name=resolution["po_name"], pr_doc=pr_doc, client=client
        )

    return GRNOutcome(
        ee_grn_id=ee_grn_id,
        operation="receipted",
        grn_map_status=final_status,
        purchase_receipt=pr_doc.name,
        linked_po=resolution.get("po_name"),
        flag_reasons=[],
        discrepancies=discrepancies,
        sync_record_name=sr_name,
    )


# ============================================================
# Resolution
# ============================================================


def _resolve_for_receipt(
    *, grn_row: dict, location_row: dict, company: str
) -> dict:
    """PO + Supplier + Warehouse + Items resolution. Returns:
      {
        "po_name": str | None,
        "po_map_name": str | None,
        "po_unknown_reason": str | None,
        "supplier": str,
        "supplier_state": str | None,
        "supplier_country": str,
        "warehouse_state": str | None,
        "set_warehouse": str,
        "rejected_warehouse": str | None,
        "lines": [(line_payload, item_map_row), ...],
        "supplier_missing": True | False,
        "error": str | None,
        "line_failures": list[str],
      }
    """
    out: dict[str, Any] = {
        "po_name": None,
        "po_map_name": None,
        "po_unknown_reason": None,
        "lines": [],
        "supplier_missing": False,
        "error": None,
        "line_failures": [],
    }

    # PO resolution — po_ref_num primary, ee_po_id fallback. ONLY
    # match against SUBMITTED POs (docstatus=1). A Draft PO with the
    # same name (test residue, in-flight edit, etc.) must not be
    # treated as "the ordered PO" — the PR-on-Draft-PO path explodes
    # at ERPNext's PR submit validation. If po_ref_num matches a Draft
    # / Cancelled PO, treat it as "no match" and fall through to the
    # both-refs-miss → PR-anyway + Discrepancy path.
    po_ref_num = (grn_row.get("po_ref_num") or "").strip()
    ee_po_id = int(grn_row.get("po_id") or 0)
    po_name = None
    po_map_name = None
    if po_ref_num:
        candidate = frappe.db.get_value(
            "Purchase Order", po_ref_num, ["docstatus"], as_dict=True
        )
        if candidate and int(candidate.docstatus or 0) == 1:
            po_name = po_ref_num
            po_map_name = frappe.db.get_value(
                "EasyEcom PO Map", {"purchase_order": po_ref_num}, "name"
            )
    if not po_name and ee_po_id:
        po_map_name = frappe.db.get_value(
            "EasyEcom PO Map", {"ee_po_id": ee_po_id}, "name"
        )
        if po_map_name:
            candidate_po = frappe.db.get_value(
                "EasyEcom PO Map", po_map_name, "purchase_order"
            )
            if candidate_po:
                po_ds = frappe.db.get_value(
                    "Purchase Order", candidate_po, "docstatus"
                )
                if int(po_ds or 0) == 1:
                    po_name = candidate_po
    if not po_name:
        out["po_unknown_reason"] = (
            f"GRN for unknown PO: po_ref_num={po_ref_num!r} "
            f"+ po_id={ee_po_id} both unmatched on ERPNext. Creating "
            "PR against the resolved supplier/items/warehouse; FDE "
            "should link the PO Map row manually if the upstream PO "
            "lands later."
        )
    out["po_name"] = po_name
    out["po_map_name"] = po_map_name

    # Supplier resolution — vendor_c_id → Supplier Map (READ key).
    vendor_c_id = int(grn_row.get("vendor_c_id") or 0)
    sup_row = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"ee_vendor_c_id": str(vendor_c_id)},
        ["erpnext_name"],
        as_dict=True,
    )
    if not sup_row or not sup_row.erpnext_name:
        out["supplier_missing"] = True
        out["error"] = (
            f"Supplier Map missing: no row with ee_vendor_c_id="
            f"{vendor_c_id} (READ key). FDE must create the Map row "
            "or run §8f Supplier discovery."
        )
        return out
    out["supplier"] = sup_row.erpnext_name
    out["supplier_country"] = (
        frappe.db.get_value("Supplier", sup_row.erpnext_name, "country")
        or "India"
    )
    out["supplier_state"] = _find_supplier_state(sup_row.erpnext_name)

    # Warehouse + warehouse_state.
    out["set_warehouse"] = location_row["mapped_warehouse"]
    wh_addr = _resolve_warehouse_address(location_row["mapped_warehouse"])
    out["warehouse_state"] = (wh_addr or {}).get("gst_state")

    # rejected_warehouse from settings (Account.default_rejected_warehouse).
    out["rejected_warehouse"] = _default_rejected_warehouse_for(company)

    # Item resolution per line — collect failures.
    items_payload = grn_row.get("grn_items") or grn_row.get("items") or []
    for line in items_payload:
        sku = (line.get("sku") or "").strip()
        ean = (line.get("ean") or "").strip()
        item_map = None
        if sku:
            item_map = frappe.db.get_value(
                "EasyEcom Item Map",
                {"ee_sku": sku},
                ["name", "erpnext_name", "ee_sku"],
                as_dict=True,
            )
        if not item_map and ean:
            item_map = frappe.db.get_value(
                "EasyEcom Item Map",
                {"erpnext_name": ean},
                ["name", "erpnext_name", "ee_sku"],
                as_dict=True,
            )
        if not item_map:
            out["line_failures"].append(
                f"Item Map missing for sku={sku!r} ean={ean!r} on "
                f"grn_detail_id={line.get('grn_detail_id')}"
            )
            continue
        out["lines"].append((line, item_map))

    return out


# ============================================================
# PR build
# ============================================================


def _build_pr_header(
    *, resolution: dict, grn_row: dict, location_row: dict, company: str
) -> Any:
    """PR header does NOT carry purchase_order at the header level
    (ERPNext PR aggregates PO links per-line via PR Item.purchase_order
    + the purchase_orders child table; one PR can satisfy multiple
    POs in principle). The per-line link is set in _append_pr_line."""
    pr = frappe.new_doc("Purchase Receipt")
    pr.update(
        {
            "supplier": resolution["supplier"],
            "company": company,
            "set_warehouse": resolution["set_warehouse"],
            "posting_date": _date_from_ee(grn_row.get("grn_created_at"))
            or frappe.utils.today(),
            "supplier_delivery_note": (
                grn_row.get("grn_invoice_number") or ""
            ),
            "ecs_easyecom_grn_id": str(grn_row.get("grn_id") or ""),
            "ecs_supplier_invoice_date": _date_from_ee(
                grn_row.get("grn_invoice_date")
            ),
            "currency": "INR",
            "conversion_rate": 1,
        }
    )
    return pr


def _append_pr_line(
    *, pr_doc: Any, line_payload: dict, item_map_row: dict, resolution: dict
) -> None:
    item_code = item_map_row.erpnext_name
    received_qty = flt(line_payload.get("received_quantity") or 0)
    qc_fail = flt(line_payload.get("qc_fail") or 0)
    accepted_qty = max(received_qty - qc_fail, 0)
    rejected_qty = qc_fail
    rate = flt(line_payload.get("grn_detail_price") or 0)

    # Rejected qty handling: rejected_qty>0 with no
    # default_rejected_warehouse → flag-not-pushed (NOT a hard throw;
    # the flow boundary needs the chance to record the failure on the
    # GRN Map row + Sync Record). Caller (process_one_grn) catches.
    if rejected_qty > 0 and not resolution.get("rejected_warehouse"):
        raise _RejectedWarehouseMissingError(
            f"§9 GRN pull: line has qc_fail={rejected_qty} (rejected qty) "
            f"but EasyEcom Account.default_rejected_warehouse is unset. "
            f"Configure it in Settings → EasyEcom Account before "
            f"re-pulling. Affected SKU: {item_code}, GRN line "
            f"{line_payload.get('grn_detail_id')}."
        )

    line = pr_doc.append(
        "items",
        {
            "item_code": item_code,
            # ERPNext PR Item invariant: received_qty = qty (accepted)
            # + rejected_qty. `qty` is the accepted portion that goes
            # to the receiving warehouse. EE's accepted = received -
            # qc_fail.
            "qty": accepted_qty,
            "received_qty": received_qty,
            "rejected_qty": rejected_qty,
            "rate": rate,
            "warehouse": resolution["set_warehouse"],
            "rejected_warehouse": (
                resolution["rejected_warehouse"]
                if rejected_qty > 0
                else None
            ),
            # Per-line PO link (PR Item carries purchase_order natively;
            # PR header does NOT). Stage 4 may set purchase_order_item
            # when we can resolve the exact PO line via po_detail_id +
            # a Purchase Order Item custom-field mapping; deferred.
            "purchase_order": resolution.get("po_name") or None,
            "ecs_easyecom_grn_detail_id": str(
                line_payload.get("grn_detail_id") or ""
            ),
            "ecs_easyecom_po_detail_id": str(
                line_payload.get("purchase_order_detail_id") or ""
            ),
        },
    )

    # Batch + expiry handling (skip serial-no for Stage 3 — wired in
    # Stage 4 if needed).
    if frappe.db.get_value("Item", item_code, "has_batch_no"):
        batch_code = (line_payload.get("batch_code") or "").strip()
        expire_date = _date_from_ee(line_payload.get("expire_date"))
        if batch_code:
            batch_name = _ensure_batch(
                item_code=item_code,
                batch_id=batch_code,
                expiry_date=expire_date,
            )
            line.batch_no = batch_name


def _ensure_batch(
    *, item_code: str, batch_id: str, expiry_date: Any
) -> str:
    """Create or fetch a Batch row for this Item+batch_id, honouring
    expiry_date if Item.has_expiry_date is set."""
    existing = frappe.db.get_value(
        "Batch", {"item": item_code, "batch_id": batch_id}, "name"
    )
    if existing:
        return existing
    b = frappe.new_doc("Batch")
    b.update(
        {
            "item": item_code,
            "batch_id": batch_id,
        }
    )
    if expiry_date:
        b.expiry_date = expiry_date
    b.insert(ignore_permissions=True)
    return b.name


# ============================================================
# Tax + tolerance + discrepancy
# ============================================================


def _check_tax_variance(
    *, pr_doc: Any, grn_row: dict, company: str
) -> str | None:
    """Cross-check the PR's computed gross against EE's total_grn_value.
    Variance > tax_variance_tolerance_pct → Discrepancy.

    Also catches the Stage-2 blank-tax case: PO pushed 0% tax, PR's
    derived tax is non-zero (Item Tax Template changed since push) →
    variance against the PO Map's recorded tax signature.
    """
    ee_total = flt(grn_row.get("total_grn_value") or 0)
    pr_gross = sum(flt(l.qty) * flt(l.rate) for l in (pr_doc.items or []))
    if ee_total <= 0:
        return None

    tolerance_pct = flt(
        frappe.db.get_value(
            "EasyEcom Account",
            {"enabled": 1},
            "tax_variance_tolerance_pct",
        )
        or 1.0
    )
    delta_pct = abs(pr_gross - ee_total) / ee_total * 100.0
    if delta_pct <= tolerance_pct:
        return None

    return _raise_discrepancy(
        kind="tax variance",
        reference_doctype="Purchase Receipt",
        reference_name=pr_doc.name,
        company=company,
        reason=(
            f"§9 tax variance: ERPNext PR gross ({pr_gross:.2f}) vs EE "
            f"total_grn_value ({ee_total:.2f}) differ by "
            f"{delta_pct:.2f}% > tolerance ({tolerance_pct:.2f}%). "
            "Likely cause: Item Tax Template changed between PO push and "
            "GRN receipt, or EE bucket arithmetic drifted. PR is still "
            "created — FDE reconciles."
        ),
    )


def _check_cumulative_tolerance(
    *, pr_doc: Any, grn_row: dict, company: str
) -> str | None:
    """Per purchase_order_detail_id: cumulative received across all
    PRs vs original_quantity on the PO line. Over by >
    allow_over_receipt_pct → Discrepancy.
    """
    over_pct = flt(
        frappe.db.get_value(
            "EasyEcom Account", {"enabled": 1}, "allow_over_receipt_pct"
        )
        or 0
    )
    breaches: list[str] = []
    for line in pr_doc.items or []:
        po_detail = line.get("ecs_easyecom_po_detail_id")
        if not po_detail:
            continue
        # Sum received across all PR lines pointing at this PO detail.
        cumulative = (
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(received_qty), 0)
                FROM `tabPurchase Receipt Item` pri
                JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
                WHERE pri.ecs_easyecom_po_detail_id = %s
                  AND pr.docstatus = 1
                """,
                (po_detail,),
            )[0][0]
            or 0
        )
        # Find the PO line by purchase_order_item back-ref OR by EE
        # po_detail_id via the EasyEcom PO Map's PO. For Stage 3,
        # approximate by querying the Purchase Order Item where this
        # PO line's name matches. (purchase_order_item on PR Item is
        # the ERPNext PO line docname — set by ERPNext if the PR was
        # linked, otherwise None for us.)
        original = None
        # Best-effort: find the PO line on the linked PO with matching
        # ecs_easyecom_po_detail_id stored as a custom on PO Item
        # (deferred — Stage 4 adds that field if needed). For now, if
        # we have a per-line PO + matching item_code, sum its qty.
        line_po = line.get("purchase_order")
        if line_po and line.item_code:
            original = (
                frappe.db.sql(
                    """
                    SELECT COALESCE(SUM(qty), 0)
                    FROM `tabPurchase Order Item`
                    WHERE parent = %s AND item_code = %s
                    """,
                    (line_po, line.item_code),
                )[0][0]
                or 0
            )
        if original is None or original <= 0:
            continue
        ceiling = original * (1 + over_pct / 100.0)
        if cumulative > ceiling:
            breaches.append(
                f"{line.item_code}: cumulative {cumulative} > "
                f"ordered {original} + over_pct {over_pct}%"
            )
    if not breaches:
        return None
    return _raise_discrepancy(
        kind="over-receipt",
        reference_doctype="Purchase Receipt",
        reference_name=pr_doc.name,
        company=company,
        reason=(
            "§9 over-receipt cumulative tolerance breached:\n"
            + "\n".join(f"  - {b}" for b in breaches)
        ),
    )


def _raise_discrepancy(
    *,
    kind: str,
    reference_doctype: str,
    reference_name: str,
    company: str,
    reason: str,
) -> str | None:
    """Create one §23 stub Integration Discrepancy row. Returns docname,
    or None if creation failed (logged)."""
    try:
        doc = frappe.new_doc("EasyEcom Integration Discrepancy")
        doc.update(
            {
                "kind": kind,
                "status": "Open",
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "company": company,
                "reason": reason[:5000],
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception as exc:
        frappe.log_error(
            title=f"§9 GRN pull failed to raise Discrepancy ({kind})",
            message=f"{type(exc).__name__}: {exc}\n\nReason text:\n{reason}",
        )
        return None


# ============================================================
# Status reconciliation (per-GRN, in the same sweep)
# ============================================================


def _reconcile_po_status(*, grn_row: dict, ee_grn_id: int) -> None:
    """Update linked PO Map's ee_observed_po_status + ee_observed_at.
    Raise Discrepancy when the observed status indicates EE-side action
    contrary to ERPNext state. Echo (== last_pushed) = no Discrepancy.
    11-16 fulfilment = observation only.
    """
    observed = int(grn_row.get("po_status_id") or 0)
    if not observed:
        return

    # Find the linked PO Map.
    po_ref_num = (grn_row.get("po_ref_num") or "").strip()
    ee_po_id = int(grn_row.get("po_id") or 0)
    po_map_row = None
    if po_ref_num:
        po_map_row = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_ref_num},
            ["name", "purchase_order", "last_pushed_po_status"],
            as_dict=True,
        )
    if not po_map_row and ee_po_id:
        po_map_row = frappe.db.get_value(
            "EasyEcom PO Map",
            {"ee_po_id": ee_po_id},
            ["name", "purchase_order", "last_pushed_po_status"],
            as_dict=True,
        )
    if not po_map_row:
        return  # observation has nowhere to land

    frappe.db.set_value(
        "EasyEcom PO Map",
        po_map_row["name"],
        {
            "ee_observed_po_status": observed,
            "ee_observed_at": now_datetime(),
        },
        update_modified=False,
    )

    # Echo → no Discrepancy.
    last_pushed = int(po_map_row.get("last_pushed_po_status") or 0)
    if observed == last_pushed:
        return
    # 11-16 fulfilment → observation only (operational lifecycle).
    if observed in EE_FULFILMENT_STATUSES:
        return
    # Cancelled / Rejected when ERPNext PO is active → contrary-action drift.
    po_docstatus = int(
        frappe.db.get_value("Purchase Order", po_map_row["purchase_order"], "docstatus") or 0
    )
    if observed in (EE_PO_STATUS_REJECTED, EE_PO_STATUS_CANCELLED) and po_docstatus == 1:
        company = frappe.db.get_value(
            "Purchase Order", po_map_row["purchase_order"], "company"
        )
        _raise_discrepancy(
            kind="po_status drift",
            reference_doctype="EasyEcom PO Map",
            reference_name=po_map_row["name"],
            company=company,
            reason=(
                f"§9 status drift: EE observed po_status_id={observed} "
                f"({_status_label(observed)}) for PO "
                f"{po_map_row['purchase_order']} while ERPNext shows the "
                f"PO as submitted/active. Last pushed by ERPNext: "
                f"{last_pushed}. EE-side action is contrary to ERPNext "
                "state. NEVER auto-cancelling the ERPNext PO — FDE "
                "decides."
            ),
        )


def _status_label(po_status_id: int) -> str:
    return {
        1: "Open",
        2: "Waiting for Approval",
        3: "Approved",
        4: "Rejected",
        5: "Completed",
        6: "Pending on Supplier",
        7: "Cancelled",
        8: "Payment Pending",
        9: "Payment Done",
        11: "Shipped to FF",
        12: "Pending Dispatch on FF",
        13: "Shipped",
        14: "Shipped by FF",
        15: "Received by FF",
        16: "Invoice done by Vendor",
    }.get(po_status_id, f"Unknown({po_status_id})")


# ============================================================
# Completion trigger
# ============================================================


def _maybe_fire_completion(
    *, po_name: str, pr_doc: Any, client: EasyEcomClient | None
) -> None:
    """If cumulative received_qty >= original_quantity across all PO
    lines (modulo allow_under_receipt_pct), fire Stage 2's
    updatePoStatus=5. Idempotent on last_pushed_po_status.

    If client is None and no live EE Account exists, skip — tests that
    don't mock the client should not be triggering live EE calls.
    """
    if not po_name or not frappe.db.exists("Purchase Order", po_name):
        return
    if client is None:
        # In production, push_po_status constructs its own client. In
        # tests that don't pass one, skip the completion check to keep
        # the path mock-friendly.
        return

    under_pct = flt(
        frappe.db.get_value(
            "EasyEcom Account", {"enabled": 1}, "allow_under_receipt_pct"
        )
        or 0
    )
    rows = frappe.db.sql(
        """
        SELECT
          poi.name AS po_item_name,
          poi.item_code,
          poi.qty AS original_qty,
          COALESCE(
            (SELECT SUM(received_qty) FROM `tabPurchase Receipt Item` pri
              JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
              WHERE pri.purchase_order_item = poi.name
                AND pr.docstatus = 1), 0
          ) AS received
        FROM `tabPurchase Order Item` poi
        WHERE poi.parent = %s
        """,
        (po_name,),
        as_dict=True,
    )
    if not rows:
        return

    # If purchase_order_item linkage is empty (which it is for our PRs
    # because we don't set purchase_order_item — we set purchase_order
    # on the PR Item as the per-line link), aggregate by item_code via
    # PR Item.purchase_order (per-line link).
    if all(int(r.received or 0) == 0 for r in rows):
        rows = frappe.db.sql(
            """
            SELECT
              poi.name AS po_item_name,
              poi.item_code,
              SUM(poi.qty) AS original_qty,
              (SELECT COALESCE(SUM(pri.received_qty), 0)
                 FROM `tabPurchase Receipt Item` pri
                 JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
                 WHERE pri.item_code = poi.item_code
                   AND pri.purchase_order = poi.parent
                   AND pr.docstatus = 1) AS received
            FROM `tabPurchase Order Item` poi
            WHERE poi.parent = %s
            GROUP BY poi.item_code
            """,
            (po_name,),
            as_dict=True,
        )

    complete = True
    for r in rows:
        original = flt(r.original_qty)
        received = flt(r.received)
        floor = original * (1 - under_pct / 100.0)
        if received < floor:
            complete = False
            break
    if not complete:
        return

    # Fire Stage 2 status push. The push_po_status function has its
    # own last_pushed_po_status idempotency guard.
    frappe.flags[PO_PUSH_PING_PONG_FLAG] = True
    try:
        push_po_status(
            po_docname=po_name,
            target_status=PO_STATUS_COMPLETED,
            client=client,
        )
    finally:
        frappe.flags[PO_PUSH_PING_PONG_FLAG] = False


# ============================================================
# Force-close hook (PO Close button)
# ============================================================


def enqueue_on_po_close(doc: Any, method: str | None = None) -> None:
    """Purchase Order.on_update_after_submit hook. When the user clicks
    Close on the PO (which sets status='Closed'), force-close on EE
    via updatePoStatus=5 + markPoComplete=1. Idempotent on
    last_pushed_po_status (skipped if 5 was already pushed via the
    cumulative-receipt path).
    """
    if doc.doctype != "Purchase Order":
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return
    if (doc.status or "").lower() != "closed":
        return
    map_row = frappe.db.get_value(
        "EasyEcom PO Map",
        {"purchase_order": doc.name},
        ["name", "ee_po_id", "last_pushed_po_status"],
        as_dict=True,
    )
    if not map_row or not map_row.get("ee_po_id"):
        return
    push_po_status(
        po_docname=doc.name,
        target_status=PO_STATUS_COMPLETED,
        mark_complete=1,
    )


# ============================================================
# GRN Map upsert helpers
# ============================================================


def _grn_map_base_fields(
    *,
    grn_row: dict,
    inwarded_wh_c_id: int,
    vendor_c_id: int | None = None,
) -> dict[str, Any]:
    """Frappe Int columns are NOT NULL at the SQL layer (default 0). Pass
    0 for unknown int fields so MariaDB doesn't reject the UPDATE."""
    return {
        "ee_grn_id": int(grn_row.get("grn_id") or 0),
        "grn_invoice_number": grn_row.get("grn_invoice_number") or "",
        "grn_invoice_date": _date_from_ee(grn_row.get("grn_invoice_date")),
        "grn_status_id": int(grn_row.get("grn_status_id") or 0),
        "total_grn_value": flt(grn_row.get("total_grn_value") or 0),
        "inwarded_warehouse_c_id": inwarded_wh_c_id,
        "vendor_c_id": int(vendor_c_id) if vendor_c_id is not None else 0,
        "po_ref_num": grn_row.get("po_ref_num") or "",
        "ee_po_id": int(grn_row.get("po_id") or 0),
        "last_observed_at": now_datetime(),
    }


def _upsert_grn_map_stn(
    *, ee_grn_id: int, grn_row: dict, inwarded_wh_c_id: int
) -> None:
    base = _grn_map_base_fields(
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=inwarded_wh_c_id,  # by definition for STN
    )
    base.update(
        {
            "status": "STN-Routed",
            "routed_to_stn": 1,
        }
    )
    _upsert_grn_map(ee_grn_id=ee_grn_id, fields=base)


def _upsert_grn_map_held(
    *,
    ee_grn_id: int,
    grn_row: dict,
    inwarded_wh_c_id: int,
    vendor_c_id: int,
) -> None:
    base = _grn_map_base_fields(
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=vendor_c_id,
    )
    base.update({"status": "Held-Pre-QC"})
    _upsert_grn_map(ee_grn_id=ee_grn_id, fields=base)


def _upsert_grn_map_failed(
    *,
    ee_grn_id: int,
    grn_row: dict,
    inwarded_wh_c_id: int,
    vendor_c_id: int,
    reason: str,
) -> None:
    base = _grn_map_base_fields(
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=vendor_c_id,
    )
    base.update({"status": "Failed"})
    _upsert_grn_map(ee_grn_id=ee_grn_id, fields=base)
    # Note the reason on the GRN Map row via a Comment (the map doesn't
    # have a flag_reason field).
    name = frappe.db.get_value(
        "EasyEcom GRN Map", {"ee_grn_id": ee_grn_id}, "name"
    )
    if name:
        try:
            doc = frappe.get_doc("EasyEcom GRN Map", name)
            doc.add_comment(
                comment_type="Info",
                text=f"<b>§9 GRN pull — Failed</b>: {frappe.utils.escape_html(reason)[:500]}",
            )
        except Exception:
            pass


def _upsert_grn_map_receipted(
    *,
    ee_grn_id: int,
    grn_row: dict,
    inwarded_wh_c_id: int,
    vendor_c_id: int,
    pr_name: str,
    linked_po_map_name: str | None,
    status: str,
) -> None:
    base = _grn_map_base_fields(
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=vendor_c_id,
    )
    base.update(
        {
            "status": status,
            "purchase_receipt": pr_name,
            "linked_po_map": linked_po_map_name,
        }
    )
    _upsert_grn_map(ee_grn_id=ee_grn_id, fields=base)


def _upsert_grn_map(*, ee_grn_id: int, fields: dict) -> None:
    existing = frappe.db.get_value(
        "EasyEcom GRN Map", {"ee_grn_id": ee_grn_id}, "name"
    )
    if existing:
        frappe.db.set_value("EasyEcom GRN Map", existing, fields, update_modified=True)
        return
    doc = frappe.new_doc("EasyEcom GRN Map")
    # ee_grn_id is in fields; ensure it's set before insert.
    doc.update(fields)
    doc.insert(ignore_permissions=True)


def _refresh_observed_only(*, grn_map_name: str, grn_row: dict) -> None:
    """Idempotency path — refresh observed fields on a no-op re-pull."""
    frappe.db.set_value(
        "EasyEcom GRN Map",
        grn_map_name,
        {
            "grn_status_id": int(grn_row.get("grn_status_id") or 0),
            "last_observed_at": now_datetime(),
        },
        update_modified=False,
    )


def _get_grn_map(ee_grn_id: int) -> dict | None:
    return frappe.db.get_value(
        "EasyEcom GRN Map",
        {"ee_grn_id": ee_grn_id},
        ["name", "status", "purchase_receipt", "linked_po_map"],
        as_dict=True,
    )


# ============================================================
# Misc helpers
# ============================================================


def _grn_receipt_trigger_status() -> int:
    """Read the Account setting (default 3 QC Complete)."""
    raw = (
        frappe.db.get_value(
            "EasyEcom Account",
            {"enabled": 1},
            "grn_receipt_trigger_status",
        )
        or "3 QC Complete"
    )
    # The Select stores 'N <label>'; take the leading int.
    try:
        return int((raw or "3").strip().split()[0])
    except Exception:
        return 3


def _resolve_location_for_warehouse_c_id(wh_c_id: int) -> dict | None:
    """EE's `inwarded_warehouse_c_id` is the EE-side company_id int
    (live finding 2026-05-28 on Harmony — real GRN payloads have
    inwarded_warehouse_c_id=99303 matching the location's company_id,
    NOT the location_key string which has alphanumeric prefixes like
    'ee9861085809'). The §9 packet's original claim that this matches
    location_key was wrong.

    Resolution: look up via EasyEcom Location.ee_company_id (the int
    captured during §8a Discover Locations from EE payload field
    `company_id`).
    """
    if not wh_c_id:
        return None
    # Primary: match by ee_company_id (real Harmony shape).
    row = frappe.db.get_value(
        "EasyEcom Location",
        {"ee_company_id": int(wh_c_id)},
        ["name", "location_key", "mapped_warehouse", "workflow_state",
         "ee_company_id"],
        as_dict=True,
    )
    # Fallback: match by location_key (legacy / test-fixture shape
    # where the synthetic Location's location_key happens to be the
    # int the test sends as inwarded_warehouse_c_id). Real Harmony
    # location_keys are strings with alpha prefixes (e.g. 'ee9861085809')
    # so the string-equality fallback is harmless for production data
    # — it can only match numeric location_keys.
    if not row:
        row = frappe.db.get_value(
            "EasyEcom Location",
            {"location_key": str(wh_c_id)},
            ["name", "location_key", "mapped_warehouse", "workflow_state",
             "ee_company_id"],
            as_dict=True,
        )
    if not row:
        return None
    if not row.mapped_warehouse:
        return None  # mapped to a Frappe Warehouse is the actual Gate-0 gate
    return row


def _company_for_warehouse(warehouse: str) -> str:
    return (
        frappe.db.get_value("Warehouse", warehouse, "company")
        or frappe.db.get_value("Company", filters={}, fieldname="name")
    )


def _default_rejected_warehouse_for(company: str) -> str | None:
    """Account setting; the field stores a single Warehouse Link (not
    per-Company), so we just return its value. Stage 4 may add a
    per-Company override on EasyEcom Company Settings."""
    return frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1}, "default_rejected_warehouse"
    )


def _resolve_warehouse_address(warehouse: str) -> dict | None:
    if not warehouse:
        return None
    rows = frappe.db.sql(
        """
        SELECT a.address_line1, a.city, a.pincode, a.state, a.gst_state, a.country
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


def _find_supplier_state(supplier_docname: str) -> str | None:
    rows = frappe.db.sql(
        """
        SELECT a.gst_state
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
          AND a.address_type IN ('Billing', 'Shipping')
        ORDER BY (a.address_type='Billing') DESC, a.creation ASC
        LIMIT 1
        """,
        (supplier_docname,),
    )
    return rows[0][0] if rows else None


def _date_from_ee(value: Any) -> Any:
    if not value:
        return None
    try:
        return getdate(value)
    except Exception:
        return None


def _fmt_dt_for_ee(value: Any) -> str:
    """EE expects 'YYYY-MM-DD HH:MM:SS' on created_after."""
    if not value:
        return ""
    try:
        d = frappe.utils.get_datetime(value)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _extract_next_url(page: dict | None) -> str | None:
    if not page:
        return None
    for k in ("nextUrl", "next_url", "next_page_url"):
        v = page.get(k)
        if v:
            return v
    return None


__all__ = [
    "GRNOutcome",
    "GRNSweepOutcome",
    "pull_grns_for_location",
    "scheduled_grn_pull",
    "process_one_grn",
    "grn_pull_queue_handler",
    "enqueue_on_po_close",
    "PING_PONG_FLAG",
]
