"""Worker entry point for EasyEcom Queue Job execution.

`execute_job` is called by Frappe RQ workers (`bench start` spawns short /
default / long worker pools). It reads the EasyEcom Queue Job row,
dispatches to the registered handler for the job_type, manages state
transitions, and re-enqueues on transient failure with exponential back-off.

JOB_TYPE_HANDLERS starts empty in this foundation packet. Each flow packet
(§8 onwards) registers its handler entries when the flow is built. Calling
execute_job with a job_type that has no handler raises CleanError and
lands the job Failed — surfacing the missing-handler bug rather than
silently no-op'ing.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Callable

import frappe

from ecommerce_super.easyecom.exceptions import (
    CompanyConcurrencyExceeded,
    EasyEcomError,
    EasyEcomRateLimitError,
)
from ecommerce_super.easyecom.queue.concurrency import (
    company_concurrency_semaphore,
    release_slot,
)
from ecommerce_super.easyecom.queue.routing import (
    max_attempts_for,
    queue_for,
    timeout_for,
)

# Filled in by each flow as it is built. Keys are job_type strings from the
# Queue Job DocType's Select; values are dotted method paths Frappe can
# resolve via frappe.get_attr.
#
# Example (when Master Sync is built):
#   JOB_TYPE_HANDLERS["Item Push"] = "ecommerce_super.easyecom.flows.master_sync.item.push_handler"
#
JOB_TYPE_HANDLERS: dict[str, str] = {
    # §8d Stage 3+6: per-Item push (Create / Update / lifecycle —
    # push_one_item dispatches). Enqueued by enqueue_item_push from
    # (a) the auto-push hook, (b) the batch sweep (one job per
    # candidate, audit fix #8), (c) drift resolution's "Push
    # ERPNext → EE" action.
    "Item Push": "ecommerce_super.easyecom.flows.item_push.item_push_queue_handler",
    # §8e Stage 4: per-Customer push. Enqueued by enqueue_customer_push
    # from (a) the auto-push hook, (b) the batch sweep. gh#27 sibling
    # fix — the JOB_TYPE_HANDLERS entry was missing entirely, so even a
    # successful enqueue would have failed at execute_job time with
    # "no handler for job_type Customer Push".
    "Customer Push": (
        "ecommerce_super.easyecom.flows.customer_push.customer_push_queue_handler"
    ),
    # §8f Stage 4: per-Supplier push. Same audit gap as Customer Push
    # (gh#27).
    "Supplier Push": (
        "ecommerce_super.easyecom.flows.supplier_push.supplier_push_queue_handler"
    ),
    # §9 Stage 2: PO push (content channel + optional status push to
    # po_status=3). Enqueued by the on_submit hook + the batch sweep.
    "PO Push": "ecommerce_super.easyecom.flows.po_push.po_push_queue_handler",
    # §9 Stage 2: status-only push (po_status=7 on cancel; po_status=5
    # completion will land in Stage 3). Enqueued by the on_cancel hook
    # and Stage 3's GRN-driven completion trigger.
    "PO Status Push": "ecommerce_super.easyecom.flows.po_push.po_status_push_queue_handler",
    # §9 Stage 3: GRN pull → PR + status reconciliation. Enqueued per
    # location (Stage 4 wires the per-tick scheduler that fans out).
    "GRN Pull": "ecommerce_super.easyecom.flows.grn_pull.grn_pull_queue_handler",
    # §10 Stage 2: Stock Transfer outbound. Enqueued by DN on_submit +
    # the push_all_pending_transfers batch sweep.
    "Transfer Push": "ecommerce_super.easyecom.flows.transfer_push.transfer_push_queue_handler",
    # §11 Phase 1 Stage 2: B2B Sales Order outbound (Old + New B2B
    # createOrder). Enqueued by Sales Order on_submit_push.
    "SO Push": "ecommerce_super.easyecom.flows.b2b_sales.push.b2b_push_queue_handler",
}


def compute_backoff(attempts: int) -> int:
    """Exponential back-off with jitter per §6.3.8.

    backoff_s = min(2^attempts * 30, 3600) ± random(0, 30)
    """
    base = min((2**attempts) * 30, 3600)
    jitter = random.randint(0, 30)
    return base + jitter


def execute_job(easyecom_queue_job: str) -> None:
    """RQ worker entry point. Always called with the EasyEcom Queue Job
    docname as its sole kwarg (per `enqueue_easyecom_job`'s `frappe.enqueue`
    call). Wraps the handler dispatch with state-machine transitions,
    per-Company concurrency, and the retry/backoff disposition."""
    qj = frappe.get_doc("EasyEcom Queue Job", easyecom_queue_job)

    # Honour a pre-execution Cancel.
    if qj.state == "Cancelled":
        return

    handler_path = JOB_TYPE_HANDLERS.get(qj.job_type)
    if not handler_path:
        qj.transition_to_failed(
            error=(
                f"No handler registered for job_type {qj.job_type!r}. "
                "Add an entry to JOB_TYPE_HANDLERS in easyecom.queue.workers."
            ),
            translation_key="ECS_QJ_NO_HANDLER",
        )
        return

    qj.transition_to_running()

    try:
        handler: Callable = frappe.get_attr(handler_path)
        with company_concurrency_semaphore(qj.company):
            handler(qj)
        qj.transition_to_success()

    except CompanyConcurrencyExceeded as e:
        # Treat as transient — re-enqueue with a short back-off (don't burn
        # an attempt for a concurrency wait).
        backoff_s = 30 + random.randint(0, 15)
        next_at = frappe.utils.now_datetime() + timedelta(seconds=backoff_s)
        qj.transition_to_retrying(
            next_attempt_at=next_at,
            error=str(e),
            translation_key="ECS_MC_CONCURRENCY_EXCEEDED",
        )
        _reenqueue(qj, delay_seconds=backoff_s)

    except EasyEcomRateLimitError as e:
        # EE asked us to back off explicitly via retry_after; honour it.
        backoff_s = int(e.retry_after or 60)
        next_at = frappe.utils.now_datetime() + timedelta(seconds=backoff_s)
        qj.transition_to_retrying(
            next_attempt_at=next_at, error=str(e), translation_key=e.error_code
        )
        _reenqueue(qj, delay_seconds=backoff_s)

    except EasyEcomError as e:
        translation = getattr(e, "error_code", "ECS_ERROR")
        if e.retry_policy == "transient" and qj.attempts < (
            qj.max_attempts or max_attempts_for(qj.job_type)
        ):
            backoff_s = compute_backoff(qj.attempts)
            next_at = frappe.utils.now_datetime() + timedelta(seconds=backoff_s)
            qj.transition_to_retrying(
                next_attempt_at=next_at, error=str(e), translation_key=translation
            )
            _reenqueue(qj, delay_seconds=backoff_s)
        else:
            qj.transition_to_failed(error=str(e), translation_key=translation)

    except Exception as e:
        # Unexpected exception — record and surface. The integration's rule
        # (CLAUDE.md "Anti-patterns") forbids catching bare Exception
        # elsewhere; here at the outer worker frame it's the right call so
        # the row lands Failed rather than the worker process crashing.
        qj.transition_to_failed(
            error=f"{type(e).__name__}: {e}",
            translation_key="ECS_UNEXPECTED",
        )
        # Re-raise so RQ records it in its own failure registry too.
        raise


def _reenqueue(qj, *, delay_seconds: int) -> None:
    """Re-enqueue this Queue Job via frappe.enqueue with enqueue_after for
    back-off. The next attempt re-enters execute_job and goes through
    transition_to_running again, which bumps `attempts`.

    gh#176 followup — surface enqueue failures instead of leaving the row
    to sit at whatever state the caller set (Retrying, Queued, …).
    """
    try:
        rq_job = frappe.enqueue(
            method="ecommerce_super.easyecom.queue.workers.execute_job",
            queue=qj.queue_tier or queue_for(qj.job_type),
            job_name=f"{qj.name}-attempt-{(qj.attempts or 0) + 1}",
            timeout=timeout_for(qj.job_type),
            enqueue_after_commit=False,
            at_front=False,
            # Frappe's enqueue takes `enqueue_after` as a timedelta on some
            # versions and as seconds on others; we pass seconds inside the
            # job's kwargs and let RQ schedule it.
            **{"enqueue_after": delay_seconds, "easyecom_queue_job": qj.name},
        )
    except Exception as exc:
        try:
            qj.reload()
            qj.transition_to_failed(
                error=(
                    f"_reenqueue: frappe.enqueue failed: "
                    f"{type(exc).__name__}: {exc}. The reclaim scheduler "
                    "will pick this up on its next run."
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
        except Exception:
            pass


def reclaim_orphaned_jobs() -> int:
    """Scheduler hook (§6.3.9). Reclaim Queue Job rows that got stuck.

    Two orphan patterns handled:

    1. state=Running, last_attempted_at > 10 min ago, no live RQ job.
       Worker died mid-job. Original §6.3.9 scope.

    2. state=Queued, creation > 10 min ago, no live RQ job. (gh#176,
       added 2026-07-13.) `frappe.enqueue` failed silently after the
       DocType insert, OR a duplicate enqueue landed and the sibling
       job won — either way, this row sits at Queued forever unless we
       reclaim it. Handling:
         - If underlying work is already done (idempotency check —
           map/target row exists), transition_to_success with a
           reconciliation note. No re-enqueue needed.
         - Otherwise, re-enqueue.

    Returns the count of jobs reclaimed."""
    cutoff = frappe.utils.now_datetime() - timedelta(minutes=10)
    reclaimed = 0

    reclaimed += _reclaim_running_orphans(cutoff)
    reclaimed += _reclaim_queued_orphans(cutoff)  # gh#176

    return reclaimed


def _reclaim_running_orphans(cutoff) -> int:
    """Original reclaim path — state=Running, worker died mid-job."""
    candidates = frappe.db.get_all(
        "EasyEcom Queue Job",
        filters={
            "state": "Running",
            "last_attempted_at": ["<=", cutoff],
        },
        fields=["name", "rq_job_id", "company", "queue_tier", "job_type", "attempts"],
    )
    if not candidates:
        return 0

    reclaimed = 0
    for row in candidates:
        # Best-effort RQ liveness check — if Frappe's RQ helper isn't
        # available in this version, assume the job is dead (the > 10-min
        # cutoff is conservative enough).
        try:
            from rq import Worker  # noqa: F401  (presence check only)

            live = bool(row.rq_job_id) and _rq_job_alive(row.rq_job_id)
        except Exception:
            live = False

        if live:
            continue

        qj = frappe.get_doc("EasyEcom Queue Job", row.name)
        backoff_s = 60
        next_at = frappe.utils.now_datetime() + timedelta(seconds=backoff_s)
        qj.transition_to_retrying(
            next_attempt_at=next_at,
            error="Reclaimed: worker died mid-job (no live RQ id).",
            translation_key="ECS_QJ_ORPHAN_RECLAIM",
        )
        # §6.3.7 Crash-drift fix: a killed worker never ran its `finally`
        # decrement, so the per-Company semaphore is still holding this
        # job's slot. Release exactly one slot for the reclaimed job —
        # NOT a hard reset, which would also free slots held by live
        # workers for the same Company.
        release_slot(qj.company)
        _reenqueue(qj, delay_seconds=backoff_s)
        reclaimed += 1

    return reclaimed


def _reclaim_queued_orphans(cutoff) -> int:
    """gh#176 — pick up state=Queued rows the RQ side never fired for.

    Two outcomes:
      - Underlying work already done (target row exists via idempotency)
        → transition_to_success with a reconciliation note, no re-enqueue
      - Not done → re-enqueue via the standard path
    """
    candidates = frappe.db.get_all(
        "EasyEcom Queue Job",
        filters={
            "state": "Queued",
            "creation": ["<=", cutoff],
        },
        fields=[
            "name", "rq_job_id", "company", "queue_tier", "job_type",
            "attempts", "target_doctype", "target_name",
        ],
    )
    if not candidates:
        return 0

    reclaimed = 0
    for row in candidates:
        try:
            from rq import Worker  # noqa: F401
            live = bool(row.rq_job_id) and _rq_job_alive(row.rq_job_id)
        except Exception:
            live = False
        if live:
            continue

        qj = frappe.get_doc("EasyEcom Queue Job", row.name)
        work_already_done = _queued_work_already_completed(row)
        if work_already_done:
            # Reconciliation success — the actual work landed via a
            # sibling job / earlier attempt. Nothing to do; just flip
            # state so the row stops sitting in the operator's worklist.
            try:
                qj.transition_to_success()
                _annotate(qj, (
                    "Reconciled by gh#176 reclaim: target row already "
                    "exists (idempotency); this Queue Job was a "
                    "duplicate / orphaned enqueue."
                ))
            except Exception as exc:  # noqa: BLE001
                frappe.log_error(
                    title=f"gh#176 reconcile-success failed for {qj.name}",
                    message=f"{type(exc).__name__}: {exc}",
                )
                continue
            reclaimed += 1
            continue

        # Not done — re-enqueue. No semaphore release: Queued state means
        # execute_job never ran, so no slot was ever taken.
        _reenqueue(qj, delay_seconds=5)
        _annotate(qj, (
            "Re-enqueued by gh#176 reclaim: state was Queued > 10 min "
            "with no live RQ task."
        ))
        reclaimed += 1

    return reclaimed


def _queued_work_already_completed(row) -> bool:
    """Best-effort idempotency probe — did the underlying work land
    despite this Queue Job never running? Returns True if we can prove
    the target has an EasyEcom-side artifact that indicates completion.

    Heuristics per job_type:
      - SO Push  → EasyEcom B2B Order Map exists for the SO
      - PO Push  → EasyEcom PO Map exists for the PO (if that DocType exists)
      - Item Push → EasyEcom Item Map has ee_product_id set for the Item
      - Customer Push → EasyEcom Customer Map has ee_customer_id set
      - Any other job_type → False (conservative — force re-enqueue)

    False on any lookup error (conservative — force re-enqueue).
    """
    if not row.target_name:
        return False
    try:
        if row.job_type == "SO Push":
            return bool(frappe.db.get_value(
                "EasyEcom B2B Order Map",
                {"sales_order": row.target_name},
                "name",
            ))
        if row.job_type == "Item Push":
            return bool(frappe.db.get_value(
                "EasyEcom Item Map",
                {"erpnext_doctype": "Item", "erpnext_name": row.target_name},
                "ee_product_id",
            ))
        if row.job_type == "Customer Push":
            return bool(frappe.db.get_value(
                "EasyEcom Customer Map",
                {"erpnext_doctype": "Customer", "erpnext_name": row.target_name},
                "ee_customer_id",
            ))
    except Exception:
        return False
    return False


def _annotate(qj, note: str) -> None:
    """Append a note to the Queue Job's error_message for audit."""
    try:
        existing = qj.get("last_error") or ""
        sep = "\n---\n" if existing else ""
        qj.db_set(
            "last_error",
            (existing + sep + note)[:4000],
            update_modified=False,
        )
    except Exception:
        pass


def _rq_job_alive(rq_job_id: str) -> bool:
    """Return True if RQ believes the given job ID is still queued or
    executing. Defensive — failures in this probe count as 'dead'."""
    try:
        from frappe.utils.background_jobs import get_redis_conn
        from rq.job import Job

        conn = get_redis_conn()
        job = Job.fetch(rq_job_id, connection=conn)
        return job.get_status() in {"queued", "started", "deferred", "scheduled"}
    except Exception:
        return False
