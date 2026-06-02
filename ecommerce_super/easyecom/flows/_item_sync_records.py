"""§8d audit follow-up #1 — Sync Record writes for the five Item-flow
operation points.

This is the FIRST entity-sync flow to write Sync Records. 8e Customer
and 8f Supplier will mirror this pattern when they ship. 8a Location,
8b Channel, and 8c Tax Rule Map deliberately do NOT — they're §7.7
foundational/config calls, not entity-sync work (see
location_discovery.py:197 for the explicit comment).

Sync Record state machine recap (§7.3 / Sync Record controller):
  Pending → Running → {Success | Failed | Discrepancy}
The integration owns transitions; the controller's validate is a
defensive guard against out-of-band human edits. Using `db_set`
bypasses validate, which is the right move for flow-driven
transitions (per the controller's own retry_now → db_set example).

Status mapping per the five op points:
  Pull (per product):
    - Mapped / Created-Flagged outcomes → Success
    - Flagged-Not-Created (HSN held, unsupported type) → Success
      (the operation completed; the decision was 'don't create'; the
      FDE finds FNC items via the Item Map worklist, not the
      Sync Record)
    - Drift outcome (post-flip detection finds divergence) → Discrepancy
    - Raised exception inside the savepoint → Failed (written by
      the flow's _on_failure callback, outside the rollback)

  Push (individual / sweep / drift-resolution):
    - Create / Update succeeded → Success
    - Flagged-Not-Pushed (missing mandatory, unpushed component) → Failed
    - Raised exception → Failed

  Lifecycle push (ActivateDeactivateProduct):
    - Sent successfully → Success
    - No-op (item never had ee_product_id) → Success (entity hasn't
      reached EE; no work to do; not a failure)
    - Raised → Failed

Entity-type / entity-doctype:
  - entity_type is the broader §31.2.3 classification — always "Item"
    for §8d (the existing enum has no "Product Bundle" — Product
    Bundles ARE catalogue items in spirit, and the entity_doctype
    field carries the actual DocType for the link).
  - entity_doctype is the actual link target — "Item" for normal
    products, "Product Bundle" for combos.

Company:
  §8d items are account-wide (not per-Company), but the Sync Record
  schema requires a Company per §10.1.2 (multi-Company isolation).
  Pick the first enabled EasyEcom Company Settings; fall back to
  the sentinel "EasyEcom-Shared" when none configured. Same
  approach as the Item Push queue job.

Correlation ID:
  Each pull/push run mints a fresh UUIDv7 (via new_correlation_id).
  A re-pull of the same entity gets a new correlation_id but
  upsert reuses the existing Sync Record (composite UNIQUE on
  company × entity × direction); so the row's correlation_id
  reflects the LAST run, not the first. That's the right semantic
  — the FDE looking at the row cares about the latest attempt.

Idempotency:
  Per-flow idempotency keys (item_push_key for push,
  internal_job_key for pull) so a re-pull/re-push of the same
  payload produces the same key — EE-side dedup if we ever call
  twice with the same content.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.doctype.easyecom_sync_record import (
    easyecom_sync_record as sync_record_mod,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.idempotency import (
    internal_job_key,
    item_push_key,
)

# Sync Record status enum (SPEC §7.3 M1 binary state machine).
STATUS_PENDING = "Pending"
STATUS_RUNNING = "Running"
STATUS_SUCCESS = "Success"
STATUS_FAILED = "Failed"
# gh#16: STATUS_DISCREPANCY is now an ALIAS for "Failed" — the per-record
# outcome is binary by spec, so a discrepancy on any line makes the
# whole record Failed. The constant is preserved to keep existing flow
# callsites (item_pull / supplier_pull / grn_pull / po_push) intentful:
# `status=STATUS_DISCREPANCY` reads as "this is a drift-flavored
# failure" while writing the correct binary value to the DB. Drift
# context is preserved in last_error so §22 alert routing can still
# differentiate. New callsites should prefer STATUS_FAILED directly.
STATUS_DISCREPANCY = "Failed"

# Entity type — Item is the closest match in the §31.2.3 enum for
# both normal items and Product Bundles. entity_doctype carries the
# real DocType for the link.
ENTITY_TYPE_ITEM = "Item"

# Sentinel Company name when no EasyEcom Company Settings is enabled.
# Same sentinel the Item Push queue job uses (item_push.py
# _company_for_item_push) so both row stores reference the same
# logical "shared" identity.
SHARED_COMPANY_SENTINEL = "EasyEcom-Shared"


def write_item_pull_sync_record(
    *,
    entity_doctype: str,
    entity_name: str,
    sku: str,
    status: str,
    last_error: str | None = None,
) -> str:
    """Upsert and transition the Sync Record for one Item Pull
    operation. Called from process_one_product / drift detector.

    `entity_name` may be the Item docname or the Product Bundle
    docname; `entity_doctype` distinguishes. For FNC outcomes (no
    creation happened) entity_name CAN be None — pass the SKU as
    entity_name to keep the row identifiable.

    Returns the Sync Record docname (for tests / FDE links)."""
    return _upsert_with_status(
        entity_doctype=entity_doctype,
        entity_name=entity_name or f"<unmapped:{sku}>",
        direction="Pull",
        sku=sku,
        status=status,
        last_error=last_error,
    )


def write_item_push_sync_record(
    *,
    entity_doctype: str,
    entity_name: str,
    sku: str,
    status: str,
    last_error: str | None = None,
) -> str:
    """Upsert and transition the Sync Record for one Item Push
    operation (individual / batch-sweep job / lifecycle). One row per
    (Company × entity × Push); subsequent pushes update the same row's
    status + last_attempt_at."""
    return _upsert_with_status(
        entity_doctype=entity_doctype,
        entity_name=entity_name,
        direction="Push",
        sku=sku,
        status=status,
        last_error=last_error,
    )


def _upsert_with_status(
    *,
    entity_doctype: str,
    entity_name: str,
    direction: str,
    sku: str,
    status: str,
    last_error: str | None,
) -> str | None:
    # Sync Record's entity_name is a Dynamic Link — Frappe validates
    # the target exists on insert. For Flagged-Not-Created outcomes
    # (missing HSN, unsupported product_type, etc.) and for failures
    # raised before the ERPNext doc was created, there's no entity
    # to link to. Writing the SR would hit "Could not find {Doctype}:
    # {sku}" and bubble up via msgprint — the user sees a wall of
    # errors even though the pull itself succeeded.
    #
    # The right semantic (per CLAUDE.md / §10.1.2): Sync Records are
    # entity-centric, one per (ERPNext doc × direction). No entity,
    # no record. The Item Map row already carries the FNC / failure
    # state for the FDE worklist — that's the visible record.
    if not entity_doctype or not entity_name:
        return None
    if not frappe.db.exists(entity_doctype, entity_name):
        return None

    company = _company_for_item_sync()
    correlation_id = new_correlation_id()
    idem_key = _idempotency_key_for_op(
        direction=direction, sku=sku, company=company
    )

    sr = sync_record_mod.upsert(
        company=company,
        entity_doctype=entity_doctype,
        entity_name=entity_name,
        entity_type=ENTITY_TYPE_ITEM,
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
        # Truncate long error chains so the field stays readable. The
        # full detail lives in the Error Log already.
        updates["last_error"] = (last_error or "")[:1000]
    else:
        updates["last_error"] = None

    # db_set bypasses validate (which guards the state machine for
    # out-of-band human edits, per the controller). The integration
    # owns transitions, so we go straight to the terminal status.
    sr.db_set(updates, update_modified=True, commit=False)
    return sr.name


def map_outcome_to_sync_status(outcome: Any, direction: str) -> tuple[str, str | None]:
    """Translate a ProductOutcome / PushOutcome into a Sync Record
    status + optional last_error string.

    Pull outcomes (Stage 2/4/5):
      - Mapped, Created-Flagged → Success (op completed; flags are
        FDE worklist concerns surfaced via the Item Map, not via
        the Sync Record)
      - Flagged-Not-Created → Success (deliberate non-creation; the
        decision succeeded)
      - Drift → Discrepancy (§7.3: divergence is not failure)

    Push outcomes (Stage 3/4/6):
      - create / update with pushed=True → Success
      - skipped → Success (deliberate skip — e.g. disabled item, or
        a hook short-circuit)
      - flagged → Failed (we couldn't push; the SKU isn't on EE)
      - error → Failed (exception captured by the batch sweep)
    """
    if direction == "Pull":
        if outcome.status == "Drift":
            return STATUS_DISCREPANCY, _join_reasons(outcome.flag_reasons)
        # Mapped / Created-Flagged / Flagged-Not-Created — operation
        # succeeded; the flag (if any) is a content concern, surfaced
        # via the Item Map's status field, not via the Sync Record.
        return STATUS_SUCCESS, _join_reasons(outcome.flag_reasons) if outcome.flag_reasons else None

    # direction == "Push"
    if outcome.pushed:
        return STATUS_SUCCESS, None
    if outcome.operation == "skipped":
        return STATUS_SUCCESS, _join_reasons(outcome.flag_reasons) if outcome.flag_reasons else None
    # flagged / error → Failed
    return STATUS_FAILED, _join_reasons(outcome.flag_reasons)


def _join_reasons(reasons: list[str] | None) -> str | None:
    if not reasons:
        return None
    return " || ".join(reasons)


def _company_for_item_sync() -> str:
    """Pick a Company for the Sync Record row. §8d items are
    account-wide, but Sync Records are Company-scoped per §10.1.2.

    Order:
      1. First enabled EasyEcom Company Settings
      2. First Company on the site (fallback — real ERPNext sites
         always have at least one)
      3. Raise — pre-onboarding state where no Company exists at all
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
            "Cannot write Item Sync Record: no Company exists on this site."
        )
    return fallback


def _idempotency_key_for_op(
    *, direction: str, sku: str, company: str
) -> str:
    """Per-operation idempotency key.

    Pull: synthesised via internal_job_key (no EE-side write happens;
    the key just makes the Sync Record row's idempotency_key non-
    empty per the upsert contract).

    Push: item_push_key with the SKU as item_code. (The PUSH itself
    has its OWN finer-grained idempotency key built per-payload by
    item_push._idempotency_key; this key is the Sync Record's
    coarser identity for upsert purposes.)
    """
    if direction == "Pull":
        return internal_job_key(
            job_type="item_pull",
            company=company,
            target_doctype="Item",
            target_name=sku,
        )
    return item_push_key(
        company=company,
        item_code=sku,
        ee_location_key=company,  # account-wide; use Company as the location proxy
        change_hash="item_push_v1",
    )
