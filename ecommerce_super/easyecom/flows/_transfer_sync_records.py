"""§10 Sync Record writes for the Stock Transfer outbound flow.

Mirrors `_po_sync_records.py` shape, scoped to the Delivery Note entity
(the §10 outbound anchor). One Sync Record per DN-submit-attempt;
upsert keyed on (company, entity_doctype, entity_name, direction).

Sync Record state machine (§7.3):
  Pending → Running → {Success | Failed | Discrepancy}

Status mapping for §10 Stage 2 outbound:
  - DN-submit + EE create-order succeeds → Success
  - EE 4xx / network / precondition fail → Failed
  - Pause-deferred (push not attempted) → Pending (waiting for un-pause)

Entity-type / entity-doctype:
  - entity_type = "Delivery Note"  (per §31.2.3 added in §10)
  - entity_doctype = "Delivery Note"

Company:
  DN has its own `company` field (reqd in ERPNext). Use directly.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.doctype.easyecom_sync_record import (
    easyecom_sync_record as sync_record_mod,
)
from ecommerce_super.easyecom.flows._item_sync_records import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    _company_for_item_sync,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.idempotency import internal_job_key


ENTITY_TYPE_TRANSFER = "Delivery Note"


def write_transfer_push_sync_record(
    *,
    dn_name: str,
    company: str | None,
    status: str,
    last_error: str | None = None,
    line_outcomes: list[dict[str, Any]] | None = None,
) -> str | None:
    """Upsert + transition the §10 Sync Record for one DN-submit attempt.

    line_outcomes: list of per-DN-line dicts (one row per line):
        {
          "source_line_ref":    str,        # item_code
          "source_line_number": int,        # DN line idx
          "target_field":       str,        # "Sku" (the EE-side resolved field)
          "line_status":        "OK" | "Failed",
          "reason":             str | None,
        }
    """
    if not dn_name:
        return None
    if not frappe.db.exists("Delivery Note", dn_name):
        return None

    resolved_company = company or _company_for_item_sync()
    correlation_id = new_correlation_id()
    idem_key = internal_job_key(
        job_type="transfer_push",
        company=resolved_company,
        target_doctype="Delivery Note",
        target_name=dn_name,
    )

    sr = sync_record_mod.upsert(
        company=resolved_company,
        entity_doctype="Delivery Note",
        entity_name=dn_name,
        entity_type=ENTITY_TYPE_TRANSFER,
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
    "ENTITY_TYPE_TRANSFER",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "write_transfer_push_sync_record",
]
