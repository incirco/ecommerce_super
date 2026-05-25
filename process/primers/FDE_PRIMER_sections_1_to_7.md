# FDE Primer — Understanding the Integration (Sections 1–7)

**Who this is for:** a Forward-Deployed Engineer joining the project cold. You know ERPNext. You know nothing yet about this app or EasyEcom. By the end of this primer you will understand *what* has been built so far, *why* it behaves the way it does, and *how* the pieces fit — enough to read the per-section test scripts and verify the build with comprehension instead of rote.

**This is not a test script and not the spec.** It is the mental model. Read it once, start to finish. Then go to the test scripts in `../test_scripts/` (`HOW_TO_RUN_FDE_TESTS.md` first, then the per-section script). The full detail lives in `docs/EasyEcom_Integration_Specification_v1.2.docx`; you only open that when a test step points you there.

Time to read: about 25 minutes. Worth every minute — the principles in Part B explain almost everything the system does.

---

## Part A — What this product is

### A.1 The one-paragraph version

`ecommerce_super` is an ERPNext-native app that connects an ERPNext site to **EasyEcom** (a marketplace order- and inventory-management platform that SME sellers use to run Amazon/Flipkart/etc. from one place) and reconciles marketplace settlements back to the books. It keeps the two systems aligned — orders, inventory, returns, masters — and, crucially, produces clean financial records an accountant can trust. The product is sold with a human in the loop: an FDE (you) configures and operates each deployment. It is built for ERPNext v16 only.

### A.2 The two systems and who owns what

The single most important idea in the whole product:

- **ERPNext is the books-of-record.** Anything with financial impact — invoices, receipts, credit notes, journal/payment entries — lives in ERPNext as the authoritative copy. The auditor reads ERPNext.
- **EasyEcom is the operations-of-record.** Operational state — order fulfilment status, the goods-receipt process, manifests, marketplace channel state — lives in EasyEcom as the authoritative copy.

When the two disagree, **ERPNext wins financial questions, EasyEcom wins operational questions.** The integration's whole job is to keep them aligned within a tolerable lag, and — when it *can't* align them — to raise a visible flag rather than guess. Hold onto this; half the design follows from it.

### A.3 Why we built our own (instead of using the existing app)

There is an official EasyEcom-ERPNext app already. We chose to build our own because the integration is the foundation the reconciliation engine sits on: every flow has to produce data shaped exactly the way recon needs it (correlation keys, custom fields, idempotency tokens), and our target clients run **multiple companies under one EasyEcom account**, which the existing app treats as a single-company setup. We accept the extra engineering cost because the integration is too central to financial correctness to depend on someone else's release cadence. (You don't need this to test — but when someone asks "why not just use the other one," that's the answer.)

### A.4 Who the client is

SME multi-channel sellers, roughly ₹5–200 crore GMV, selling on 3+ marketplaces, using EasyEcom as their operations hub, hosted on Frappe Cloud (Mumbai), with the `india_compliance` app installed (GST is always in play). Every deployment goes live with an FDE configuring it — never self-serve.

---

## Part B — The nine principles (the part that explains everything)

These are the non-negotiable rules every flow obeys. If you internalize these, the system's behavior stops being a list of facts to memorize and becomes predictable. When a test result surprises you, the explanation is almost always one of these.

**B.1 Books vs operations of record.** (Covered above.) ERPNext = financial truth; EasyEcom = operational truth; diverge → surface a discrepancy, never silently pick a winner.

**B.2 Idempotency is mandatory.** Running the same thing twice must never create two financial documents. Every push to EasyEcom carries a deterministic key (usually the ERPNext document name). Every webhook from EasyEcom is de-duplicated. Every poll checks-before-inserting. *Why you care:* a core test move is "do it twice, confirm one result." If a retry ever creates a duplicate invoice, that's a serious bug — and the system is explicitly built to prevent it.

**B.3 Replay is mandatory.** Every flow can be re-run by the FDE, and a replay either reproduces the same outcome or tells you clearly why it can't. A bad document created from a faulty event can be reversed and the event re-pulled. *Why you care:* "recover from failure by replaying" is a normal operation, not an emergency hack.

**B.4 ERPNext submission is never blocked by EasyEcom being down.** Submitting a Sales Order, PO, Item, etc. in ERPNext always completes even if EasyEcom is unreachable; the push to EasyEcom is queued and retried in the background. The ERPNext UI never hangs waiting on EasyEcom. *Why you care:* if you take EasyEcom offline in a test, ERPNext should keep working and the push should queue — that's correct, not broken. (One opt-in exception exists for B2B synchronous orders, configured per channel.)

