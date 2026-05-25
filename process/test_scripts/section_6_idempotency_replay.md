# Section 6 — Idempotency, Replay, Correlation & Queue — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the safety machinery behind principles B.2/B.3/B.9: deterministic idempotency keys, correlation IDs threaded across a logical operation, the background Queue (retry with back-off; Success/Partial/Failed job states), and the FDE replay/retry surfaces (Cursor Rewind, Retry, Cancel). Derived from `docs/SPEC.md` §6 and the §6 build packet.

> **First time running these?** Read `HOW_TO_RUN_FDE_TESTS.md` (same folder) first. Read the foundation primer (`../primers/FDE_PRIMER_sections_1_to_7.md`, Part B) for *why* these behave as they do.

> **Important — what "good" looks like here.** This section is the safety net, so several tests deliberately *cause trouble* and confirm the system handles it well. A retry happening, a job reporting Partial, a failure escalating to the dashboard — these are the system **working**, not failing. Read each Expected cell carefully before deciding Pass/Fail. The recurring theme: **doing the same thing twice must never create two financial documents** (B.2).

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` app installed on staging, migrated clean
- [ ] System Manager access; plus a user with **EasyEcom FDE** role but not System Manager (for the surface/permission spot-checks)
- [ ] An EasyEcom Account configured and Test Connection successful (so calls can be made)
- [ ] A way to reach the EasyEcom sandbox; or the ability to point a call at an unreachable host to force failure (the tester guide explains how)
- [ ] Note: §6 is foundation machinery — some tests exercise it through whatever flow is available (e.g. the 8a location pull, or a queued job). Where a step needs "a sync to run," use the simplest available one.

### Steps — Idempotency keys (do it twice → one result)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| I1 | Trigger the same pull/sync operation twice in succession (e.g. run the location discovery pull twice; or re-queue the same push) | The second run does **not** create duplicate records — it recognises the work as already done (updates in place / no-ops), never a second copy | ☐ P ☐ F |
| I2 | Inspect the idempotency key on the resulting Sync Record / Queue Job | A deterministic key is present (derived from the operation + business identifier, e.g. the document name), not a random per-run value | ☐ P ☐ F |
| I3 | Force the same webhook event to be delivered twice (replay the same payload with the same event id) | The second delivery is recognised as a duplicate and not processed again — one Webhook Event effect, not two | ☐ P ☐ F |
| I4 | Confirm a re-run with the SAME key short-circuits with an AlreadySynced-style outcome | The Sync Record shows the already-synced disposition rather than re-doing the work or erroring | ☐ P ☐ F |

### Steps — Correlation (one operation, one thread through the logs)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| X1 | Trigger one logical operation that makes >1 API call (e.g. a pull that paginates, or token-then-call) | All the API Call rows for that operation share **one correlation_id** | ☐ P ☐ F |
| X2 | Open the Sync Record (or Queue Job) for that operation | It carries the same correlation_id, so you can pivot from the record to all its API Call rows | ☐ P ☐ F |
| X3 | Filter the API Call list by that correlation_id | You see the full set of calls that made up the one operation, in order | ☐ P ☐ F |

### Steps — The Queue (background work, Success / Partial / Failed)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| Q1 | Trigger an operation that enqueues background work; open the EasyEcom Queue Job list | A Queue Job row is created; status moves Pending → Running → a terminal state | ☐ P ☐ F |
| Q2 | A job where every record succeeds | Terminal status **Success**; succeeded_count = total, failed_count = 0 | ☐ P ☐ F |
| Q3 | A job where every record fails (point it at an unreachable host, or feed all-bad records) | Terminal status **Failed**; failed_count = total | ☐ P ☐ F |
| Q4 | A job where some records succeed and some fail (mixed batch) | Terminal status **Partial**, with succeeded_count and failed_count both > 0 and summing to the total | ☐ P ☐ F |
| Q5 | Confirm the good records in the Q4 mixed batch actually persisted | The succeeded records are committed; the failed ones are not — one bad record did **not** roll back the good ones (per-record isolation, §7.1) | ☐ P ☐ F |

### Steps — Retry & back-off (failure is expected and recoverable, B.9)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| Y1 | Cause a transient failure (429 or 5xx or connection error) on a queued push | The job retries automatically with a back-off delay (not an immediate hammer); the API Call log shows the retry attempts | ☐ P ☐ F |
| Y2 | Observe the retry spacing across attempts | Delays grow (exponential back-off) and aren't all identical (jitter) — not a fixed-interval tight loop | ☐ P ☐ F |
| Y3 | Let a failure persist past the retry cap | Retries stop at the cap (don't loop forever); the job lands Failed and the failure is surfaced (dashboard / failed state), not swallowed | ☐ P ☐ F |
| Y4 | Use the **Retry** action on a Failed Sync Record / Queue Job | It returns to Pending and re-runs; the attempts counter is preserved (you can see it was retried, not reset to a fresh record) | ☐ P ☐ F |
| Y5 | Use the **Cancel** action on a retriable/pending item | It stops cleanly and lands in a Cancelled terminal state; no further retries fire | ☐ P ☐ F |

### Steps — Replay & Cursor Rewind (B.3)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| W1 | Find the **Cursor Rewind** surface (on the EasyEcom Location / Sync Cursor — wherever the pull cursor lives) | You can set the cursor back to an earlier point | ☐ P ☐ F |
| W2 | Rewind a cursor and re-run the pull | The pull re-fetches from the earlier point; because of idempotency (block I), the re-pulled records do **not** duplicate — they reconcile/no-op | ☐ P ☐ F |
| W3 | Confirm a replay either reproduces the same outcome or explains why it can't | No silent divergence: a replay that can't reproduce gives a clear reason / discrepancy, not a quiet different result | ☐ P ☐ F |

### Steps — ERPNext is never blocked by EasyEcom being down (B.4)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| B1 | With EasyEcom unreachable (or the push set to fail), perform an action that would push to EasyEcom | The ERPNext-side action completes / submits without hanging; the push is **queued** for background retry rather than blocking the UI | ☐ P ☐ F |
| B2 | Bring EasyEcom back; let the queue drain (or Retry) | The queued push then succeeds on retry; no data was lost while EasyEcom was down | ☐ P ☐ F |

> **Known deferred (do NOT raise as failures):** the richer alerting/escalation surfaces depend on §17–§19 (Operational Surface / Alerts), not built — for now a Failed job landing in its terminal state + visible in the list is sufficient. Any Configuration Audit linkage depends on §28 (not built).

### Overall result
- [ ] **PASS** — every step passed (deferred items excluded)
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number, the Expected cell, what you saw, and a screenshot. Remember the §6 theme: a retry, a Partial job, or an escalated failure is usually the system **working** — only raise a failure when the *Expected* behaviour didn't happen (e.g. a duplicate was created, a retry looped forever, or a bad record rolled back good ones).
