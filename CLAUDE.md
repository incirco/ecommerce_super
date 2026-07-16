# CLAUDE.md

Operating manual for Claude Code working in the `ecommerce_super` repository.
This file is loaded by Claude Code at session start. Read it fully before doing anything else.

---

## What this repo is

This repo implements **ERPNext E-commerce Super-App** — a Frappe app suite that runs on ERPNext v16 to provide marketplace settlement reconciliation, multi-marketplace inventory and order integration via EasyEcom, AI-assisted operations, and a methodology-driven Forward-Deployed-Engineer (FDE) operating model. The product is opinionated: it embodies a methodology, not a configurable toolkit.

There are two apps in scope:

- **`ecommerce_super`** — the parent app. Methodology, integration core, operational surface, recon engine, AI assistant. This is where 95% of the code lives.
- **`ecommerce_super_<client>`** — per-client extension app, scaffolded per client by an FDE. Contains client-specific Field Mapping overrides, settlement template overrides, and methodology variances. **Never put generic logic here.**

---

## The four documents that govern this repo

In order of authority for any specific question:

| Document | Authoritative for | When in conflict |
| --- | --- | --- |
| `SPEC.md` | Technical contract. DocType schemas, API surface, error class hierarchy, file layout, hooks, settings, error codes. | Wins on "how is it built." |
| `BRD.md` | Methodology embedded as business rules. Account Role Map defaults, GST disposition rules, recon thresholds, the meaning of "good." | Wins on "what does the business want this to do." |
| `PRD.md` | Product requirements. What we're building and why. Scope boundaries. Pricing & commercial framing. | Wins on "should we build this at all." |
| `CLAUDE.md` (this file) | How you, the AI agent, behave while building. | Wins on "how should you work." |

**When two documents contradict each other**, you do not pick. You stop, surface the contradiction to the user with both sources quoted, and wait for direction. Do not silently reconcile.

---

## Reading order at session start

Every new session, before writing a single line of code:

1. Read this file (`CLAUDE.md`) fully.
2. Skim `SPEC.md` table of contents; read in full any section relevant to the current task.
3. Read `BRD.md` if the task touches recon, settlement, methodology defaults, or business rules.
4. Read `PRD.md` only if the task is about scope or product positioning.
5. Read any per-area `CLAUDE.md` if you've descended into a subdirectory that has one.
6. Read the relevant playbook from `docs/playbooks/` if the task matches one.

You may skip step 2-5 only if the task is purely mechanical (e.g., "rename this file"). For anything that produces or modifies behaviour, the relevant SPEC section is mandatory reading.

---

## How you should think while working in this repo

### 0. You read the spec; you never write it (the single most important rule)

`docs/SPEC.md` and the build packets in `spec_sections/` are **read-only for you.** You consume them; you do not edit them. This is absolute:

- **Never edit `docs/SPEC.md`.** Not to fix a typo, not to reconcile an inconsistency, not to "update it to match the code." The spec has exactly one author path: the human, working in a separate channel. If you change it, you create silent drift that no one approved.
- **Never edit files in `spec_sections/`.** These are approved, frozen build packets handed to you. They are inputs, not working files.
- **If you believe a spec section is wrong, incomplete, or contradicts the code or another section: STOP and report it to the human.** Quote the specific lines. State the problem. Wait. Do not fix the spec, and do not silently build something different from what the spec says to "work around" it. Either the spec is wrong (the human fixes it and re-approves) or your reading is wrong (the human corrects you) — but a human decides, not you.
- **The build packet's recap-verification step is mandatory.** When a `spec_sections/section_N.md` packet includes a recap of already-built foundation, verify that recap against the actual committed code *before* building. If the recap and the code disagree, STOP and report the delta — do not build against the recap, and do not silently follow the code. A delta means either the recap is stale or the code drifted from spec; the human resolves which.

You are the builder. The spec is the contract. A builder who rewrites the contract is not building — they are guessing with extra steps. When in doubt, the answer is always: stop, quote, report, wait.

### 1. The spec is the truth, not your training data

ERPNext v16 has many conventions. Some have changed since your training cutoff. Some are non-obvious. Some contradict v15 patterns you may have learned. **Always check `SPEC.md` Section 31 (Implementation Reference: file layout and hooks) before adding files or registering hooks.** When in doubt, look at how the spec describes a similar existing pattern in the codebase, not how Frappe documentation describes it.

