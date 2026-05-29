"""§10 Stage 3 — EE GRN-Complete → IPR + IPI + DN inbound flow.

The §10 invariant (packet line 11, written here per the controller
docstring rule):

  When a financial pre-condition isn't met, the integration creates
  the dependent document in Draft and notifies, never auto-submits.
  SI-not-submitted → IPR-in-Draft. Submitted-DN-exists → late IPR in
  Draft. Manual-reconciliation states are surfaced via ERPNext-native
  UX, not auto-resolved.

Entry points:

  process_inbound_grn(grn_row, ee_grn_id, inwarded_wh_c_id, vendor_c_id,
                       location_row, company, transfer_map_name)
    Called from §9 grn_pull.process_one_grn when GRN.po_ref_num resolves
    to a §10 Delivery Note's Transfer Map. Builds the IPR with Internal-
    Supplier pattern, applies the submit gate (§3), chains IPI + Debit
    Note on auto-submit (§5).

  handle_ee_originated_grn(grn_row, ee_grn_id, inwarded_wh_c_id,
                            vendor_c_id)
    Called from §9 grn_pull self-GRN routing branch. The GRN has no
    originating DN — IPR is standalone Draft + Integration Discrepancy.
    FDE picks Internal Supplier and submits manually (same hands-off
    pattern as §9's drift Create-PR-from-GRN).

  on_sales_invoice_submit(doc, method=None)
    DN-event hook fired when an ERP user submits an SI. Walks the
    linked Transfer Map's drafted IPRs and re-evaluates the submit
    gate; auto-submits IPRs whose source-side SI just crystallised.

  on_purchase_invoice_submit(doc, method=None)
    DN-event hook for the Debit-Note submit path. When a draft DN
    becomes submitted, transitions Transfer Map status to
    DN-Submitted-Locked so subsequent GRNs hit §7's late-GRN block.

Reuses §9 grn_pull helpers (_build_pr_header, _append_pr_line) where
the qty/buckets/back-refs model is identical — do NOT fork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import frappe
from frappe.utils import flt, now_datetime


PING_PONG_FLAG = "easyecom_transfer_inbound_in_flight"

InboundOp = Literal[
    "ipr_submitted",      # full happy path — auto-submit fired
    "ipr_drafted",        # SI-pending or late-GRN block — Draft
    "ipr_failed",         # supplier/item resolution failure
    "noop_already_done",  # idempotent re-pull
    "ee_originated_draft",  # standalone Draft IPR (no Transfer Map)
]


@dataclass
class InboundOutcome:
    ee_grn_id: int
    operation: InboundOp
    transfer_map: str | None = None
    purchase_receipt: str | None = None
    purchase_invoice: str | None = None
    debit_note: str | None = None
    flag_reasons: list[str] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)
    sync_record_name: str | None = None
    status: str | None = None  # Transfer Map status after this run


# ============================================================
# Public entry — §10-routed GRN
# ============================================================


def process_inbound_grn(
    *,
    grn_row: dict,
    ee_grn_id: int,
    inwarded_wh_c_id: int,
    vendor_c_id: int,
    location_row: dict,
    company: str,
    transfer_map_name: str,
) -> Any:
    """The §10 inbound entrypoint. Builds the IPR, applies the submit
    gate, chains IPI + Debit Note on auto-submit. Returns a §9-shaped
    GRNOutcome so the caller (process_one_grn) can return it directly.
    """
    # Late import to avoid the cycle (grn_pull imports us, we import
    # GRNOutcome from grn_pull).
    from ecommerce_super.easyecom.flows.grn_pull import (
        GRNOutcome,
        _append_pr_line,
        _build_pr_header,
        _RejectedWarehouseMissingError,
        _resolve_for_receipt,
    )
    from ecommerce_super.easyecom.flows._grn_sync_records import (
        STATUS_FAILED,
        STATUS_SUCCESS,
        write_grn_drift_sync_record,
    )

    tm = frappe.get_doc("EasyEcom Transfer Map", transfer_map_name)

    # Idempotency: if this Transfer Map already has an IPR linked to
    # this ee_grn_id, no-op (re-pull).
    existing_pr = _find_existing_ipr_for_grn(
        transfer_map=transfer_map_name, ee_grn_id=ee_grn_id
    )
    if existing_pr:
        _upsert_grn_map_for_transfer(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            pr_name=existing_pr,
            transfer_map_name=transfer_map_name,
            status="Receipted",
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="noop",
            grn_map_status="Receipted",
            purchase_receipt=existing_pr,
        )

    # Resolve Internal Supplier. Refusal → Failed; FDE fixes pair fabric
    # and retries.
    target_company = company
    source_company = _resolve_source_company_from_transfer_map(tm)
    internal_supplier = _find_internal_supplier(
        source_company=source_company, target_company=target_company
    )
    if not internal_supplier:
        reason = (
            f"§10 Internal Supplier missing for source Company "
            f"{source_company!r} → target Company {target_company!r}. "
            "Run ensure_internal_party_pairs_for_account on the Account "
            "to create the pair fabric, then re-pull this GRN."
        )
        _upsert_grn_map_for_transfer(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            pr_name=None,
            transfer_map_name=transfer_map_name,
            status="Failed",
            flag_reason=reason,
        )
        sr = write_grn_drift_sync_record(
            ee_grn_id=ee_grn_id,
            company=company,
            status=STATUS_FAILED,
            last_error=reason,
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=[reason],
            sync_record_name=sr,
        )

    # §10 line resolution. Cannot reuse §9's _resolve_for_receipt
    # directly — its supplier_missing path short-circuits before line
    # resolution (the §9 model expects an EE-side Supplier Map; §10's
    # supplier is the Internal Supplier which lives on the ERPNext
    # side only, never in Supplier Map). Do the line resolution inline
    # to keep transfer_inbound additive over §9 without touching §9's
    # _resolve_for_receipt.
    resolution: dict[str, Any] = {
        "po_name": None,
        "po_map_name": None,
        "supplier": internal_supplier,
        "supplier_country": (
            frappe.db.get_value("Supplier", internal_supplier, "country")
            or "India"
        ),
        "supplier_state": None,
        "lines": [],
        "line_failures": [],
    }
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
            resolution["line_failures"].append(
                f"Item Map missing for sku={sku!r} ean={ean!r} on "
                f"grn_detail_id={line.get('grn_detail_id')}"
            )
            continue
        resolution["lines"].append((line, item_map))

    if resolution.get("line_failures"):
        reason = " || ".join(resolution["line_failures"][:3])
        _upsert_grn_map_for_transfer(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            pr_name=None,
            transfer_map_name=transfer_map_name,
            status="Failed",
            flag_reason=reason,
        )
        sr = write_grn_drift_sync_record(
            ee_grn_id=ee_grn_id,
            company=company,
            status=STATUS_FAILED,
            last_error=reason,
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=resolution["line_failures"],
            sync_record_name=sr,
        )

    # §10 IPR overrides: replace resolution['supplier'] + 'set_warehouse'
    # so §9's _build_pr_header builds against the Internal Supplier
    # pulling FROM GIT.
    git_warehouse = _resolve_git_warehouse(target_company)
    if not git_warehouse:
        reason = (
            f"Target Company {target_company!r} has no "
            "default_in_transit_warehouse. §10 IPR pulls FROM GIT — "
            "configure on the EasyEcom Account."
        )
        _upsert_grn_map_for_transfer(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            pr_name=None,
            transfer_map_name=transfer_map_name,
            status="Failed",
            flag_reason=reason,
        )
        sr = write_grn_drift_sync_record(
            ee_grn_id=ee_grn_id,
            company=company,
            status=STATUS_FAILED,
            last_error=reason,
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=[reason],
            sync_record_name=sr,
        )

    # §10 IPR specifics:
    #   - set_warehouse / per-line warehouse = the EE-mapped TARGET
    #     warehouse (where stock lands on IPR submit).
    #   - per-line from_warehouse = GIT (where stock came from after
    #     DN submit) — overridden post-_append_pr_line below.
    resolution["set_warehouse"] = location_row["mapped_warehouse"]
    # Warehouse state for tax derivation (used by §9's tax helpers).
    from ecommerce_super.easyecom.flows.grn_pull import (
        _resolve_warehouse_address as _grn_resolve_addr,
    )
    wh_addr = _grn_resolve_addr(location_row.get("mapped_warehouse")) or {}
    resolution["warehouse_state"] = wh_addr.get("gst_state")
    # Rejected warehouse per the target Company's account setting.
    resolution["rejected_warehouse"] = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        "default_rejected_warehouse",
    )

    # Build PR header + lines via §9 helpers.
    pr_doc = _build_pr_header(
        resolution=resolution,
        grn_row=grn_row,
        location_row=location_row,
        company=target_company,
    )
    # Internal-supplier flag for the §10 IPR. ERPNext's
    # validate_inter_company_reference requires the source-side DN
    # reference at the header level for internal transfers.
    pr_doc.is_internal_supplier = 1
    pr_doc.inter_company_reference = tm.delivery_note
    # §10 back-ref so subsequent doc-event hooks can find the Transfer Map.
    if frappe.get_meta("Purchase Receipt").get_field(
        "ecs_section10_transfer_map"
    ):
        pr_doc.ecs_section10_transfer_map = transfer_map_name

    try:
        for line_payload, item_map_row in resolution["lines"]:
            _append_pr_line(
                pr_doc=pr_doc,
                line_payload=line_payload,
                item_map_row=item_map_row,
                resolution=resolution,
            )
        # ERPNext's is_internal_supplier validate requires each PR line
        # to reference the source-side DN line via `delivery_note` +
        # `dn_detail`. Resolve the matching DN line per item_code.
        dn_lines_by_item: dict[str, str] = {}
        for ln in frappe.db.sql(
            """SELECT name, item_code FROM `tabDelivery Note Item`
               WHERE parent = %s""",
            (tm.delivery_note,),
            as_dict=True,
        ):
            # First match wins for multi-line same-SKU DNs — Stage 4
            # can refine this with explicit grn_detail_id → dn_detail
            # pairing if real Harmony shows the multi-line case.
            dn_lines_by_item.setdefault(
                ln["item_code"], ln["name"]
            )
        for pr_line in pr_doc.items:
            dn_detail = dn_lines_by_item.get(pr_line.item_code)
            if dn_detail:
                pr_line.delivery_note = tm.delivery_note
                pr_line.delivery_note_item = dn_detail
            # ERPNext internal-transfer line invariant: from_warehouse
            # is the GIT (the line WAS in GIT after DN-submit; IPR
            # pulls FROM it to the destination warehouse).
            pr_line.from_warehouse = git_warehouse
            # §10 IPR: stock was already valued at the source-side DN
            # submit; this IPR is a transfer, not a fresh purchase, so
            # zero-valuation is acceptable at the line level. ERPNext
            # would otherwise refuse submit on a zero-rate line.
            pr_line.allow_zero_valuation_rate = 1
    except _RejectedWarehouseMissingError as exc:
        _upsert_grn_map_for_transfer(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            pr_name=None,
            transfer_map_name=transfer_map_name,
            status="Failed",
            flag_reason=str(exc),
        )
        sr = write_grn_drift_sync_record(
            ee_grn_id=ee_grn_id,
            company=company,
            status=STATUS_FAILED,
            last_error=str(exc),
        )
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="failed",
            grn_map_status="Failed",
            flag_reasons=[str(exc)],
            sync_record_name=sr,
        )

    # Insert as Draft (docstatus=0).
    pr_doc.insert(ignore_permissions=True)

    # Append to Transfer Map.internal_purchase_receipts.
    tm.append(
        "internal_purchase_receipts",
        {"internal_purchase_receipt": pr_doc.name},
    )
    tm.save(ignore_permissions=True)

    # Apply submit gate per §3 + §7.
    submit_decision = _decide_ipr_submit(
        transfer_map=tm,
        ee_grn_id=ee_grn_id,
    )

    if submit_decision["action"] == "submit":
        try:
            pr_doc.submit()
        except Exception as exc:
            sr = write_grn_drift_sync_record(
                ee_grn_id=ee_grn_id,
                company=company,
                status=STATUS_FAILED,
                last_error=f"IPR submit: {type(exc).__name__}: {exc}",
            )
            _upsert_grn_map_for_transfer(
                ee_grn_id=ee_grn_id,
                grn_row=grn_row,
                inwarded_wh_c_id=inwarded_wh_c_id,
                vendor_c_id=vendor_c_id,
                pr_name=pr_doc.name,
                transfer_map_name=transfer_map_name,
                status="Failed",
                flag_reason=f"IPR submit: {exc}"[:1000],
            )
            return GRNOutcome(
                ee_grn_id=ee_grn_id,
                operation="failed",
                grn_map_status="Failed",
                purchase_receipt=pr_doc.name,
                flag_reasons=[f"IPR submit: {type(exc).__name__}: {exc}"],
                sync_record_name=sr,
            )
        # Chain IPI + DN for different-GSTIN.
        chain_result = _chain_ipi_and_debit_note(tm)
        new_status = _compute_transfer_status_after_ipr_submit(tm)
        _upsert_grn_map_for_transfer(
            ee_grn_id=ee_grn_id,
            grn_row=grn_row,
            inwarded_wh_c_id=inwarded_wh_c_id,
            vendor_c_id=vendor_c_id,
            pr_name=pr_doc.name,
            transfer_map_name=transfer_map_name,
            status="Receipted",
        )
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            tm.name,
            {"status": new_status},
            update_modified=True,
        )
        sr = write_grn_drift_sync_record(
            ee_grn_id=ee_grn_id,
            company=company,
            status=STATUS_SUCCESS,
            last_error=None,
        )
        discs = chain_result.get("discrepancies") or []
        return GRNOutcome(
            ee_grn_id=ee_grn_id,
            operation="receipted",
            grn_map_status="Receipted",
            purchase_receipt=pr_doc.name,
            sync_record_name=sr,
            discrepancies=discs,
        )

    # Action = "draft" — submit gate not cleared.
    reason = submit_decision["reason"]
    _add_ipr_block_comment(pr_doc.name, reason)
    discrepancies: list[str] = []
    if submit_decision.get("kind") == "late_grn_after_submitted_dn":
        from ecommerce_super.easyecom.flows.grn_pull import (
            _raise_discrepancy,
        )

        disc = _raise_discrepancy(
            kind="Late GRN after submitted DN",
            reference_doctype="Purchase Receipt",
            reference_name=pr_doc.name,
            company=company,
            reason=reason,
        )
        if disc:
            discrepancies.append(disc)
    _upsert_grn_map_for_transfer(
        ee_grn_id=ee_grn_id,
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=vendor_c_id,
        pr_name=pr_doc.name,
        transfer_map_name=transfer_map_name,
        status="Pending",
        flag_reason=reason,
    )
    sr_status = (
        STATUS_FAILED
        if submit_decision.get("kind") == "late_grn_after_submitted_dn"
        else "Pending"
    )
    sr = write_grn_drift_sync_record(
        ee_grn_id=ee_grn_id,
        company=company,
        status=sr_status,
        last_error=reason,
    )
    return GRNOutcome(
        ee_grn_id=ee_grn_id,
        operation="held",
        grn_map_status="Pending",
        purchase_receipt=pr_doc.name,
        sync_record_name=sr,
        discrepancies=discrepancies,
        flag_reasons=[reason],
    )


# ============================================================
# Helpers — Internal-Supplier resolution + Transfer Map
# ============================================================


def _resolve_source_company_from_transfer_map(tm: Any) -> str:
    return frappe.db.get_value("Warehouse", tm.source_warehouse, "company")


def _find_internal_supplier(
    *, source_company: str, target_company: str
) -> str | None:
    """Symmetric to Stage 2's Internal Customer lookup. Returns the
    Internal Supplier representing source_company AND permitted to
    transact with target_company."""
    if not source_company or not target_company:
        return None
    rows = frappe.db.sql(
        """
        SELECT s.name
        FROM `tabSupplier` s
        JOIN `tabAllowed To Transact With` atw
          ON atw.parent = s.name
        WHERE s.is_internal_supplier = 1
          AND s.represents_company = %s
          AND atw.company = %s
        LIMIT 1
        """,
        (source_company, target_company),
        as_dict=True,
    )
    return rows[0]["name"] if rows else None


def _resolve_git_warehouse(company: str) -> str | None:
    """Account.default_in_transit_warehouse — §3.3.6 + §10 packet."""
    return frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        "default_in_transit_warehouse",
    )


def _find_existing_ipr_for_grn(
    *, transfer_map: str, ee_grn_id: int
) -> str | None:
    """Idempotency hinge: a PR linked to this transfer with matching
    ee_easyecom_grn_id (the §9 back-ref) is the re-pull case."""
    rows = frappe.db.sql(
        """
        SELECT pr.name
        FROM `tabPurchase Receipt` pr
        WHERE pr.ecs_section10_transfer_map = %s
          AND pr.ecs_easyecom_grn_id = %s
          AND pr.docstatus IN (0, 1)
        LIMIT 1
        """,
        (transfer_map, str(ee_grn_id)),
        as_dict=True,
    )
    return rows[0]["name"] if rows else None


def _upsert_grn_map_for_transfer(
    *,
    ee_grn_id: int,
    grn_row: dict,
    inwarded_wh_c_id: int,
    vendor_c_id: int,
    pr_name: str | None,
    transfer_map_name: str,
    status: str,
    flag_reason: str | None = None,
) -> None:
    """Mirror §9's _upsert_grn_map_* family but writes the §10
    linked_transfer_map field too."""
    from ecommerce_super.easyecom.flows.grn_pull import (
        _grn_map_base_fields,
        _upsert_grn_map,
    )

    base = _grn_map_base_fields(
        grn_row=grn_row,
        inwarded_wh_c_id=inwarded_wh_c_id,
        vendor_c_id=vendor_c_id,
    )
    base.update(
        {
            "status": status,
            "purchase_receipt": pr_name,
            "linked_transfer_map": transfer_map_name,
        }
    )
    _upsert_grn_map(ee_grn_id=ee_grn_id, fields=base)
    if flag_reason:
        name = frappe.db.get_value(
            "EasyEcom GRN Map", {"ee_grn_id": ee_grn_id}, "name"
        )
        if name:
            try:
                doc = frappe.get_doc("EasyEcom GRN Map", name)
                doc.add_comment(
                    comment_type="Info",
                    text=(
                        f"<b>§10 inbound — {status}</b>: "
                        + frappe.utils.escape_html(flag_reason)[:500]
                    ),
                )
            except Exception:
                pass


# ============================================================
# Submit gate (§3 + §7)
# ============================================================


def _decide_ipr_submit(
    *, transfer_map: Any, ee_grn_id: int
) -> dict[str, Any]:
    """Returns {action: 'submit'|'draft', reason: str|None, kind: str|None}.

    Kinds (for the 'draft' decision):
      - 'same_gstin'           — never returned (same-GSTIN auto-submits)
      - 'si_pending'           — different-GSTIN + SI Draft/Missing
      - 'late_grn_after_submitted_dn'  — §7 block
    """
    # §7 first: a submitted Debit Note locks the transfer.
    if transfer_map.draft_debit_note:
        dn_docstatus = frappe.db.get_value(
            "Purchase Invoice",
            transfer_map.draft_debit_note,
            "docstatus",
        )
        if int(dn_docstatus or 0) == 1:
            return {
                "action": "draft",
                "kind": "late_grn_after_submitted_dn",
                "reason": (
                    f"§7 Submitted-DN-late-GRN block: Transfer Map "
                    f"{transfer_map.name} has a submitted Debit Note "
                    f"({transfer_map.draft_debit_note}). Late GRN "
                    f"{ee_grn_id} cannot auto-submit IPR — ERP user "
                    "must reverse the DN via a fresh Purchase Invoice "
                    "or Journal Entry, then submit the IPR manually."
                ),
            }

    # §3: same GSTIN auto-submits.
    if not int(transfer_map.gstin_different or 0):
        return {"action": "submit", "reason": None, "kind": "same_gstin"}

    # Different GSTIN: SI must be Submitted.
    si_name = transfer_map.sales_invoice
    si_docstatus = (
        frappe.db.get_value("Sales Invoice", si_name, "docstatus")
        if si_name
        else None
    )
    if si_docstatus is not None and int(si_docstatus or 0) == 1:
        return {
            "action": "submit",
            "reason": None,
            "kind": "different_gstin_si_submitted",
        }

    return {
        "action": "draft",
        "kind": "si_pending",
        "reason": (
            f"§3 IPR submit gate: different-GSTIN transfer requires "
            f"source-side SI to be Submitted before IPR auto-submits. "
            f"SI {si_name!r} is {'Draft' if si_docstatus == 0 else 'missing'}. "
            "ERP user must submit the SI; on_submit hook then auto-"
            "submits this IPR."
        ),
    }


def _add_ipr_block_comment(pr_name: str, reason: str) -> None:
    """Surface the block to the ERP user via Comment + ToDo."""
    try:
        doc = frappe.get_doc("Purchase Receipt", pr_name)
        doc.add_comment(
            comment_type="Info",
            text=f"<b>§10 IPR in Draft</b>: {frappe.utils.escape_html(reason)[:500]}",
        )
    except Exception:
        pass
    # Native ToDo for the assigned user (Customer / Company owner).
    # Best-effort: skip if the assignment can't be resolved.
    try:
        owner = frappe.db.get_value("Purchase Receipt", pr_name, "owner")
        if owner:
            frappe.get_doc(
                {
                    "doctype": "ToDo",
                    "owner": owner,
                    "allocated_to": owner,
                    "reference_type": "Purchase Receipt",
                    "reference_name": pr_name,
                    "description": (
                        f"§10 IPR pending: {reason[:200]}"
                    ),
                    "priority": "Medium",
                    "status": "Open",
                }
            ).insert(ignore_permissions=True)
    except Exception:
        pass


# ============================================================
# IPI + Debit Note chain (different-GSTIN only)
# ============================================================


def _chain_ipi_and_debit_note(transfer_map: Any) -> dict[str, Any]:
    """After IPR auto-submits + different-GSTIN: ensure IPI exists (or
    create Draft) and ensure Debit Note tracks the cumulative gap.

    Returns {"ipi": str|None, "debit_note": str|None, "discrepancies": []}.
    """
    if not int(transfer_map.gstin_different or 0):
        return {"ipi": None, "debit_note": None, "discrepancies": []}
    if not transfer_map.sales_invoice:
        return {"ipi": None, "debit_note": None, "discrepancies": []}
    si_docstatus = frappe.db.get_value(
        "Sales Invoice", transfer_map.sales_invoice, "docstatus"
    )
    if int(si_docstatus or 0) != 1:
        # SI not submitted yet — defer IPI to the SI on_submit hook.
        return {"ipi": None, "debit_note": None, "discrepancies": []}

    # IPI — create on first call; update would-be sizing on subsequent
    # multi-GRN passes is unnecessary (IPI is SI-sized, fixed).
    ipi_name = transfer_map.internal_purchase_invoice
    if not ipi_name:
        ipi_name = _draft_internal_purchase_invoice(transfer_map)
        if ipi_name:
            frappe.db.set_value(
                "EasyEcom Transfer Map",
                transfer_map.name,
                "internal_purchase_invoice",
                ipi_name,
                update_modified=False,
            )

    # Debit Note — recompute gap per item, refresh draft (or create /
    # cancel as needed).
    dn_name = _reconcile_draft_debit_note(transfer_map)
    if dn_name != transfer_map.draft_debit_note:
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            transfer_map.name,
            "draft_debit_note",
            dn_name,
            update_modified=False,
        )

    return {"ipi": ipi_name, "debit_note": dn_name, "discrepancies": []}


def _draft_internal_purchase_invoice(transfer_map: Any) -> str | None:
    """Build IPI sized to SI dispatched qty. Draft. Returns PI name."""
    si_name = transfer_map.sales_invoice
    if not si_name:
        return None
    si = frappe.get_doc("Sales Invoice", si_name)
    target_company = frappe.db.get_value(
        "Warehouse", transfer_map.target_warehouse, "company"
    )
    supplier = _find_internal_supplier(
        source_company=_resolve_source_company_from_transfer_map(transfer_map),
        target_company=target_company,
    )
    if not supplier:
        return None

    pi = frappe.new_doc("Purchase Invoice")
    pi.update(
        {
            "supplier": supplier,
            "company": target_company,
            "posting_date": frappe.utils.today(),
            "due_date": frappe.utils.today(),
            "is_internal_supplier": 1,
            "update_stock": 0,
            "currency": si.currency or "INR",
            "conversion_rate": si.conversion_rate or 1,
            "buying_price_list": frappe.db.get_value(
                "Price List", {"buying": 1}, "name"
            )
            or "Standard Buying",
            "price_list_currency": "INR",
            "plc_conversion_rate": 1,
        }
    )
    if frappe.get_meta("Purchase Invoice").get_field(
        "ecs_section10_transfer_map"
    ):
        pi.ecs_section10_transfer_map = transfer_map.name

    for si_line in si.items or []:
        pi.append(
            "items",
            {
                "item_code": si_line.item_code,
                "qty": si_line.qty,
                "rate": si_line.rate,
                "warehouse": transfer_map.target_warehouse,
                "item_tax_template": si_line.item_tax_template,
            },
        )
    pi.insert(ignore_permissions=True)
    return pi.name


def _reconcile_draft_debit_note(transfer_map: Any) -> str | None:
    """Compute per-Item gap = dispatched − cumulative_received. Refresh
    the draft Debit Note's line qtys to match. If gap collapses to 0
    across all Items, CANCEL the draft. If no draft yet and gap > 0,
    create one. Returns DN name or None."""
    si_name = transfer_map.sales_invoice
    if not si_name:
        return None
    si = frappe.get_doc("Sales Invoice", si_name)
    cumulative = _cumulative_received_per_item(transfer_map)
    gap_per_item: dict[str, dict[str, Any]] = {}
    for si_line in si.items or []:
        dispatched = flt(si_line.qty)
        received = cumulative.get(si_line.item_code, 0)
        gap = dispatched - received
        if gap > 0:
            gap_per_item[si_line.item_code] = {
                "qty": gap,
                "rate": flt(si_line.rate),
                "item_tax_template": si_line.item_tax_template,
            }

    existing_dn = transfer_map.draft_debit_note
    if not gap_per_item:
        # Gap closed. Cancel existing draft DN.
        if existing_dn:
            try:
                doc = frappe.get_doc("Purchase Invoice", existing_dn)
                if int(doc.docstatus or 0) == 0:
                    doc.add_comment(
                        comment_type="Info",
                        text=(
                            "<b>§10 Auto-cancelled</b>: cumulative receipt "
                            "closed the gap — Debit Note no longer needed."
                        ),
                    )
                    # Delete the draft so the cumulative-close case
                    # doesn't leave a 0-qty draft floating.
                    frappe.delete_doc(
                        "Purchase Invoice",
                        existing_dn,
                        force=True,
                        ignore_permissions=True,
                    )
            except Exception:
                pass
        return None

    if existing_dn:
        # Update existing draft DN line qtys.
        try:
            doc = frappe.get_doc("Purchase Invoice", existing_dn)
            if int(doc.docstatus or 0) != 0:
                # Submitted — handled by §7 block separately, not here.
                return existing_dn
            doc.set("items", [])
            for code, spec in gap_per_item.items():
                doc.append(
                    "items",
                    {
                        "item_code": code,
                        "qty": spec["qty"],
                        "rate": spec["rate"],
                        "warehouse": transfer_map.target_warehouse,
                        "item_tax_template": spec["item_tax_template"],
                    },
                )
            doc.save(ignore_permissions=True)
            doc.add_comment(
                comment_type="Info",
                text=(
                    f"<b>§10 Draft DN revised</b>: gap recomputed across "
                    f"{len(gap_per_item)} item(s)."
                ),
            )
            return existing_dn
        except Exception:
            pass

    # Create a fresh Draft DN against the IPI.
    ipi_name = transfer_map.internal_purchase_invoice
    if not ipi_name:
        return None
    target_company = frappe.db.get_value(
        "Warehouse", transfer_map.target_warehouse, "company"
    )
    supplier = _find_internal_supplier(
        source_company=_resolve_source_company_from_transfer_map(transfer_map),
        target_company=target_company,
    )
    if not supplier:
        return None
    dn = frappe.new_doc("Purchase Invoice")
    dn.update(
        {
            "supplier": supplier,
            "company": target_company,
            "posting_date": frappe.utils.today(),
            "due_date": frappe.utils.today(),
            "is_return": 1,
            "return_against": ipi_name,
            "is_internal_supplier": 1,
            "update_stock": 0,
            "currency": "INR",
            "conversion_rate": 1,
            "buying_price_list": frappe.db.get_value(
                "Price List", {"buying": 1}, "name"
            )
            or "Standard Buying",
            "price_list_currency": "INR",
            "plc_conversion_rate": 1,
        }
    )
    if frappe.get_meta("Purchase Invoice").get_field(
        "ecs_section10_transfer_map"
    ):
        dn.ecs_section10_transfer_map = transfer_map.name
    for code, spec in gap_per_item.items():
        dn.append(
            "items",
            {
                "item_code": code,
                "qty": -spec["qty"],  # Return → negative qty.
                "rate": spec["rate"],
                "warehouse": transfer_map.target_warehouse,
                "item_tax_template": spec["item_tax_template"],
            },
        )
    dn.insert(ignore_permissions=True)
    return dn.name


def _cumulative_received_per_item(transfer_map: Any) -> dict[str, float]:
    """Σ received_qty per item_code across all submitted IPRs linked
    to this Transfer Map."""
    out: dict[str, float] = {}
    for row in transfer_map.internal_purchase_receipts or []:
        pr_name = row.internal_purchase_receipt
        if not pr_name:
            continue
        docstatus = frappe.db.get_value(
            "Purchase Receipt", pr_name, "docstatus"
        )
        if int(docstatus or 0) != 1:
            continue
        for line in frappe.db.sql(
            """
            SELECT item_code, received_qty
            FROM `tabPurchase Receipt Item`
            WHERE parent = %s
            """,
            (pr_name,),
            as_dict=True,
        ):
            out[line["item_code"]] = (
                out.get(line["item_code"], 0) + flt(line["received_qty"])
            )
    return out


def _compute_transfer_status_after_ipr_submit(transfer_map: Any) -> str:
    """Walk SI items + cumulative receipts to decide Partial vs Fully."""
    if not transfer_map.sales_invoice:
        # Same-GSTIN — use DN dispatched qty.
        dn_items = frappe.db.sql(
            """
            SELECT item_code, qty FROM `tabDelivery Note Item`
            WHERE parent = %s
            """,
            (transfer_map.delivery_note,),
            as_dict=True,
        )
        dispatched = {r["item_code"]: flt(r["qty"]) for r in dn_items}
    else:
        si_items = frappe.db.sql(
            """
            SELECT item_code, qty FROM `tabSales Invoice Item`
            WHERE parent = %s
            """,
            (transfer_map.sales_invoice,),
            as_dict=True,
        )
        dispatched = {r["item_code"]: flt(r["qty"]) for r in si_items}
    cumulative = _cumulative_received_per_item(transfer_map)
    fully = True
    for code, qty in dispatched.items():
        if cumulative.get(code, 0) < qty:
            fully = False
            break
    return "Fully-Received" if fully else "Partial-Received"


# ============================================================
# EE-originated standalone path (§8)
# ============================================================


def handle_ee_originated_grn(
    *,
    grn_row: dict,
    ee_grn_id: int,
    inwarded_wh_c_id: int,
    vendor_c_id: int,
) -> InboundOutcome:
    """Standalone IPR for EE-internal inwards (self-GRN routed). No
    Transfer Map, no SI. PR is Draft + Discrepancy; FDE picks Internal
    Supplier and submits manually (option (ii) per the §10 packet,
    confirmed at Stage 3 build). Reuses §9's drift Create-PR-from-GRN
    pattern for the FDE-resolution surface."""
    from ecommerce_super.easyecom.flows.grn_pull import (
        _raise_discrepancy,
    )

    # Don't insert a PR row at all on this path — Frappe refuses to
    # save a Purchase Receipt without a supplier, and we deliberately
    # leave supplier blank so the FDE picks one. Instead, surface the
    # event via an Integration Discrepancy keyed on the GRN Map row.
    # The FDE workflow:
    #   1. Open the Discrepancy → click through to the GRN Map row.
    #   2. Use the existing §9 "Create PR from this GRN" action on the
    #      drift-state GRN Map (the action handles standalone PRs).
    #   3. Supply an Internal Supplier on the PR before submit.
    company = _company_for_warehouse_safe(inwarded_wh_c_id)
    reason = (
        f"§10 EE-originated GRN (self-GRN: vendor_c_id == "
        f"inwarded_warehouse_c_id == {inwarded_wh_c_id}). No "
        "ERPNext-side originating Delivery Note → no Internal Supplier "
        "can be auto-resolved. FDE: invoke 'Create PR from this GRN' "
        "on the GRN Map row, pick an appropriate Internal Supplier "
        "(or a regular Supplier if this is a non-§10 inwards), then "
        "submit."
    )
    disc = _raise_discrepancy(
        kind="EE-originated transfer (self-GRN)",
        reference_doctype="EasyEcom GRN Map",
        reference_name=f"ECS-GRN-{ee_grn_id}",
        company=company or "",
        reason=reason,
    )
    return InboundOutcome(
        ee_grn_id=ee_grn_id,
        operation="ee_originated_draft",
        discrepancies=[disc] if disc else [],
        flag_reasons=[reason],
    )


def _company_for_warehouse_safe(inwarded_wh_c_id: int) -> str | None:
    """Best-effort resolve from inwarded_warehouse_c_id → Location →
    Warehouse → Company. Returns None if any step misses."""
    loc = frappe.db.get_value(
        "EasyEcom Location",
        {
            "location_key": str(inwarded_wh_c_id),
            "workflow_state": "Live",
            "enabled": 1,
        },
        "mapped_warehouse",
    )
    if not loc:
        return None
    return frappe.db.get_value("Warehouse", loc, "company")


# ============================================================
# doc_event hooks
# ============================================================


def on_sales_invoice_submit(doc: Any, method: str | None = None) -> None:
    """SI.on_submit hook — auto-retry drafted IPRs whose source-side SI
    just crystallised. No-op for SIs not linked to a §10 Transfer Map."""
    if doc.doctype != "Sales Invoice":
        return
    if getattr(frappe.flags, PING_PONG_FLAG, False):
        return
    tm_name = getattr(doc, "ecs_section10_transfer_map", None)
    if not tm_name:
        return
    if not frappe.db.exists("EasyEcom Transfer Map", tm_name):
        return
    tm = frappe.get_doc("EasyEcom Transfer Map", tm_name)
    drafted_iprs: list[str] = []
    for row in tm.internal_purchase_receipts or []:
        pr_name = row.internal_purchase_receipt
        if not pr_name:
            continue
        if int(
            frappe.db.get_value("Purchase Receipt", pr_name, "docstatus")
            or 0
        ) == 0:
            drafted_iprs.append(pr_name)
    if not drafted_iprs:
        return

    frappe.flags[PING_PONG_FLAG] = True
    try:
        for pr_name in drafted_iprs:
            pr_doc = frappe.get_doc("Purchase Receipt", pr_name)
            # Re-evaluate gate (now SI is Submitted on the latest read).
            tm.reload()
            decision = _decide_ipr_submit(
                transfer_map=tm,
                ee_grn_id=int(pr_doc.ecs_easyecom_grn_id or 0),
            )
            if decision["action"] != "submit":
                continue
            try:
                pr_doc.submit()
                pr_doc.add_comment(
                    comment_type="Info",
                    text=(
                        f"<b>§10 IPR auto-submitted</b> after SI "
                        f"{doc.name} submission cleared the gate."
                    ),
                )
            except Exception as exc:
                pr_doc.add_comment(
                    comment_type="Info",
                    text=(
                        f"<b>§10 IPR auto-submit failed</b>: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
                continue
        # Chain IPI + DN after all IPRs settled.
        tm.reload()
        _chain_ipi_and_debit_note(tm)
        new_status = _compute_transfer_status_after_ipr_submit(tm)
        frappe.db.set_value(
            "EasyEcom Transfer Map",
            tm.name,
            "status",
            new_status,
            update_modified=True,
        )
    finally:
        frappe.flags[PING_PONG_FLAG] = False


def on_purchase_invoice_submit(
    doc: Any, method: str | None = None
) -> None:
    """PI.on_submit hook — when a draft Debit Note becomes submitted,
    transition Transfer Map status to DN-Submitted-Locked. Subsequent
    GRNs then hit the §7 late-GRN block in _decide_ipr_submit."""
    if doc.doctype != "Purchase Invoice":
        return
    if not int(getattr(doc, "is_return", 0) or 0):
        return
    tm_name = frappe.db.get_value(
        "EasyEcom Transfer Map", {"draft_debit_note": doc.name}, "name"
    )
    if not tm_name:
        return
    frappe.db.set_value(
        "EasyEcom Transfer Map",
        tm_name,
        "status",
        "DN-Submitted-Locked",
        update_modified=True,
    )


__all__ = [
    "InboundOutcome",
    "PING_PONG_FLAG",
    "process_inbound_grn",
    "handle_ee_originated_grn",
    "on_sales_invoice_submit",
    "on_purchase_invoice_submit",
]
