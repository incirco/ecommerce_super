"""Queue tier routing for EasyEcom jobs.

Maps every job_type to a Frappe RQ queue tier (`short` / `default` / `long`)
and the per-job-type timeout in seconds. Lifted verbatim from SPEC §31.4.3
and §6.3.2.

Adding a new job_type requires:
  1. Adding it to the EasyEcom Queue Job DocType's job_type Select options.
  2. Adding an entry to both dicts here.
  3. Registering a handler in easyecom.queue.workers.JOB_TYPE_HANDLERS.
"""

from __future__ import annotations

# Tier mapping per §31.4.3
QUEUE_FOR_JOB_TYPE: dict[str, str] = {
    # Short queue (low-latency: webhook responses, fast compute)
    "Webhook Process": "short",
    "SLA Breach Compute": "short",
    "Configuration Audit Write": "short",
    # Default queue (routine integration work)
    "Item Push": "default",
    "Customer Push": "default",
    "Supplier Push": "default",
    "PO Push": "default",
    # §9 Stage 2 — status-channel push (separately observable from content).
    "PO Status Push": "default",
    "SO Push": "default",
    "B2B Invoice Push": "default",
    "Order Pull": "default",
    "GRN Pull": "default",
    "Return Pull": "default",
    "Field Mapping Compile": "default",
    # Long queue (bulk and scheduled compute)
    "Inventory Pull": "long",
    "Master Sync Bulk": "long",
    "Replay Plan Step": "long",
    "Schema Snapshot Compute": "long",
    "Mapping Coverage Compute": "long",
    "Morning Brief Compute": "long",
}

# Per-job-type timeouts in seconds per §31.4.3
TIMEOUT_FOR_JOB_TYPE: dict[str, int] = {
    "Webhook Process": 60,
    "Item Push": 120,
    "Customer Push": 120,
    "Supplier Push": 120,
    "PO Push": 120,
    "PO Status Push": 60,
    "SO Push": 120,
    "B2B Invoice Push": 300,
    "Order Pull": 180,
    "GRN Pull": 180,
    "Return Pull": 180,
    "Inventory Pull": 1500,
    "Master Sync Bulk": 3600,
    "Replay Plan Step": 300,
    "Field Mapping Compile": 30,
    "Schema Snapshot Compute": 600,
    "Mapping Coverage Compute": 600,
    "Morning Brief Compute": 600,
    "SLA Breach Compute": 60,
    "Configuration Audit Write": 30,
}

# Per-job-type max retry attempts per §6.3.8 (default 5; some job types may
# override to bound their retry budget — e.g. webhook processing should fail
# fast and surface, not retry 10 times).
MAX_ATTEMPTS_FOR_JOB_TYPE: dict[str, int] = {
    "Webhook Process": 5,
    "Item Push": 5,
    "Customer Push": 5,
    "Supplier Push": 5,
    "PO Push": 5,
    "PO Status Push": 5,
    "SO Push": 5,
    "B2B Invoice Push": 5,
    "Order Pull": 5,
    "GRN Pull": 5,
    "Return Pull": 5,
    "Inventory Pull": 3,  # long, expensive — fewer retries
    "Master Sync Bulk": 3,
    "Replay Plan Step": 5,
    "Field Mapping Compile": 3,
    "Schema Snapshot Compute": 3,
    "Mapping Coverage Compute": 3,
    "Morning Brief Compute": 3,
    "SLA Breach Compute": 5,
    "Configuration Audit Write": 5,
}

DEFAULT_MAX_ATTEMPTS: int = 5


def queue_for(job_type: str) -> str:
    if job_type not in QUEUE_FOR_JOB_TYPE:
        raise ValueError(
            f"Unknown job_type {job_type!r}. Register it in QUEUE_FOR_JOB_TYPE first."
        )
    return QUEUE_FOR_JOB_TYPE[job_type]


def timeout_for(job_type: str) -> int:
    return TIMEOUT_FOR_JOB_TYPE.get(job_type, 300)


def max_attempts_for(job_type: str) -> int:
    return MAX_ATTEMPTS_FOR_JOB_TYPE.get(job_type, DEFAULT_MAX_ATTEMPTS)
