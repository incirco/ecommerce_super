"""EasyEcom queue facade — single entry point for async work.

`enqueue_easyecom_job` is the only function flow code should call to defer
work. It creates the EasyEcom Queue Job tracking row and calls
`frappe.enqueue` referencing it. The worker (`workers.execute_job`) reads
the row, dispatches to the job_type-specific handler, and manages state.

This indirection is deliberate: Frappe RQ handles the worker pool, crash
recovery, and queue mechanics; the DocType layer captures correlation_id,
idempotency_key, per-Company concurrency, financial impact, and the
FDE-debuggable state machine that bare RQ doesn't provide (§6.3.1).

Never call frappe.enqueue directly for EasyEcom work — the facade owns
the pairing of DocType row + RQ job (CLAUDE.md "Anti-patterns").
"""

from __future__ import annotations

import frappe

from ecommerce_super.easyecom.queue.routing import (
    max_attempts_for,
    queue_for,
    timeout_for,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.redaction import redact


def enqueue_easyecom_job(
    job_type: str,
    company: str,
    *,
    target_doctype: str | None = None,
    target_name: str | None = None,
    payload: dict | None = None,
    correlation_id: str | None = None,
    parent_correlation_id: str | None = None,
    parent_event: str | None = None,
    parent_sync_record: str | None = None,
    parent_replay_plan: str | None = None,
    priority: int = 5,
    max_attempts: int | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Single entry point for enqueuing async EasyEcom work.

    Behaviour:
      1. Look up queue_tier and timeout from routing.py.
      2. Compute idempotency_key if not provided.
      3. Insert an EasyEcom Queue Job row with state=Queued, payload redacted.
      4. frappe.enqueue with job_name=<row.name> so RQ's job ID matches our
         DocType, enabling cross-reference via bench show-pending-jobs.
      5. Return the row name (== rq_job_id).

    Args:
      job_type: must be a key in QUEUE_FOR_JOB_TYPE.
      company: the Frappe Company this work runs against.
      target_doctype, target_name: optional — the Frappe doc this job operates on.
      payload: optional — JSON-serialisable input for the handler.
      correlation_id: optional — mint a fresh UUIDv7 if not provided.
      idempotency_key: optional — derive from payload+job_type if not provided.

    Returns:
      The EasyEcom Queue Job docname.
    """
    queue_tier = queue_for(job_type)
    timeout_seconds = timeout_for(job_type)
    if max_attempts is None:
        max_attempts = max_attempts_for(job_type)

    if not correlation_id:
        correlation_id = new_correlation_id()

    if not idempotency_key:
        # SPEC §6.1 / §2.7: the facade must not silently substitute a
        # divergent generic formula. Callers MUST build the key with the
        # appropriate per-operation builder from
        # `easyecom.utils.idempotency` (e.g. item_push_key, po_push_key)
        # or `internal_job_key` for internal-bookkeeping job types. This
        # raise is the contract — a missing key is a programmer error,
        # not a runtime detail to paper over.
        raise ValueError(
            f"enqueue_easyecom_job: idempotency_key is required for job_type "
            f"{job_type!r}. Use a per-operation builder from "
            "ecommerce_super.easyecom.utils.idempotency (e.g. item_push_key, "
            "po_push_key) or internal_job_key for internal-bookkeeping jobs."
        )

    redacted_payload = redact(payload) if payload else None

    qj = frappe.new_doc("EasyEcom Queue Job")
    qj.update(
        {
            "company": company,
            "job_type": job_type,
            "queue_tier": queue_tier,
            "priority": priority,
            "target_doctype": target_doctype,
            "target_name": target_name,
            "correlation_id": correlation_id,
            "parent_correlation_id": parent_correlation_id,
            "idempotency_key": idempotency_key,
            "parent_event": parent_event,
            "parent_sync_record": parent_sync_record,
            "parent_replay_plan": parent_replay_plan,
            "max_attempts": max_attempts,
            "attempts": 0,
            "state": "Queued",
            "payload": (
                frappe.as_json(redacted_payload)
                if redacted_payload is not None
                else None
            ),
            "created_at": frappe.utils.now_datetime(),
        }
    )
    qj.insert(ignore_permissions=True)
    frappe.db.commit()

    # gh#176 followup — surface enqueue failures at the moment they happen
    # instead of letting the DocType row sit at state=Queued indefinitely.
    # Before this guard, a transient Redis / RQ issue after the DocType
    # commit would silently leave an orphan; the hourly reclaimer would
    # catch it eventually but ops would look at a Queued row for ≤60 min
    # with no clue whether the underlying work fired.
    #
    # Two changes:
    #   1. try/except around frappe.enqueue → on failure, mark the row
    #      Failed with a clear reason. Raise so the caller sees the error
    #      (e.g. an on_submit hook surfaces to the user).
    #   2. Persist the returned RQ job id on the DocType so the reclaim
    #      path's _rq_job_alive check has something to correlate against.
    try:
        rq_job = frappe.enqueue(
            method="ecommerce_super.easyecom.queue.workers.execute_job",
            queue=queue_tier,
            job_name=qj.name,
            timeout=timeout_seconds,
            easyecom_queue_job=qj.name,
        )
    except Exception as exc:
        try:
            qj.reload()
            qj.transition_to_failed(
                error=(
                    f"frappe.enqueue failed after DocType insert: "
                    f"{type(exc).__name__}: {exc}. The Queue Job row "
                    "exists but no RQ task was created. Retry from the "
                    "form via the standard retry action."
                ),
                translation_key="ECS_QJ_ENQUEUE_FAILED",
            )
        except Exception:
            # If even the failure-transition write fails, the hourly
            # reclaim will still pick it up on the Queued path (gh#176).
            pass
        raise

    if rq_job is not None:
        try:
            rq_job_id = getattr(rq_job, "id", None)
            if rq_job_id:
                qj.db_set("rq_job_id", str(rq_job_id), update_modified=False)
                frappe.db.commit()
        except Exception:
            # Best-effort — losing rq_job_id doesn't break the enqueue,
            # it only degrades reclaim liveness detection.
            pass

    return qj.name


def cancel_job(job_id: str, reason: str) -> None:
    """Mark a Queued or Retrying job as Cancelled. If the job is already
    Running on a worker, the cancellation is recorded but the worker may
    complete the current attempt; the next attempt will early-exit by
    checking state at the top of execute_job."""
    qj = frappe.get_doc("EasyEcom Queue Job", job_id)
    qj.cancel(reason)


def retry_job(job_id: str) -> str:
    """Re-enqueue a Failed/Cancelled job. Re-uses the original correlation_id
    so historical logs link to the same operation. Resets attempts to 0
    and clears next_attempt_at (§6.3.8 manual retry)."""
    qj = frappe.get_doc("EasyEcom Queue Job", job_id)
    if qj.state not in {"Failed", "Cancelled"}:
        frappe.throw(
            frappe._(
                "Only Failed or Cancelled jobs can be retried; this job is {0}."
            ).format(qj.state)
        )

    qj.db_set(
        {
            "state": "Queued",
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
            "completed_at": None,
        },
        update_modified=False,
        commit=True,
    )

    # gh#176 followup — same enqueue-failure guard as enqueue_easyecom_job.
    try:
        rq_job = frappe.enqueue(
            method="ecommerce_super.easyecom.queue.workers.execute_job",
            queue=qj.queue_tier or queue_for(qj.job_type),
            job_name=f"{qj.name}-retry-{frappe.utils.now()}",
            timeout=timeout_for(qj.job_type),
            easyecom_queue_job=qj.name,
        )
    except Exception as exc:
        try:
            qj.reload()
            qj.transition_to_failed(
                error=(
                    f"retry_job: frappe.enqueue failed: "
                    f"{type(exc).__name__}: {exc}. State reset to Queued "
                    "but no RQ task was created. Retry again from the form."
                ),
                translation_key="ECS_QJ_ENQUEUE_FAILED",
            )
        except Exception:
            pass
        raise

    if rq_job is not None:
        try:
            rq_job_id = getattr(rq_job, "id", None)
            if rq_job_id:
                qj.db_set("rq_job_id", str(rq_job_id), update_modified=False)
                frappe.db.commit()
        except Exception:
            pass
    return qj.name