### 2. Be implementation-faithful, not implementation-creative

This codebase is the embodiment of a methodology. The methodology team has thought carefully about every decision. If you see what looks like an obvious simplification — "this could be one DocType instead of three" — assume the spec is right and you are missing context. **Do not refactor toward your aesthetic.** Implement what the spec says.

If you genuinely believe the spec is wrong, say so explicitly to the user. Quote the spec line. State your concern. Wait for direction. Do not silently deviate.

### 3. Implementation order is dictated by the spec

`SPEC.md` Section 29 specifies the build phasing. `SPEC.md` Section 31.9 specifies the implementation order within each phase. **Do not skip ahead.** If the user asks you to implement Field Mapping (Section 5) before the foundation phase (Sections 3-7) is done, push back: "Field Mapping depends on the data model (Section 4) and is itself foundation; the integration contract (Section 7) and EasyEcomClient (Section 3) come first. Should I implement those first, or are they already done elsewhere?"

### 4. Two apps, one logic

Generic logic always lives in `ecommerce_super`. Client-specific logic always lives in `ecommerce_super_<client>`. The boundary is hard:

- A new flow that any client could use? → parent app.
- A new transformer for a marketplace any client might encounter? → parent app.
- A specific client's odd settlement schema for one marketplace? → client app fixture.
- A specific client's tweaked Account Role Map? → client app fixture.

**If you find yourself wanting to add a `client_name` field to a DocType in the parent app to handle one client's special case, stop.** That's a client-app override, not a parent-app field.

### 5. Fixtures are data; code is code; do not blur the line

Things that ship as fixtures (in `apps/ecommerce_super/ecommerce_super/fixtures/`):
- Custom fields, property setters, roles, workspaces, dashboard charts, reports
- Initial Field Mapping rulesets (the library shipped per `SPEC.md` Section 5.11)
- Initial Error Translation entries (`SPEC.md` Section 27.3)
- Default SLA Budgets (`SPEC.md` Section 23.2)
- Standard Marketplaces and Marketplace Channels

Things that **never** ship as fixtures:
- Per-client credentials, API keys, passwords (these are configured per-deployment by the FDE)
- Per-client EasyEcom Settings rows (one per Company, populated by FDE during onboarding)
- Per-client Marketplace Account rows
- Operational data (Sync Records, API Calls, Webhook Events) — these are produced at runtime

Adding new fixtures requires updating `SPEC.md` Section 31 (Implementation Reference, fixture load order). If you're tempted to add a fixture without an entry there, pause.

### 6. Test before you claim done

The product's reputation depends on integration correctness. "Looks right" is not enough. The minimum bar for any integration code:

- **Unit test** for any function with branching logic (`tests/unit/`)
- **Integration test** for any flow that touches the EE mock server (`tests/integration/`, with `tests/ee_mock/` providing the fixture responses)
- **Contract test** for any API client method (`tests/contract/`) that documents the EE-side request/response shape we're coding against

The test pyramid lives in `SPEC.md` Section 15. Read it before writing tests for an area for the first time.

A change without tests is incomplete. Don't say "implementation done" until tests pass. If you can't write a test because the surface isn't testable, that's a design defect — surface it to the user before pushing forward.

### 7. Idempotency is a property, not a comment

Every outbound EE call must carry a deterministic `idempotency_key` per the formula in `SPEC.md` Section 6.1. Every inbound webhook must dedup on the composite key per Section 6.4. Never write code that says `// TODO: make idempotent` and ships. **The codebase has no concept of "we'll add idempotency later"** — failure to be idempotent corrupts production data and there is no retroactive fix.

### 8. Migrations are append-only, mostly

Frappe's migration model lets you change DocType schemas in `patches/` files. **You may add fields freely**. You may **rename fields only with explicit user sign-off** (per `SPEC.md` Section 28's audit guarantees). You **may never delete a field that's been in production** without explicit migration to copy data and an explicit user-acknowledged audit gap. If a field is wrong, deprecate by marking `hidden: 1, read_only: 1` and adding a replacement; migrate at a planned cutover, not silently.

