"""§8e Sync Record writes for the Customer-flow operation points.

Mirrors _item_sync_records.py — same shape, scoped to Customer entity.
This is the SECOND entity-sync flow to write Sync Records (after §8d
Item). 8f Supplier will mirror this when it ships. 8a Location, 8b
Channel, 8c Tax, and 8e Stage 2 (country/state) are foundational and
deliberately do NOT write Sync Records.

Sync Record state machine recap (§7.3 / Sync Record controller):
  Pending → Running → {Success | Failed | Discrepancy}
The integration owns transitions; `db_set` bypasses validate.

Status mapping for §8e Pull (Stage 3) per customer:
  - Mapped / Created-Flagged outcomes → Success (op completed; flags
    are FDE worklist concerns surfaced via the Customer Map, not via
    the Sync Record)
  - Flagged-Not-Created (invalid GSTIN held) → Success (deliberate
    non-creation; the decision succeeded; the FDE finds FNC rows via
    the Customer Map worklist)
  - Drift (Stage 5 post-flip detection) → Discrepancy
  - Raised exception inside the savepoint → Failed (written by the
    flow's _on_failure callback, outside the rollback)

Stage 4 Push (when built) will mirror item push's status mapping.

Entity-type / entity-doctype:
  - entity_type = "Customer" (the §31.2.3 broad classification)
  - entity_doctype = "Customer" (the actual Frappe DocType for the
    Dynamic Link). The Customer Map's own erpnext_doctype is also
    "Customer" — they line up.

Company:
  Same approach as §8d: §8e customers are account-wide (EE has no
  per-Company customer master) but Sync Records require a Company per
  §10.1.2. Reuse _company_for_item_sync — same picker, no point
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


# §31.2.3 entity_type for Customer entity-sync rows.
ENTITY_TYPE_CUSTOMER = "Customer"


def write_customer_pull_sync_record(
    *,
    entity_name: str | None,
    ee_c_id: str,
    status: str,
    last_error: str | None = None,
) -> str | None:
    """Upsert and transition the Sync Record for one Customer Pull
    operation. Called from process_one_customer / drift detector.

    entity_name may be None for Flagged-Not-Created outcomes where no
    Customer was created on the ERPNext side. In that case no Sync
    Record row is written (Dynamic Link can't resolve to a non-existent
    target — same FNC semantics as §8d); the Customer Map row carries
    the visible FNC state.

    Returns the Sync Record docname (or None for FNC).
    """
    return _upsert_with_status(
        entity_name=entity_name,
        ee_c_id=ee_c_id,
        direction="Pull",
        status=status,
        last_error=last_error,
    )


def write_customer_push_sync_record(
    *,
    entity_name: str,
    ee_c_id: str,
    status: str,
    last_error: str | None = None,
) -> str | None:
    """Mirror of the pull variant for the Stage 4 push. Not exercised
    in Stage 3 but defined here so the helper module's shape parallels
    _item_sync_records cleanly."""
    return _upsert_with_status(
        entity_name=entity_name,
        ee_c_id=ee_c_id,
        direction="Push",
        status=status,
        last_error=last_error,
    )


def _upsert_with_status(
    *,
    entity_name: str | None,
    ee_c_id: str,
    direction: str,
    status: str,
    last_error: str | None,
) -> str | None:
    # Sync Record's entity_name is a Dynamic Link — Frappe validates
    # target exists on insert. For Flagged-Not-Created outcomes there's
    # no Customer to link to. Same semantic as §8d: no entity, no
    # record; the Customer Map's status field already carries the FNC
    # state for the FDE worklist.
    if not entity_name:
        return None
    if not frappe.db.exists("Customer", entity_name):
        return None

    company = _company_for_item_sync()
    correlation_id = new_correlation_id()
    idem_key = internal_job_key(
        job_type=f"customer_{direction.lower()}",
        company=company,
        target_doctype="Customer",
        target_name=str(ee_c_id),
    )

    sr = sync_record_mod.upsert(
        company=company,
        entity_doctype="Customer",
        entity_name=entity_name,
        entity_type=ENTITY_TYPE_CUSTOMER,
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


# Re-export the status constants so the customer-pull flow can write
# them without reaching back into the item module.
__all__ = [
    "ENTITY_TYPE_CUSTOMER",
    "STATUS_DISCREPANCY",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "write_customer_pull_sync_record",
    "write_customer_push_sync_record",
]