**B.5 Multi-company is first-class.** The credential boundary is the **EasyEcom Account** (one per client), *not* the Company. Company identity is resolved *through the location*: each EasyEcom Location says which Frappe Company it belongs to, and several locations can map to the same Company (many-to-one). Every operational record carries a `company` field, filled in by resolving its location. A user who can see Company A's data must not see Company B's. *Why you care:* almost everything keys off location→company resolution. When a record has the wrong (or blank) company, suspect the location mapping first. (The one exception: "foundational" calls — token, location discovery, connection test — are account-scoped and legitimately have a blank company.)

**B.6 Source-of-truth is configurable, not hardcoded.** We don't assume EasyEcom always owns stock or ERPNext always owns customers. A per-warehouse **Source-of-Truth Map** says, for each warehouse, who owns inventory, who originates goods receipts, who originates adjustments, and how B2B reservations are handled. Masters (Item/Customer/Supplier/Tax) have similar configurable direction-of-truth. *Why you care:* "who wins" is a setting you (the FDE) configure per deployment, not a fixed rule — so the same software behaves differently for two clients, by design.

**B.7 No silent data divergence.** If the integration can't reconcile a difference, it does **not** quietly overwrite one side. It creates a visible **Integration Discrepancy** record with a severity (Info / Warning / Error / Critical) and routes it to you with suggested actions. *Why you care:* this is the heart of what makes the product safe for financial work. A test that ends in a clear discrepancy is often a *pass* (the system correctly refused to guess), not a fail.

**B.8 Audit trail is mandatory.** Every API call (both directions) is logged — endpoint, payload with credentials redacted, status, response, latency. Every webhook is recorded before processing. Every document the integration creates carries back-references to the EasyEcom source and the API call that produced it. Logs are kept 90 days minimum. *Why you care:* when something looks wrong, the answer is in the API Call / Webhook Event logs. You will live in these.

**B.9 Failure is expected and recoverable.** The system assumes any call/webhook/job will eventually fail. Retries with exponential back-off and jitter on 429/5xx/connection errors; capped retry counts; persistent failures escalate to your dashboard; every flow has a documented manual-recovery path. *Why you care:* seeing a retry happen, or a failure escalate to a dashboard, is the system working as designed — not a defect.

---

## Part C — What has been built so far (Sections 3–7)

The build proceeds in two parts. **Part II of the spec is "The Foundation"** — the connection, the records, the translation engine, and the contract — built first, before any business flow. Sections 3–7 are that foundation. (Sections 1–2 are the intro and the principles above — nothing to "test," everything to understand.) Here is what each built section is, in plain terms, and what it means for you.

### C.1 Section 3 — Authentication & Connection Model

