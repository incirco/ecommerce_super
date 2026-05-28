"""§9 Sync Record writes for the PO-push flow.

Mirrors _supplier_sync_records.py exactly — same shape, scoped to the
Purchase Order entity. The §9 push is the FIRST flow that populates
the Sync Record Line child table (§7.1.1 amendment + Stage 1's
source_line_number add) per push attempt: one row per PO line, with
line_status OK / Failed / Discrepancy.

Sync Record state machine (§7.3):
  Pending → Running → {Success | Failed | Discrepancy}

Status mapping for §9 Stage 2 push (per attempt):
  - Content push succeeds + no line discrepancies → Success
  - Status-only push succeeds → Success
  - EE 4xx / network failure / precondition fail → Failed
  - Line-level discrepancy (e.g. tax_rate divergence vs Item) — DEFERRED
    to Stage 3 reconciliation; Stage 2 push doesn't surface line
    discrepancies (only Failed/Success).

Per-line outcomes (the new `lines` child):
  - source_line_ref      = PO line item_code
  - source_line_number   = PO line idx
  - target_field         = "sku" (the EE-side field the line resolved to)
  - line_status          = OK | Failed
  - reason               = populated on Failed (missing Item Map, etc.)

Entity-type / entity-doctype:
  - entity_type = "Purchase Order"
  - entity_doctype = "Purchase Order"

Company:
  PO has its own `company` field (unlike masters where we picked one).
  Use that directly; falls back to the picker only if somehow blank
  (shouldn't happen — PO.company is reqd in ERPNext).
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.doctype.easyecom_sync_record import (
    easyecom_sync_record as sync_record_mod,
)
from ecommerce_super.easyecom.flows._item_sync_records import (
    STATUS_DISCREPANCY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    _company_for_item_sync,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.idempotency import internal_job_key


# §31.2.3 entity_type for PO push.
ENTITY_TYPE_PO = "Purchase Order"


def write_po_push_sync_record(
    *,
    entity_name: str,
    company: str | None,
    status: str,
    last_error: str | None = None,
    line_outcomes: list[dict[str, Any]] | None = None,
) -> str | None:
    """Upsert and transition the Sync Record for one §9 PO push attempt.

    line_outcomes (optional): list of dicts shaped:
        {
          "source_line_ref": str,         # item_code
          "source_line_number": int,      # PO line idx
          "target_field": str,            # "sku" / "ean" / "AccountingSku"
          "line_status": "OK" | "Failed" | "Discrepancy",
          "reason": str | None,
        }
    Empty / None → no Lines populated (status-channel-only push or pre-
    line-iteration failure).
    """
    if not entity_name:
        return None
    if not frappe.db.exists("Purchase Order", entity_name):
        return None

    resolved_company = company or _company_for_item_sync()
    correlation_id = new_correlation_id()
    idem_key = internal_job_key(
        job_type="po_push",
        company=resolved_company,
        target_doctype="Purchase Order",
        target_name=entity_name,
    )

    sr = sync_record_mod.upsert(
        company=resolved_company,
        entity_doctype="Purchase Order",
        entity_name=entity_name,
        entity_type=ENTITY_TYPE_PO,
        direction="Push",
        correlation_id=correlation_id,
        idempotency_key=idem_key,
    )

    updates: dict[str, Any] = {
        "status": status,
        "last_attempt_at": frappe.utils.now_datetime(),
        "correlation_id": correlation_id,
        "attempts": (sr.attempts or 0) + 1,
    }
    if last_error is not None:
        updates["last_error"] = (last_error or "")[:1000]
    else:
        updates["last_error"] = None

    sr.db_set(updates, update_modified=True, commit=False)

    # Populate the line-child table. The Sync Record was just upserted;
    # lines from a prior attempt should be replaced (the latest attempt
    # is the authoritative outcome — Sync Record is mutable-in-place per
    # §7.1).
    if line_outcomes is not None:
        sr.reload()
        sr.set("lines", [])
        for lo in line_outcomes:
            sr.append(
                "lines",
                {
                    "source_line_ref": str(lo.get("source_line_ref") or ""),
                    "source_line_number": int(lo.get("source_line_number") or 0),
                    "target_field": str(lo.get("target_field") or ""),
                    "line_status": lo.get("line_status") or "OK",
                    "reason": (lo.get("reason") or "")[:500] or None,
                },
            )
        sr.save(ignore_permissions=True)
    return sr.name


__all__ = [
    "ENTITY_TYPE_PO",
    "STATUS_DISCREPANCY",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "write_po_push_sync_record",
]