### 9. Audit hooks are mandatory

Every DocType change to a configuration record (Settings, Field Mapping, Source-of-Truth Map, Marketplace Account, etc.) must produce an `EasyEcom Configuration Audit` row per `SPEC.md` Section 28. Adding a new configuration DocType means adding the audit hook. Forgetting the audit hook is not a minor oversight — it breaks compliance and time-travel guarantees.

### 10. Never break the three-log invariant

The three log DocTypes (`SPEC.md` Section 10.1.2) have specific append-only and entity-centric semantics:

- `EasyEcom Sync Record` — entity-centric, mutable in place, one per (ERPNext doc, direction)
- `EasyEcom API Call` — call-centric, append-only, one per HTTP request
- `EasyEcom Webhook Event` — inbound-centric, append-only, one per webhook received

You may not insert into `EasyEcom API Call` and then update it. You may not delete a `EasyEcom Webhook Event`. You may not produce two `EasyEcom Sync Record` rows for the same (entity_type, erpnext_name, direction) on the same Company — update the existing one in place.

If you're tempted to violate any of these to "make the code simpler," you are designing the wrong solution.

---

## How to ask the user vs. proceed

A high-level rule: **ask when the answer is load-bearing for a decision the spec doesn't make; proceed when the spec answers it.**

Ask the user when:
- The spec is silent on a specific design choice you must make.
- The spec contradicts itself across two sections (already covered above; this is mandatory).
- The user's request implies a scope expansion not covered by `PRD.md` (e.g., they ask you to add Shopify support — this is `PRD.md` Section 17 / 30.2 v0.5 territory).
- A migration would touch production-shaped data (renames, deletes, field-type changes).
- A change requires modifying `SPEC.md` itself (not just code) — confirm both the code change and the doc change are wanted.
- You're about to make a non-trivial architectural choice (e.g., choosing between DocType inheritance vs. composition, between adding a custom transformer vs. using `custom_python`).

Proceed without asking when:
- The spec answers the question. If `SPEC.md` Section 6.1 specifies the idempotency key formula, just use it.
- It's a routine implementation task that follows established patterns (creating a new transformer, adding a test, fixing a clear bug).
- You're implementing what was just decided in the conversation, even if not yet in the spec — but make a note that the spec needs updating.

When in doubt: ask. The cost of asking is one extra round trip; the cost of proceeding wrong is potentially hours of unwound work and a degraded artifact.

---

## How to handle multi-step tasks

For any task estimated >2 hours of work or >5 file edits:

1. **Plan first.** Write a numbered plan in chat. The plan names files you'll touch, tests you'll write, and the order. Get user confirmation before starting.
2. **Checkpoint at natural breaks.** After completing a coherent chunk (one DocType + its tests, one flow + its integration test), pause and surface progress to the user. Don't run for an hour and dump everything at the end.
3. **Hold context.** When the conversation gets long, summarize what's been done in chat before continuing. Do not assume the user remembers your earlier reasoning.
4. **Surface defects you find along the way.** If you notice a typo in `SPEC.md` while implementing something else, mention it. Don't silently work around it.

---

## Specific patterns to follow

### Creating a new DocType

1. Locate the spec section for it. Every parent-app DocType is specified in `SPEC.md` Section 31 (Implementation Reference, DocType schemas). If it's not there, this is a new DocType and `SPEC.md` must be updated first.
2. Create the directory: `apps/ecommerce_super/ecommerce_super/ecommerce_super/doctype/<doctype_snake_case>/`.
3. Create the JSON: `<doctype_snake_case>.json` with all fields per the spec.
4. Create the controller: `<doctype_snake_case>.py` with `validate()`, `on_update()`, `before_save()` as specified.
5. Create the form script: `<doctype_snake_case>.js` if there are UI behaviours.
6. Create the test file: `test_<doctype_snake_case>.py` covering at minimum: insert, update, validation rule violations, permission checks.
7. Add to `fixtures` in `hooks.py` if it ships with seed data.
8. Run `bench --site <test_site> migrate` to verify the migration runs.

### Adding a Field Mapping rule

