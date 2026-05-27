"""§8f Sync Record writes for the Supplier-flow operation points.

Mirrors _customer_sync_records.py exactly — same shape, scoped to
Supplier entity. This is the THIRD entity-sync flow to write Sync
Records (after §8d Item and §8e Customer). §8a Location, §8b Channel,
§8c Tax, and §8e Stage 2 (country/state) are foundational and
deliberately do NOT write Sync Records — they live at the API Call
layer only.

Sync Record state machine recap (§7.3):
  Pending → Running → {Success | Failed | Discrepancy}
The integration owns transitions; `db_set` bypasses validate.

Status mapping for §8f Pull (Stage 3) per supplier:
  - Mapped outcomes → Success (op completed cleanly)
  - Flagged-Not-Created (Indian + bad GSTIN, IC threw) → Success
    (deliberate non-creation; the decision succeeded; the FDE finds
    FNC rows via the Supplier Map worklist)
  - Drift (Stage 5 post-flip detection) → Discrepancy
  - Raised exception inside the savepoint → Failed (written by the
    flow's _on_failure callback, outside the rollback)

Stage 4 Push (when built) will mirror the customer push variant.

Entity-type / entity-doctype:
  - entity_type = "Supplier"
  - entity_doctype = "Supplier" (the Dynamic Link target)
  - The Supplier Map's erpnext_doctype is also "Supplier" — they
    line up by design (§8f Stage 1 schema).

Company:
  Same approach as §8d/§8e: §8f suppliers are account-wide (EE has
  no per-Company vendor master) but Sync Records require a Company
  per §10.1.2. Reuse _company_for_item_sync — same picker, no point
  duplicating.
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


# §31.2.3 entity_type for Supplier entity-sync rows.
ENTITY_TYPE_SUPPLIER = "Supplier"


def write_supplier_pull_sync_record(
    *,
    entity_name: str | None,
    ee_vendor_c_id: str,
    status: str,
    last_error: str | None = None,
) -> str | None:
    """Upsert and transition the Sync Record for one Supplier Pull
    operation. Called from process_one_supplier / drift detector.

    entity_name may be None for Flagged-Not-Created outcomes where
    no Supplier was created on the ERPNext side. In that case no
    Sync Record row is written (Dynamic Link can't resolve to a
    non-existent target — same FNC semantics as §8d/§8e); the
    Supplier Map row carries the visible FNC state for the FDE
    worklist.

    Returns the Sync Record docname (or None for FNC).
    """
    return _upsert_with_status(
        entity_name=entity_name,
        ee_vendor_c_id=ee_vendor_c_id,
        direction="Pull",
        status=status,
        last_error=last_error,
    )


def write_supplier_push_sync_record(
    *,
    entity_name: str,
    ee_vendor_c_id: str,
    status: str,
    last_error: str | None = None,
) -> str | None:
    """Mirror of the pull variant for Stage 4 push. Defined here so
    this helper module's shape parallels _customer_sync_records /
    _item_sync_records cleanly. Not exercised in Stage 3."""
    return _upsert_with_status(
        entity_name=entity_name,
        ee_vendor_c_id=ee_vendor_c_id,
        direction="Push",
        status=status,
        last_error=last_error,
    )


def _upsert_with_status(
    *,
    entity_name: str | None,
    ee_vendor_c_id: str,
    direction: str,
    status: str,
    last_error: str | None,
) -> str | None:
    # Sync Record's entity_name is a Dynamic Link — Frappe validates
    # target exists on insert. For Flagged-Not-Created outcomes there's
    # no Supplier to link to. Same semantic as §8d/§8e: no entity, no
    # record; the Supplier Map's status field already carries the FNC
    # state for the FDE worklist.
    if not entity_name:
        return None
    if not frappe.db.exists("Supplier", entity_name):
        return None

    company = _company_for_item_sync()
    correlation_id = new_correlation_id()
    idem_key = internal_job_key(
        job_type=f"supplier_{direction.lower()}",
        company=company,
        target_doctype="Supplier",
        target_name=str(ee_vendor_c_id),
    )

    sr = sync_record_mod.upsert(
        company=company,
        entity_doctype="Supplier",
        entity_name=entity_name,
        entity_type=ENTITY_TYPE_SUPPLIER,
        direction=direction,
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
    return sr.name


__all__ = [
    "ENTITY_TYPE_SUPPLIER",
    "STATUS_DISCREPANCY",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "write_supplier_pull_sync_record",
    "write_supplier_push_sync_record",
]
