"""§9 Stage 3 Sync Record writes for the GRN-pull flow.

ONE GRN → ONE PR → ONE Sync Record (per packet's Sync Record model).
Lines child populated per grn_items[] line with OK / Failed /
Discrepancy line_status + optional Integration Discrepancy link
(the §23 stub).

Entity-type / entity-doctype:
  - entity_type = "GRN"
  - entity_doctype = "Purchase Receipt" (the ERPNext object the
    Sync Record's Dynamic Link target resolves to)
  - entity_name = the PR docname, OR None when the GRN didn't result
    in a PR (Held-Pre-QC, STN-Routed, Gate-0 skip, deleted-pre-receipt).
    Per §8d/§8e/§8f convention — when entity_name is None the Sync
    Record is not written; the GRN Map row carries the visible state.

Company:
  Resolved from the GRN's location → mapped warehouse → warehouse.company.
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


ENTITY_TYPE_GRN = "GRN"


def write_grn_pull_sync_record(
    *,
    pr_name: str | None,
    ee_grn_id: int,
    company: str | None,
    status: str,
    last_error: str | None = None,
    line_outcomes: list[dict[str, Any]] | None = None,
) -> str | None:
    """Upsert the Sync Record for one §9 GRN pull → PR attempt.

    line_outcomes shape (mirrors §9 Stage 2 PO push):
      {
        "source_line_ref": grn_detail_id (str),
        "source_line_number": int,
        "target_field": "received_qty" | "rejected_qty" | ...
        "line_status": "OK" | "Failed" | "Discrepancy",
        "reason": str | None,
        "linked_discrepancy": str | None  # docname of §23 stub if raised
      }

    Returns the Sync Record docname (or None when pr_name is None, i.e.
    no PR was created and we leave the visible state on the GRN Map row
    per §8d/§8e/§8f convention).
    """
    if not pr_name:
        return None
    if not frappe.db.exists("Purchase Receipt", pr_name):
        return None

    resolved_company = company or _company_for_item_sync()
    correlation_id = new_correlation_id()
    # Key on the PR docname (not just ee_grn_id) — natural dedup at
    # the upsert layer happens via (company, entity_doctype, entity_name,
    # direction); the idem_key just enforces uniqueness in the DB. PR
    # docnames are unique per insert (no recycle), so two re-pulls of
    # the same ee_grn_id share PR identity → same idem_key → upsert
    # short-circuits.
    idem_key = internal_job_key(
        job_type="grn_pull",
        company=resolved_company,
        target_doctype="Purchase Receipt",
        target_name=pr_name,
        payload={"ee_grn_id": int(ee_grn_id)},
    )

    sr = sync_record_mod.upsert(
        company=resolved_company,
        entity_doctype="Purchase Receipt",
        entity_name=pr_name,
        entity_type=ENTITY_TYPE_GRN,
        direction="Pull",
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
                    "ecs_integration_discrepancy": lo.get("linked_discrepancy"),
                },
            )
        sr.save(ignore_permissions=True)
    return sr.name


def write_grn_drift_sync_record(
    *,
    ee_grn_id: int,
    company: str,
    status: str,
    last_error: str | None,
) -> str | None:
    """Corrective commit 2026-05-29 (FIX 1) — Sync Record for the
    unknown-PO drift case. Distinct from `write_grn_pull_sync_record`
    because there is NO PR to key the Sync Record's entity link to;
    we key on the GRN Map row instead. Upsert is idempotent on re-pull
    (composite uniqueness on company + entity_doctype + entity_name +
    direction)."""
    grn_map_name = f"ECS-GRN-{ee_grn_id}"
    correlation_id = new_correlation_id()
    idem_key = internal_job_key(
        job_type="grn_pull_drift",
        company=company,
        target_doctype="EasyEcom GRN Map",
        target_name=grn_map_name,
        payload={"ee_grn_id": int(ee_grn_id)},
    )
    sr = sync_record_mod.upsert(
        company=company,
        entity_doctype="EasyEcom GRN Map",
        entity_name=grn_map_name,
        entity_type=ENTITY_TYPE_GRN,
        direction="Pull",
        correlation_id=correlation_id,
        idempotency_key=idem_key,
    )
    sr.db_set(
        {
            "status": status,
            "last_attempt_at": frappe.utils.now_datetime(),
            "correlation_id": correlation_id,
            "attempts": (sr.attempts or 0) + 1,
            "last_error": (last_error or "")[:1000] or None,
        },
        update_modified=True,
        commit=False,
    )
    return sr.name


__all__ = [
    "ENTITY_TYPE_GRN",
    "STATUS_DISCREPANCY",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "write_grn_pull_sync_record",
    "write_grn_drift_sync_record",
]
