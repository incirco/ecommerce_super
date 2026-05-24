# Playbook: Writing an Integration Test

**When to use this:** You're testing a flow, a Field Mapping ruleset, a webhook handler, or any code that touches the EE API surface.

**Pre-flight:** Read `SPEC.md` Section 15 (Test Strategy). The test pyramid is specified there.

---

## The three tiers

| Tier | Path | What it tests | Speed | Hits EE? |
| --- | --- | --- | --- | --- |
| Unit | `tests/unit/` | Pure functions, single-class behaviour | <100ms each | No |
| Integration | `tests/integration/` | Flows end-to-end with mocked EE | 1-10s each | EE mock |
| Contract | `tests/contract/` | EE request/response shape we code against | <100ms each | Recorded fixtures |
| E2E | `tests/e2e/` | Multi-flow scenarios | 10-60s each | EE mock |

If you're not sure which tier, think about: "what is the smallest piece of behaviour I'm testing?" Pure-function = unit. Single flow involving EE = integration. Cross-flow scenario = e2e.

---

## The EE mock server

Located at `tests/ee_mock/server.py`. Provides a Flask-based HTTP server that responds to EE-shaped requests with configurable responses. Use it like:

```python
from ecommerce_super.tests.ee_mock import ee_mock_server

class TestItemPushFlow(FrappeTestCase):
    def setUp(self):
        self.mock = ee_mock_server()
        # By default, the mock returns a happy-path response from a fixture.
        # Override per-test as needed.

    def test_item_push_succeeds_first_attempt(self):
        item = make_test_item(item_code="TEST-1")
        # Trigger the flow
        sync_item_to_ee(item, company="_Test Company")
        # Drain the queue
        drain_queue_jobs(company="_Test Company")
        # Assert
        sr = get_sync_record(item)
        self.assertEqual(sr.sync_status, "Success")
        self.assertEqual(self.mock.call_count("/Inventory/UpdateInventoryV2"), 1)
```

### Mocking specific responses

```python
self.mock.set_response(
    method="POST",
    path="/Inventory/UpdateInventoryV2",
    status=422,
    body={"error": {"code": "HSN_NOT_FOUND", "message": "HSN code 8517.62 not present in Tax Master"}},
)
```

### Mocking sequence (retry behaviour)

```python
self.mock.set_response_sequence(
    method="POST",
    path="/Inventory/UpdateInventoryV2",
    responses=[
        {"status": 500, "body": {"error": "Server error"}},
        {"status": 500, "body": {"error": "Server error"}},
        {"status": 200, "body": {"ee_company_product_id": "12345"}},
    ],
)
# Test that after 2 failures and 1 success, the Sync Record is Success and attempts == 3.
```

---

## Test data factories

Located at `tests/factories/`. Use them; do not hand-craft test data inline.

```python
from ecommerce_super.tests.factories import (
    make_test_company, make_test_item, make_test_customer,
    make_test_purchase_order, make_test_easyecom_settings,
)

# Each factory creates a minimum-valid instance and returns the saved doc.
# Override fields as needed:
item = make_test_item(item_code="TEST-1", has_batch_no=1)
```

Why factories? Because spec changes routinely add required fields. A factory updates centrally; inline test data breaks scattered.

---

## Test fixtures

Located at `tests/fixtures/`. Per-test JSON snapshots of EE-side payloads, used both for setup and for asserting expected output:

```
tests/fixtures/
├── ee_payloads/
│   ├── item_push_request.json     # what we send
│   ├── item_push_response.json    # what we expect EE to return
│   ├── manifest_webhook.json      # an inbound webhook sample
│   └── grn_pull_response.json
└── erpnext_docs/
    ├── test_item.json
    └── test_purchase_order.json
```

When you implement a new flow, capture a real EE-shaped payload (anonymised) as a fixture. Future tests use it.

---

## What to test (the minimum bar)

For a flow:

1. **Happy path** — input valid, EE responds 200, output correct
2. **Retry path** — EE returns 5xx the first N attempts, then 200; verify final state is Success with attempts > 1
3. **Permanent failure path** — EE returns 422 with a known error; verify Sync Record is Failed, the right exception class was raised, and the error translation library matched the error
4. **Idempotency path** — call the flow twice with the same input; verify EE was called once (or twice with the same idempotency key, depending on the flow's deduplication strategy)
5. **Concurrency path** — two workers attempt the same flow concurrently; verify the lock prevents double-processing
6. **Field Mapping path** — a missing required field causes the right exception; a transform produces the right output

For a Field Mapping ruleset:

1. **Round-trip path** — push then pull produces the original (for Bidirectional rulesets)
2. **Identity defaults** — fields not in the rules-listed paths but present in source pass through (Permissive mode)
3. **Strict mode** — a strict ruleset rejects unmapped fields
4. **Conditional rules** — a rule with `condition` fires only when the condition is true
5. **Computed fields** — a computed field's expression evaluates correctly

For a webhook handler:

1. **Signature verification** — invalid signature returns 401
2. **Dedup** — duplicate webhook returns 200 but doesn't process twice
3. **Stale timestamp** — webhook older than `webhook_max_age_seconds` returns 401
4. **Disabled** — when `webhook_enabled=0`, returns 503
5. **Processing happy path** — valid webhook spawns the right Sync Records and downstream documents
6. **Processing failure** — a processing error doesn't lose the webhook; it's still on the Webhook Event for replay

---

## Anti-patterns

### Test that asserts on logs

```python
# BAD
self.assertIn("Pushing item to EE", log_output)
```

Logs are not contracts. The behavior under test is the side effect (Sync Record state, EE call count, etc.), not the log.

### Test that calls EE directly

```python
# BAD — hits the real EE API!
client = EasyEcomClient(company="...", location_key="...")
response = client.push_item(...)
```

If you're tempted to call the real EE API in a test, you're probably writing a contract test that should use a recorded fixture. Or you're trying to validate against the live API, in which case it's not an automated test — it's a manual verification.

### Test that disables the feature it tests

```python
# BAD
def test_idempotency(self):
    settings.webhook_dedup_window_minutes = 0  # disable dedup
    ...
```

If the test only passes by disabling the feature, the test isn't testing the feature.

### Test that depends on test ordering

Each test must work in isolation. `setUp` and `tearDown` reset state. Tests that depend on a sibling test's setup are fragile.

### Asserting against datetime.now()

Use `frappe.utils.now_datetime()` in production code, then in tests use `freezegun` to freeze time:

```python
from freezegun import freeze_time

@freeze_time("2026-05-05 10:00:00")
def test_cursor_advances(self):
    ...
```

---

## When you're done

- Tests cover all six paths from the "minimum bar" section
- Tests pass on a clean migration
- Test names describe the assertion, not the setup: `test_push_succeeds_after_retry` not `test_push_with_500_then_200`
- No test calls real EE
- No test disables the feature it's testing
- Coverage on the flow's module is >85%

In your chat response, list the test files added, the count per tier, and any path you couldn't test (with explanation).