1. Locate the existing ruleset in `apps/ecommerce_super/ecommerce_super/fixtures/easyecom_field_mapping.json`.
2. **Never edit the fixture file directly with a "client-specific" rule.** Client-specific rules go in the client app's fixtures.
3. Add the rule following the schema in `SPEC.md` Section 5.3.
4. Add a test in `tests/unit/test_field_mapping_*.py` that constructs a sample input, runs the ruleset, asserts on the output.
5. If the rule introduces a new transformer that's not in the closed vocabulary (`SPEC.md` Section 5.5), this is a parent-app code change to `field_mapping/transformers.py` — pause and confirm with the user first.

### Implementing a new flow

1. Read the spec section for that flow in full (one of Sections 4-9 of `SPEC.md`).
2. Read the corresponding playbook in `docs/playbooks/` if it exists.
3. Implement in `apps/ecommerce_super/ecommerce_super/ecommerce_super/flows/<flow_name>.py`.
4. The flow's hooks register in `hooks.py` (`doc_events` and/or `scheduler_events`).
5. Idempotency keys per Section 6.1.
6. Correlation IDs per Section 6.2.
7. Sync Records updated, API Calls inserted (append-only), Webhook Events processed (append-only).
8. Errors raise the right exception class from `exceptions.py` (Section 31, error class hierarchy).
9. Integration test runs the flow end-to-end against the EE mock server.

### Modifying the EasyEcomClient

1. Every public method corresponds to a documented endpoint in `SPEC.md` Section 31 (Implementation Reference, client method list).
2. New endpoints require updating both the appendix and the client.
3. All calls go through `_request()` so rate limiting, JWT refresh, idempotency, redaction, and API Call recording happen automatically. **Never bypass `_request()` even for "simple" calls.**
4. Type hints mandatory. Return types must match the dataclass/TypedDict shapes in `types.py`.
5. Contract test in `tests/contract/test_<endpoint_name>.py` documents expected EE-side request/response shape, with a sample fixture.

### Writing alerts

1. Every alert is an `Integration Alert` DocType row, not a free-floating notification.
2. Severity from `SPEC.md` Section 19.1.
3. Message uses the alert template from Section 19.6 — populated with translated error from `EasyEcom Error Translation` library, suspected cause, suggested actions.
4. Financial impact attached per Section 25 (computed via the named impact calculator).
5. Routing handled by `alerts/router.py` — never call email/Slack APIs directly from flow code.

---

## Anti-patterns to avoid

These have all been seen in earlier integrations and produce hard-to-fix problems. Do not introduce them.

- **Synchronous EE calls inside Frappe document save transactions.** All outbound EE traffic goes through `enqueue_easyecom_job()`, which creates an EasyEcom Queue Job tracking row AND calls `frappe.enqueue` under the hood. Never call `frappe.enqueue` directly for EasyEcom work — the facade owns the pairing of DocType row + RQ job. The only exception to async-everywhere is the `test_connection` button, which is interactive.
- **Hardcoded timeout values.** Read from `EasyEcom Settings` per Section 3.1.4 (Sync Tuning).
- **Catching bare `Exception`.** Catch the specific exception class from `exceptions.py`. If you don't know which class, the spec's Section 31 (error class hierarchy) names them; pick the right one.
- **Logging credentials, even at debug level.** The redaction layer in `api/redaction.py` exists for a reason — every payload passes through it before persistence. Don't bypass.
- **Adding `print()` statements for debugging and forgetting to remove them.** Use `frappe.logger().debug()` so it goes to the right log file and is filtered by level.
- **Calling `frappe.get_doc().save()` without `ignore_permissions=False` audit.** The integration runs as the `EasyEcom Sync` user, which has scoped permissions. Don't escalate to System Manager unless the spec specifically says to.
- **Storing JSON in `Long Text` and parsing it everywhere.** If a field has structure, it's a child table. The only legitimate JSON-in-Long-Text fields are: payload archives, snapshot serialisations, audit before/after states. Anything queryable should be a real field or a child table.
- **Bypassing the Field Mapping engine.** If you're translating between an ERPNext shape and an EE shape, use a Field Mapping ruleset. Inlined `payload['xyz'] = doc.abc` in a flow is a smell — that translation should be a rule.
- **"Temporary" hardcoded marketplace lists.** Marketplaces are a DocType. Adding `if marketplace == 'Amazon':` chains in flow code is the road to a 200-marketplace if-ladder. Use Marketplace Account configuration instead.
- **Re-implementing what's in the spec because you didn't read it.** This is the most common and most expensive failure mode. Always read first.

