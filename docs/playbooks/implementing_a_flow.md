# Playbook: Implementing a Flow

**When to use this:** You're building one of the ten operational flows specified in `SPEC.md` Sections 4-9. Examples: B2C SI creation from manifest, GRN ingestion to Purchase Receipt, B2B Sales Order push.

**Pre-flight check:** This is a multi-day task. The pre-flight is non-negotiable.

- Read `SPEC.md` Section X (the relevant flow section) end-to-end. Don't skim.
- Confirm the dependencies: Master Sync (Section 4), Field Mapping engine (Sections 4.0 and 20), three log DocTypes (Section 10.1.2), Queue Job (Section 11.3), EasyEcomClient (Appendix B / Section 30.4) are all implemented before you start the flow.
- Read this playbook fully.
- Make a numbered plan in chat and get user confirmation before writing code.

---

## The structure of every flow

Every flow follows the same skeleton:

```
flows/<flow_name>.py

  # Pull side (if applicable)
  def poll_<resource>(company):
      """Cron-driven (registered in hooks.py scheduler_events).
      Reads cursor, calls EE, processes results, advances cursor.
      For per-Company crons, this iterates over all enabled Companies."""

  # Push side (if applicable)
  def on_<doctype>_<event>(doc, method):
      """Frappe document_event hook. Calls enqueue_easyecom_job() to
      schedule async work via Frappe's RQ queue. Does NOT make EE calls
      directly inside the document save transaction."""

  # Webhook side (if applicable)
  # The webhook receiver in api/webhook.py persists the Webhook Event
  # and calls enqueue_easyecom_job(job_type="Webhook Process", ...).
  # The handler below is registered in JOB_TYPE_HANDLERS in queue/workers.py:
  def process_<event_type>_webhook_handler(queue_job):
      """Job handler. Reads parent_event Webhook Event, applies Field Mapping,
      creates Frappe docs."""

  # Job-type handlers (registered in JOB_TYPE_HANDLERS)
  def push_<entity>_handler(queue_job):
      """Job handler for <entity> Push jobs. Calls EE via EasyEcomClient,
      updates Sync Record. State machine and retry are handled by execute_job
      (queue/workers.py); this function just does the work."""

  def _ingest_<entity>_from_ee(payload, correlation_id):
      """Translate a single EE-side entity into ERPNext docs.
      Pure function — no Queue Job interaction. Called from polling and
      from webhook handlers."""
```

Flows do NOT contain:
- Direct HTTP calls to EE — always via `EasyEcomClient`
- Direct field-by-field translations — always via Field Mapping rulesets
- Direct DB queries that bypass Frappe ORM (except read-only reporting queries with explicit reasoning)
- Email/Slack calls — those go through `alerts/router.py`
- Direct calls to `frappe.enqueue` for EasyEcom work — always via the `enqueue_easyecom_job` facade so the Queue Job tracking row is created
- Custom retry loops — `execute_job` (in `queue/workers.py`) owns retry policy

---

## The 10-step workflow

### 1. Read the flow's spec section end-to-end

The spec section lays out:
- Direction of truth for every field involved
- Idempotency strategy
- Replay strategy
- Error handling per failure mode
- Side effects (DocTypes created/updated)
- Reconciliation back-references that must be set

If any of this is unclear, **stop and ask** before writing code.

### 2. Identify the Field Mapping rulesets the flow uses

Every flow uses one or more rulesets from `apps/ecommerce_super/ecommerce_super/fixtures/easyecom_field_mapping.json`. List them. If a needed ruleset doesn't exist, follow the Field Mapping playbook to create it before continuing.

### 3. Identify the Queue Job types

Most flows use one or more Queue Job `job_type` values. Check `SPEC.md` Appendix A.2.5 / Section 30.2.6 for the canonical list. If you need a new `job_type`, add it to the Select options on the Queue Job DocType (small spec change — surface to user).

### 4. Identify the failure modes the flow must handle

Read the spec section for "failure modes" or "edge cases." Each failure mode produces a typed exception (per `SPEC.md` Appendix C / Section 30.5). Map each failure mode to the right exception. Examples:

