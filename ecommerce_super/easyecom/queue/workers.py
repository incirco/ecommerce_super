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
JOB_TYPE_HANDLERS: dict[str, str] = {}


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
    transition_to_running again, which bumps `attempts`."""
    frappe.enqueue(
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


def reclaim_orphaned_jobs() -> int:
    """Scheduler hook (§6.3.9). Find Queue Job rows in state=Running that
    haven't been touched in >10 minutes AND have no live RQ job, and
    transition them back to Retrying for re-enqueue.

    Returns the count of jobs reclaimed (for observability/logging)."""
    cutoff = frappe.utils.now_datetime() - timedelta(minutes=10)
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