---

## Specific Frappe v16 things to know

- **DocType class autodetection** — Frappe expects controller modules at `<app>/<app>/doctype/<snake>/<snake>.py` with a class named `DocType()` matching the DocType name (CamelCase). Don't put controllers anywhere else.
- **`hooks.py` is loaded once per site startup**, but development edits don't auto-reload. After modifying `hooks.py`, run `bench --site <site> clear-cache && bench restart`.
- **`bench migrate`** runs patches, applies new fixture data, recreates DocTypes from JSON. Runs are idempotent; you can re-run without consequence.
- **Test fixtures should use `frappe.tests.utils.FrappeTestCase`** as the base class — gives you transactional isolation per test.
- **Custom fields shipped as fixtures** require the linked DocType (e.g., Item) to exist before the fixture loads. The `fixtures` order in `hooks.py` matters.
- **`@frappe.whitelist(allow_guest=True)`** opens a method to unauthenticated requests. The webhook receiver is the *only* method we expose this way. Adding another such method requires explicit user approval and a security review note in the PR.
- **Long Text fields >64KB** should be considered for `Code` type or external storage if they grow without bound.
- **Caffeine cache** (`SPEC.md` Section 1.4) is the v16 default. Use `frappe.cache().get_value()` and `set_value()`. Cache invalidation lives in the controller's `on_update`.

---

## Frappe-primitives-first discipline

Where Frappe ships a primitive that fits, **use it**. Don't reinvent. The integration is layered with our custom DocTypes and logic on top of Frappe's primitives, never replacing them. Specifically:

