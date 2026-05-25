# Section 7 — The Integration Contract — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Section 7 is the *contract* every flow obeys — mostly enforced centrally in code rather than being a feature with its own screen. This script covers the parts you can **observe**: that logging can't be bypassed, that a per-record outcome is strictly **binary (Success / Failed)**, that a line discrepancy fails the whole record and rolls back, the Sync Record Line child table, and foundational-call scoping. Derived from `docs/SPEC.md` §7 and the §7 packet.

> **First time running these?** Read `HOW_TO_RUN_FDE_TESTS.md` first, and the foundation primer (`../primers/FDE_PRIMER_sections_1_to_7.md`, Part C.5) for the contract's intent.

> **The one idea to hold onto.** A single Sync Record is **binary**: Success or Failed — there is no "partly done", no "completed with discrepancy". If *any* line in a record has a problem (whether it blocks creation, or it's a reconciliation variance), the **whole record is Failed** and any document it would have created is **rolled back** — the books never hold a document the integration considers failed. So **seeing Failed-on-discrepancy is correct**, not a bug. The thing that WOULD be a bug: a record reporting Success while a line is visibly unreconciled. (Note: the *Queue Job* can be Partial — that's a batch of many records — but each individual record within it is cleanly Success or Failed. Don't confuse the two levels.)

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` app installed on staging, migrated clean
- [ ] System Manager access; plus an **EasyEcom FDE** (non-System-Manager) user
- [ ] An EasyEcom Account configured; Test Connection successful
- [ ] Note: §7 is enforced through whatever flows exist. Where a step needs "a sync", use the simplest available (e.g. the 8a location pull for foundational-scoping; a queued push for the state machine). Some line-level steps will be fully exercisable only once a nested-document flow (GRN §9) exists — those are marked **(fuller test at §9)**.

### Steps — Central logging cannot be bypassed (§7.2)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| L1 | Trigger any operation that calls EasyEcom; open the EasyEcom API Call list | Every outbound call produced an API Call row — endpoint, status, latency, request/response captured | ☐ P ☐ F |
| L2 | Inspect a logged call's payload | Credentials / tokens are **redacted** (e.g. `***REDACTED***`), not stored in clear | ☐ P ☐ F |
| L3 | Confirm the log row carries correlation + (where applicable) company | correlation_id present; company set for operational calls (blank only for foundational — see block F) | ☐ P ☐ F |
| L4 | Trigger an inbound webhook (if a webhook path is available) | A Webhook Event row is recorded **before** processing — the receipt is logged even if processing later fails | ☐ P ☐ F |

### Steps — Binary per-record state machine (§7.3)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| M1 | Open the EasyEcom Sync Record DocType; inspect the status field's allowed values | Exactly: Pending, Running, Success, Failed, Cancelled, AlreadySynced — **no Partial, no Discrepancy, no "completed with discrepancy"** | ☐ P ☐ F |
| M2 | Run an operation that fully succeeds | Sync Record lands **Success** | ☐ P ☐ F |
| M3 | Run an operation that fails outright (e.g. unreachable, or a blocking error) | Sync Record lands **Failed**; no half-created document left behind | ☐ P ☐ F |
| M4 | Take a **Failed** record and use **Retry** | Returns to Pending and re-runs; attempts counter preserved (§6 Y4 overlaps here) | ☐ P ☐ F |
| M5 | Attempt (e.g. via a script console, or ask the developer to confirm by test) to set a Sync Record status to "Partial" | Rejected — the binary model is enforced at persistence, not just convention. (If you can't test directly, confirm the §7 test suite asserts this — there is a test that inserting status="Partial" is rejected.) | ☐ P ☐ F |

### Steps — Discrepancy fails the whole record + rollback (§7.1.1) **(fuller test at §9)**

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| D1 | (When a nested flow exists, e.g. GRN §9) Run a multi-line sync where one line has a blocking problem (e.g. an unmapped SKU) | The **whole** Sync Record is Failed; the target document is **not** created (no partial document in the books) | ☐ P ☐ F |
| D2 | Run a multi-line sync where one line has a reconciliation variance (e.g. a quantity/tax mismatch beyond tolerance) | Still **Failed** (not a separate "discrepancy-but-done" status); any document that would have been created is **rolled back**; an Integration Discrepancy is raised for the offending line | ☐ P ☐ F |
| D3 | Confirm the failure names the offending line | You can tell *which* line caused it (not just "the record failed") — see the child table, block N | ☐ P ☐ F |
| D4 | Confirm no Success-with-hidden-discrepancy is possible | There is no state where the record says Success while a line is unreconciled. If you ever see one, that's the bug this contract exists to prevent | ☐ P ☐ F |

### Steps — Sync Record Line child table (§7.1.1, §31.2.3)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| N1 | Open a Sync Record; locate the Sync Record Lines child table | The child table exists on the Sync Record (it may be empty for single-entity syncs like masters — that's correct; it's populated by nested-document flows from §9 on) | ☐ P ☐ F |
| N2 | Inspect the child line fields | source_line_ref, target_field, line_status (OK / Failed / Discrepancy), reason, and a link to an Integration Discrepancy | ☐ P ☐ F |
| N3 | (When a nested flow exists) On a failed multi-line record, read the lines | Each line shows its outcome; the problem line is marked Failed or Discrepancy with a plain-English reason; OK lines are marked OK | ☐ P ☐ F |

### Steps — Foundational-call scoping (§7.7)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| F1 | Run a foundational call — Test Connection, or the location discovery pull (`/getAllLocation`) | It works and is logged as an API Call | ☐ P ☐ F |
| F2 | Inspect that API Call row | `is_foundational = 1` and **company is blank** — foundational calls are account-scoped, not tied to a Company | ☐ P ☐ F |
| F3 | Confirm a foundational call produced **no** Sync Record | There is no per-company Sync Record for the token / connection-test / location-discovery call — they're account-scoped by design | ☐ P ☐ F |

### Steps — Per-record isolation (§7.1) — overlaps §6 Q5

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| P1 | Run a batch where one record raises mid-way (mixed good/bad batch) | The good records still commit; the one bad record is recorded Failed; the batch is not aborted by the single failure | ☐ P ☐ F |

> **Known deferred (do NOT raise as failures):** the line-level discrepancy steps (block D, N3) are only **fully** exercisable once a nested-document flow exists (GRN, §9) — until then, confirm the child table + binary status structurally (M1, N1, N2) and revisit D/N3 at §9. Integration Discrepancy surfacing/severity routing depends on §17–§23 (not all built). Configuration Audit depends on §28.

### Overall result
- [ ] **PASS** — every applicable step passed (deferred / §9-gated items excluded)
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template. Hold onto the §7 theme: Failed-on-discrepancy with a rollback is the contract **working**; the real defect is a Success that hides an unreconciled line, an unlogged call, or a foundational call that created a per-company Sync Record.