| Failure mode | Exception | Retry policy |
| --- | --- | --- |
| EE returns 5xx | `EasyEcomServerError` | transient (retried by Queue Job) |
| EE returns 401 | `EasyEcomAuthError` | permanent (after one re-auth) |
| EE returns 422 with HSN missing | `EasyEcomMissingDependency` | manual (FDE adds HSN, retries) |
| EE returns 409 duplicate | `EasyEcomDuplicateError` | manual (FDE Mark As Already Synced) |
| Source ERPNext doc has bad data | `FieldMappingMissingRequired` | permanent |
| Network timeout | `EasyEcomTimeoutError` | transient |

### 5. Implement the pull side (if applicable)

There are two patterns depending on volume. **Pattern A** (low-volume polls) does the work inline in the cron handler. **Pattern B** (higher-volume polls) has the cron handler enqueue per-Company jobs which run on RQ workers. Pattern B is the recommended default; Pattern A is for tiny polls (e.g., locations sync) where the overhead of enqueuing isn't justified.

**Pattern B example (recommended):**

```python
# flows/b2c_sales.py

import frappe
from ecommerce_super.api.client import EasyEcomClient
from ecommerce_super.field_mapping.runner import apply_ruleset
from ecommerce_super.easyecom.queue import enqueue_easyecom_job
from ecommerce_super.utils.correlation import new_correlation_id
from ecommerce_super.exceptions import (
    EasyEcomError, EasyEcomTimeoutError, FieldMappingError,
)


def poll_orders():
    """Cron entry point. Iterates Companies and enqueues per-Company pull jobs.
    Registered in hooks.py scheduler_events at */5 * * * *."""
    enabled_companies = frappe.get_all(
        "EasyEcom Settings",
        filters={"enabled": 1, "sync_enabled_orders": 1},
        pluck="company",
    )
    for company in enabled_companies:
        enqueue_easyecom_job(
            job_type="Order Pull",
            company=company,
            correlation_id=new_correlation_id(),
        )
    # Cron is intentionally lightweight — the actual work happens in pull_orders_handler


def pull_orders_handler(queue_job):
    """Job handler for Order Pull. Registered in JOB_TYPE_HANDLERS.
    Reads cursor, fetches orders, ingests each, advances cursor."""
    company = queue_job.company
    settings = frappe.get_cached_doc("EasyEcom Settings", company)

    cursor = frappe.get_doc(
        "EasyEcom Sync Cursor",
        {"company": company, "resource": "Orders"}
    )
    # No need for in_progress lock — RQ ensures one worker per (queue, job_name).
    # Per-Company concurrency is enforced by company_concurrency_semaphore in execute_job.

    client = EasyEcomClient(company=company, location_key=settings.default_location_key)
    ruleset = frappe.get_cached_doc("EasyEcom Field Mapping", "EasyEcom-Order-Pull")

    new_cursor = cursor.cursor_value
    for order in client.fetch_orders(since=cursor.cursor_value):
        correlation_id = new_correlation_id()
        try:
            _ingest_order(order, ruleset, correlation_id, company)
            new_cursor = max(new_cursor, order.modified_at)
        except FieldMappingError as e:
            _record_ingestion_failure(order, e, correlation_id, company)
        except EasyEcomError as e:
            _record_ingestion_failure(order, e, correlation_id, company)
            # Continue with next order; don't abort the batch

    cursor.db_set({
        "cursor_value": new_cursor,
        "last_pull_at": frappe.utils.now_datetime(),
    })
```

Notes:
- The cron just enqueues per-Company jobs; it's lightweight and reliable
- Each Company's pull is a separate Queue Job, separately tracked, separately retryable
- A failure on one Company's pull doesn't affect other Companies
- A failure on one order within a pull doesn't abort the batch
- Each order gets its own `correlation_id`
- Sync Records get created via `_ingest_order` or `_record_ingestion_failure`

### 6. Implement the push side (if applicable)