**What it is:** how the app logs into EasyEcom and stays logged in. The credential boundary is the EasyEcom Account; the app mints a JWT (a time-limited token) per location and caches it, refreshing before it expires. It also sets up the permission model (Company A can't see Company B) and the connection-health surface.

**What it means for you:** this is the first thing you configure on a new deployment — enter the EasyEcom Account credentials, run **Test Connection**, and confirm it acquires a token. If the connection is mis-configured, nothing downstream works, so this is always step one. The "foundational calls" (token, connection test, and — see §8 later — location discovery) are special: they're account-scoped and don't produce per-company records.

**Tested by:** `../test_scripts/foundation_section_3_and_4.md` (combined with §4).

### C.2 Section 4 — Data Model

**What it is:** the DocTypes the integration runs on — the EasyEcom Account, the per-Company settings, and the operational/logging records (Sync Record, API Call, Sync Cursor, Queue Job, Webhook Event). Also the multi-company "dimension" plumbing: how `company` gets onto every operational record by resolving the location.

**What it means for you:** these are the tables you'll inspect constantly. When you test any later flow, you confirm its effects *here* — did a Sync Record get created, did the API Call get logged, is the company correct. Knowing this data model is most of knowing how to verify the system.

**Tested by:** `../test_scripts/foundation_section_3_and_4.md`.

### C.3 Section 5 — The Field Mapping Engine

**What it is:** the translation layer. EasyEcom's fields and ERPNext's fields don't line up one-to-one, so there's a configurable engine that maps one to the other — including a safe expression sandbox for small transformations, compile-time validation so a bad mapping is caught before it runs, and versioning/rollback so a mapping change can be undone. It has an FDE-facing editing surface and a **Test Mapping** feature that lets you run a mapping against a sample payload and see the result.

**What it means for you:** this is where you adapt the integration to a specific client's data without touching code. When EasyEcom sends a field shaped differently than ERPNext expects, you fix it here, test it with Test Mapping, and version it. Expect to use this a lot during onboarding.

**Tested by:** `../test_scripts/section_5_field_mapping.md`.

### C.4 Section 6 — Idempotency, Replay, Correlation & Queue

**What it is:** the machinery behind principles B.2, B.3, and B.9. Deterministic idempotency keys so re-runs don't duplicate; correlation IDs that thread a single logical operation across all its log rows; the background Queue (jobs that push to EasyEcom, retry with back-off, and report success/partial/failure counts); and the FDE replay/retry surfaces (including "Cursor Rewind" — re-pulling from an earlier point — and Retry/Cancel buttons).

**What it means for you:** this is the safety net. When you test it, you deliberately cause trouble — trigger the same sync twice and confirm no duplicate; force a failure and confirm it retries and then escalates; rewind a cursor and confirm a clean re-pull. The Queue Job record is where you watch background work succeed, partially succeed, or fail.

**Tested by:** `../test_scripts/section_6_idempotency_replay.md` (see the §6 test script).

### C.5 Section 7 — The Integration Contract

**What it is:** not a feature so much as the *rulebook* every flow must obey, enforced centrally so an individual flow can't break it. The key guarantees: every outbound call is logged centrally (a flow cannot skip logging); a per-record outcome is strictly **binary — Success or Failed** (there is no "partly done" state for a single record); the background Queue *job* can be Partial (some records succeeded, some failed) but each individual record within it is cleanly Success or Failed; foundational calls produce no per-company Sync Record; and each record's work is isolated, so one bad record in a batch doesn't sink the others.

It also introduces the **Sync Record Line** child table — when one logical sync touches several lines (e.g. a goods receipt with 10 SKUs), the parent Sync Record holds one row per line showing each line's outcome, so you can see *which* line was the problem, not just "the whole thing failed."

**What it means for you (and a critical point for testing):** the binary rule is something you will actively check. If a sync hits *any* unreconciled line — whether it blocks creation or is a tolerance variance — the **whole Sync Record is Failed**, and any document it would have created is rolled back (the books never hold a document the integration considers failed). You fix the cause and retry. There is no "Completed with Discrepancy" middle state for a record. If you ever see a record reporting Success while a line is visibly unreconciled, that's a bug — the contract forbids it.

**Tested by:** `../test_scripts/section_7_contract.md` (see the §7 test script).

---

## Part D — How the build and your testing fit together

### D.1 The rhythm

The app is built **one section at a time**. For each built section there is a **test script** — a numbered checklist you run on the **staging** site against an EasyEcom **sandbox** account. You mark each step Pass or Fail and raise a GitHub Issue for every Fail. You do not read code and you do not fix anything; you confirm the section works on a real site with real (sandbox) data.

### D.2 Where everything lives

| You need… | It's here |
| --- | --- |
| How to run tests (read first) | `process/test_scripts/HOW_TO_RUN_FDE_TESTS.md` |
| This primer (foundation) | `process/primers/FDE_PRIMER_sections_1_to_7.md` |
| Masters primer (§8 onward) | `process/primers/FDE_PRIMER_section_8_masters.md` |
| A section's test checklist | `process/test_scripts/<section>.md` |
| The failure issue template | `process/github_issue_template_test_failure.md` |
| The build/test status board | `process/BUILD_TRACKER.md` |
| The full readable spec (context only) | `docs/EasyEcom_Integration_Specification_v1.2.docx` |

### D.3 The order to come up to speed

1. Read this primer (you're doing it).
2. Read `../test_scripts/HOW_TO_RUN_FDE_TESTS.md` — setup, staging URL, sandbox login, how to execute a script, how to raise a failure.
3. Run the section test scripts in build order: foundation (§3+§4) → §5 → §6 → §7. Each script tells you what it covers and points back to the spec if you want depth.
4. Check `BUILD_TRACKER.md` any time to see what's built, what's tested, and what's next.

### D.4 Two mental habits that will serve you

- **When something looks wrong, go to the logs first** (API Call, Webhook Event). The audit trail (B.8) exists precisely so you can answer "what actually happened?" without guessing.
- **A clear discrepancy or a clean Failed is often a pass.** The system is *designed* to stop and surface rather than guess (B.7) and to treat a record as binary Success/Failed (§7). Refusing to silently proceed is the feature, not a defect — read each test script's expected outcome carefully before deciding Pass/Fail.

---

*Next: open `../test_scripts/HOW_TO_RUN_FDE_TESTS.md`, then start with `../test_scripts/foundation_section_3_and_4.md`.*