| Need | Frappe primitive | Our integration's layer |
| --- | --- | --- |
| Async work execution | `frappe.enqueue` (RQ-backed) | `EasyEcom Queue Job` DocType for tracking + `enqueue_easyecom_job()` facade |
| Worker pools / queue tiers | `bench start` worker config (`short`/`default`/`long`) | `QUEUE_FOR_JOB_TYPE` routing in `queue/routing.py` |
| Cache | `frappe.cache().get_value()` / `set_value()` (Redis-backed via Frappe's pool) | Cache invalidation hooks; per-Company concurrency semaphore via `frappe.cache().incr/decr` |
| Realtime UI updates | `frappe.publish_realtime` | `document_banner.js` subscribes to Queue Job events |
| Document change tracking | `Version` DocType (auto via `track_changes: 1`) | `EasyEcom Configuration Audit` for structured `actor_role`, `change_reason`, causal chains |
| Email | `frappe.sendmail()` (queues via `Email Queue`) | `Integration Alert` DocType for lifecycle, suppression, financial impact |
| Desk notifications | `frappe.publish_realtime` to user's bell | Same; the bell uses Frappe's standard primitive |
| Permissions | `permission_query_conditions`, `has_permission` hooks | These hooks are wired in `hooks.py`; the per-Company filter logic lives in `permissions.py` |
| Error logging | `Error Log` (auto for unhandled exceptions) | `EasyEcom API Call` complements Error Log; both exist with different purposes |
| Background scheduling | `scheduler_events` in `hooks.py` | Cron entries call our handlers; some handlers enqueue per-Company jobs via `enqueue_easyecom_job` |

**Direct calls to Redis, RQ, the Frappe socket layer, or any Frappe-internal primitive bypassing the public API are not permitted in this codebase.** If a feature needs something Frappe doesn't expose, surface to the user before implementing — it's likely either available through a public API you missed, or genuinely needs the methodology team to bless an exception.

### The rule extends up the stack: ERPNext + India Compliance

The Frappe-primitives rule applies with equal force one level up. **On the ERP side we follow standard ERPNext and India Compliance behaviors; we never touch, patch, or reimplement them. Translation happens only at the marketplace-integration boundary (the EE payload builder, the mirror handler, etc.).**

Concretely, when the answer to "what number goes here?" is already computed by ERPNext or India Compliance, **read it** — do not recompute. When the answer to "what side-effect should fire?" is already wired into ERPNext's document lifecycle, **let it fire** — do not simulate.

| Need | ERPNext / India Compliance primitive | Our integration's layer |
| --- | --- | --- |
| Applied tax rate per SO/SI line | `so.taxes[].item_wise_tax_detail[item_code]` (JSON: `[rate, amount]`, populated by `calculate_taxes_and_totals.py`) | Read at the EE boundary. Do NOT sum `so_item.item_tax_rate` — that dict lists template rates across every account_head (Output + Input + RCM) and sums to ~30% for a genuinely 5% item. See `#201` → `#204`. |
| Tax amount per line | Same JSON, `[1]` position | Read at boundary |
| Tax-inclusive line total | `si_item.amount + Σ(item's tax_amount across si.taxes rows)` | Read at boundary. Do NOT reconstruct from `rate * qty * multiplier`. |
| Pre-discount list rate | `si_item.price_list_rate` | Read |
| SO / SI totals | `net_total`, `grand_total`, `total_taxes_and_charges` | Read |
| Populate `SI.taxes` child table | `si.taxes_and_charges = "<Template Name>"` + per-line `si_item.item_tax_template = "GST 5%"` → ERPNext computes rows via `set_missing_values()` | Set the template + `item_tax_template`; DO NOT hand-append rows with `charge_type="Actual"` or `"On Net Total"` and a manually-derived rate. See `#206`. |
| SI dates | `set_posting_time = 1` + `posting_date`; `payment_terms_template = ""` to disable term-driven `due_date` recomputation | Set at insert. DO NOT invent shadow fields like `transaction_date` on SI (not native — it belongs to Sales Order). See `#205`. |
| Freeze `posting_date` across re-validates | `set_posting_time = 1` at insert | Set once; do NOT `db_set` after the fact to "heal" drift — the flag prevents the drift. |
| IRN / e-invoice payload | India Compliance's `generate_e_invoice.py` + `E Invoice Log` DocType | Trigger via IC's public entrypoint. DO NOT build the GSTN JSON payload ourselves. |
| E-way bill payload | India Compliance's `generate_e_waybill_json.py` + `E Waybill Log` DocType | Trigger via IC's public entrypoint. DO NOT rebuild. |
| GL entries on SI submit | ERPNext's `make_gl_entries()` fires automatically on `si.submit()` | Let it fire. DO NOT insert rows into `tabGL Entry` directly. |
| Stock ledger on SI with `update_stock=1` | ERPNext auto-creates Stock Ledger Entries | Let it happen. DO NOT bypass the flag by inserting SLEs directly. |
| Custom DocPerm on framework doctypes | `frappe.permissions.add_permission()` + `setup_custom_perms()` + `update_permission_property()` | Use the API. DO NOT `frappe.new_doc("Custom DocPerm").insert()` — Frappe's rule is "if ANY Custom DocPerm exists, ALL standard DocPerms are ignored," so a raw insert shadows every other role's access. See `#200`. |

**Never touch the standard code path.** No monkeypatches of `frappe.model`, `frappe.core`, `erpnext.controllers`, `erpnext.accounts`, or India Compliance modules. No direct SQL against standard tables (`tabSales Invoice`, `tabGL Entry`, `tabTax Rule`, etc.) to bypass validation. No `frappe.db.set_value` on doctype fields whose `on_change` side-effects matter. If a standard behavior is wrong for us, either (a) subclass with `hooks.override_doctype_class`, (b) attach a `doc_events` hook, or (c) file with the ERPNext / India Compliance project. **Never patch, never silently work around.**

**Before writing any tax / pricing / discount / GL / e-invoice / permission code:** grep the ERPNext or India Compliance source for whether the number / row / behavior is already produced by their code. If it is, use it. If you're unsure, ask the user before writing your own — the answer is almost always "there's a primitive; find it."

**Why this rule exists** (evidence from July 2026, all live-money incidents):

- `#200` — we reinvented `frappe.permissions.add_permission()` by inserting `Custom DocPerm` rows directly. Wiped Territory / Customer Group / Print Format permissions on MMPL prod (`live16version.frappe.cloud`) on the next `bench migrate`. Fixed in `#202`.
- `#201` — we reinvented tax-rate lookup by summing `so_item.item_tax_rate` (Item Tax Template dict listing every account_head). Over-billed SO-2610402 by ~24%. Fixed by @garv999 in `#204` using ERPNext's own `item_wise_tax_detail`.
- `#205`, `#206`, `#207` — audit follow-ups documenting three more reinventions in the invoice mirror: a fake `transaction_date` on SI (belongs to SO, not SI); hand-built `SI.taxes` rows instead of `Sales Taxes and Charges Template` + per-item `item_tax_template` (visibly wrong on any mixed-rate SO); dead-code fallback tiers in `_resolve_line_items`.

Each reinvention cost real money on live orders and days of forensic work. The rule pays for itself the first time it prevents an incident.

### Custom GSP contract: any behavior change requires a doc update in the same PR

Our three whitelisted endpoints (`/gettoken`, `/einvoice/update`, `/ewaybill/update`) have a public reference doc at `docs/custom_gsp_contract.md`. It's the canonical source of truth for what EasyEcom, partners, and the next maintainer see from our side.

**Any behavior change to those endpoints MUST update this doc in the same PR.** Concretely:

- Adding a new request field → document it in the field table
- Adding a new response field → document it + example
- Adding a new failure message → document it in §3 (Failure modes reference)
- Changing the Bearer TTL, rate limit default, or auth requirement → update the relevant subsection
- Adding a new endpoint → add a new §2.X subsection with the full shape

This rule exists because gh#130 (root paths) and gh#142 (`orders` shape) were both spec-vs-live drift bugs — our code did one thing, our external-facing "contract" claimed another. Live code shipped without a corresponding doc update; production incidents were the tell. Requiring the doc update in the same PR closes that gap at review time.

**Reviewers** on any PR touching `ecommerce_super/easyecom/api/gsp.py` or `ecommerce_super/easyecom/flows/b2b_sales/gsp_handler.py` should reject the PR if `docs/custom_gsp_contract.md` isn't updated in the same commit set.

---

## Conventions and style

- **Python**: ruff for lint, ruff format for formatting, mypy for type-checking. All public functions have type hints. Docstrings follow Google style.
- **JS**: prettier for formatting. ES2020+. No jQuery in new code (Frappe core uses it but we don't reach for it).
- **JSON DocType files**: 2-space indent, Frappe's auto-generated format. Hand-edits should pass `frappe.utils.fmt_json()`.
- **Field naming**: snake_case throughout. Custom fields prefixed `ecs_` (parent app) or `ecsc_<client>_` (client app). DocType names CamelCase with spaces ("EasyEcom Sync Record"). Fieldnames inside DocTypes always snake_case.
- **Imports**: standard library, third party, frappe, ecommerce_super (in that order). Within each block, alphabetical.
- **Commits**: imperative mood, short summary line, body explains *why* not *what*. Reference the relevant `SPEC.md` section if applicable.

---

## When something goes wrong

If you produce code that doesn't work as expected:

1. **Don't double down.** A failing test means your understanding is off. Re-read the spec section.
2. **Don't disable tests.** A test that fails is a test that's doing its job. Either the test is wrong (rare; verify against spec) or the code is wrong (common; fix the code).
3. **Surface to user early.** "I'm stuck on X because Y. Should I try approach A or B?" beats grinding silently for an hour.
4. **Don't paper over with try/except.** Catching an exception just to silence a test failure is technical debt that compounds. Find the cause.

---

## What success looks like for this repo

- Every DocType exists per `SPEC.md` Section 31 (Implementation Reference).
- Every flow runs end-to-end with idempotency, retry, and observability.
- The recon engine receives the data shapes it expects (Section 17 of `SPEC.md`).
- An FDE can debug any production issue using only the operational surface (Section 18) without needing to SSH or query the database directly.
- Test coverage on `flows/`, `field_mapping/`, `recon/` is >85%.
- A new client onboarding takes <2 weeks end-to-end (per the FDE operating model in `PRD.md`).
- The Morning Brief (Section 26) is the most-clicked page in the desk.

If we're hitting these, we're winning.

---

*This file is authored by the methodology team. Edits to this file are reviewed by both the methodology lead and the engineering lead. If you (Claude Code) want to suggest a change to this file, propose it as a PR with rationale; don't edit it silently.*