```python
from ecommerce_super.easyecom.queue import enqueue_easyecom_job


def on_purchase_order_submit(doc, method):
    """Hook called by Frappe on PO submit. Enqueues async work via the queue facade."""
    settings = frappe.get_cached_doc("EasyEcom Settings", doc.company)
    if not settings.enabled:
        return

    correlation_id = new_correlation_id()
    doc.db_set("ecs_correlation_id", correlation_id, update_modified=False)

    # The facade creates an EasyEcom Queue Job tracking row AND calls frappe.enqueue
    # under the hood. Routes to the right Frappe queue tier ("default" for PO Push;
    # see SPEC.md Section 11.3.2) and sets the idempotency_key per Section 11.1.
    enqueue_easyecom_job(
        job_type="PO Push",
        company=doc.company,
        target_doctype="Purchase Order",
        target_name=doc.name,
        correlation_id=correlation_id,
        priority=5,
    )


def push_po_handler(queue_job):
    """Job-type handler. Registered in JOB_TYPE_HANDLERS in queue/workers.py.
    Called by execute_job(), not directly. State machine and retry policy
    are handled by execute_job; this function just does the work."""
    po = frappe.get_doc("Purchase Order", queue_job.target_name)
    settings = frappe.get_cached_doc("EasyEcom Settings", po.company)
    client = EasyEcomClient(
        company=po.company,
        location_key=po.ecs_ee_location_key or settings.default_location_key,
    )

    ruleset = frappe.get_cached_doc("EasyEcom Field Mapping", "EasyEcom-PO-Push")
    payload = apply_ruleset(ruleset, po, direction="push")

    sync_record = _get_or_create_sync_record(
        company=po.company,
        direction="Push to EE",
        entity_type="Purchase Order",
        erpnext_doctype="Purchase Order",
        erpnext_name=po.name,
        correlation_id=queue_job.correlation_id,
    )
    sync_record.db_set({"sync_status": "In Progress",
                        "attempts": sync_record.attempts + 1})

    try:
        response = client.push_purchase_order(payload, idempotency_key=queue_job.idempotency_key)
        sync_record.db_set({
            "sync_status": "Success",
            "ee_entity_id": response["ee_po_id"],
            "last_success_at": frappe.utils.now_datetime(),
        })
        po.db_set("ecs_easyecom_po_id", response["ee_po_id"], update_modified=False)
    except EasyEcomError as e:
        sync_record.db_set({
            "sync_status": "Failed",
            "last_failure_at": frappe.utils.now_datetime(),
            "last_error": str(e),
        })
        # The translation lookup happens in the Sync Record's on_update hook
        raise  # let execute_job classify and retry/fail per the exception's retry_policy
```

Notes:
- The handler does NOT manage its own retry — `execute_job` (the worker entry point in `queue/workers.py`) catches the exception, looks up the exception class's `retry_policy` (per `SPEC.md` Appendix C / Section 30.5), and re-enqueues via `frappe.enqueue` with the right back-off OR marks the Queue Job Failed.
- `enqueue_easyecom_job` creates an `EasyEcom Queue Job` DocType row AND calls `frappe.enqueue(method=execute_job, job_name=qj.name, ...)`. The two are paired.
- The Frappe queue tier (`short`/`default`/`long`) is determined automatically from the `job_type` via `QUEUE_FOR_JOB_TYPE` in `queue/routing.py`. Don't override it in the handler.
- Idempotency key formula per `SPEC.md` Section 11.1; the facade computes it.
- Sync Record is *updated in place* (entity-centric — see `SPEC.md` Section 10.1.2). Same SR across retries.
- API Call records the HTTP detail (call-centric, append-only). Created automatically by `client._request()`.
- `bench show-pending-jobs` works as expected — the integration's jobs appear there alongside other Frappe jobs.

### 7. Implement the webhook side (if applicable)

Webhook handling has two phases:
- Receipt (in `api/webhook.py`) — fast: verify, dedupe, persist, enqueue, return 200
- Processing (in `flows/<flow_name>.py`) — does the actual work

```python
def process_manifest_webhook(webhook_event):
    """Worker for manifest webhooks. Creates Sales Invoice."""
    payload = frappe.parse_json(webhook_event.raw_payload)
    correlation_id = webhook_event.correlation_id

    ruleset = frappe.get_doc("EasyEcom Field Mapping", "EasyEcom-Manifest-Pull")

    # Idempotency at persistence: SI on (ecs_easyecom_event_id, company)
    existing = frappe.db.exists("Sales Invoice", {
        "ecs_easyecom_event_id": webhook_event.ee_event_id,
        "company": webhook_event.company,
    })
    if existing:
        webhook_event.processing_status = "Duplicate"
        webhook_event.save(ignore_permissions=True)
        return

    si_data = apply_ruleset(ruleset, payload, direction="pull")
    si_data["ecs_correlation_id"] = correlation_id
    si_data["ecs_easyecom_event_id"] = webhook_event.ee_event_id
    si_data["company"] = webhook_event.company

    si = frappe.get_doc({"doctype": "Sales Invoice", **si_data})
    si.insert(ignore_permissions=True)
    si.submit()

    webhook_event.processing_status = "Processed"
    webhook_event.spawned_sync_records = [...]  # links to relevant Sync Records
    webhook_event.downstream_documents = [{"doctype": "Sales Invoice", "name": si.name}]
    webhook_event.save(ignore_permissions=True)
    frappe.db.commit()
```

### 8. Register the hooks

In `apps/ecommerce_super/ecommerce_super/hooks.py`:

```python
doc_events = {
    "Purchase Order": {
        "on_submit": "ecommerce_super.flows.buying.on_purchase_order_submit",
    },
    ...
}

scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "ecommerce_super.flows.b2c_sales.poll_orders_for_all_companies",
        ],
        ...
    }
}
```

Note: cron jobs typically iterate Companies internally rather than registering per-Company crons. The cron entry calls a function that loops Companies with `enabled=1`.

### 9. Write tests at three levels

**Unit tests** for any pure function (e.g., `_compute_po_idempotency_key`):

```python
class TestPOIdempotency(FrappeTestCase):
    def test_idempotency_key_deterministic(self):
        key1 = _compute_po_idempotency_key(po_doc)
        key2 = _compute_po_idempotency_key(po_doc)
        self.assertEqual(key1, key2)

    def test_idempotency_key_changes_on_doc_change(self):
        ...
```

**Integration tests** for the full flow against the EE mock server (`tests/ee_mock/`):

```python
class TestPOPushFlow(FrappeTestCase):
    def setUp(self):
        # Start EE mock with predefined responses
        self.mock = ee_mock_server()

    def test_po_push_creates_sync_record_and_succeeds(self):
        po = make_test_po(...)
        po.submit()
        # Wait for queue job to drain
        run_pending_queue_jobs()

        sr = frappe.get_doc("EasyEcom Sync Record", {
            "erpnext_doctype": "Purchase Order",
            "erpnext_name": po.name,
        })
        self.assertEqual(sr.sync_status, "Success")

    def test_po_push_retries_on_5xx(self):
        self.mock.respond_with(500, attempts=2, then_succeed=True)
        ...

    def test_po_push_marks_failed_on_validation_error(self):
        self.mock.respond_with(422, body={"error": "HSN missing"})
        ...
```

**End-to-end tests** for full multi-flow scenarios (`tests/e2e/`).

### 10. Surface for review

Before declaring done:

- All tests pass
- Migration runs clean
- Manual smoke test in the desk: create a PO, submit, observe Queue Job, observe Sync Record, observe API Call
- Confirm the operational surface affordances work: open the PO's Connections panel and verify the Sync Records and API Calls are linked
- The flow handles all the failure modes from step 4 with the right exceptions

Surface to user with:
- The flow name
- Test counts (unit / integration / e2e)
- Any spec ambiguities you resolved (and how)
- Any open questions that remain

---

## Common mistakes

- **Letting a flow synchronously call EE during a Frappe save transaction.** Always enqueue. The only exception is the explicit "Test Connection" button.
- **Catching exceptions to "make the test pass."** If a flow shouldn't raise, write the flow correctly. If it should raise, the test should expect the exception.
- **Bypassing Field Mapping rulesets.** If you're writing `payload['xyz'] = doc.abc`, you're not using the engine. Add a rule to the ruleset.
- **Forgetting to update the Sync Record's hashes.** The hashes (Section 10.1.2 of `SPEC.md`) enable change detection. A flow that doesn't update them defeats the change-detection optimisation.
- **Not propagating correlation_id.** Every record the flow creates or touches must carry the correlation_id. The Event Timeline and Inspector views depend on this.
- **Hardcoded sleeps in retries.** Retry timing is the Queue Job's responsibility, not yours. Raise the right exception with the right `retry_policy`; let the dispatcher handle timing.

---

## When you're done

The flow is implementation-complete when:

- All four phases are wired (pull, push, webhook, worker as applicable)
- All Field Mapping rulesets it uses exist and are tested
- All hooks are registered in hooks.py
- The three logs (Sync Record, API Call, Webhook Event) are populated correctly
- Idempotency keys are deterministic
- All failure modes from the spec section produce the right exception class
- Tests at all three levels pass
- A manual end-to-end run in the desk works
