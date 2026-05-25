# Start Here — FDE Onboarding

Welcome. This repo (`ecommerce_super`) is the EasyEcom–ERPNext integration. Your job is to come up to speed on what's built and help test it on staging. Everything you need is under `apps/ecommerce_super/`. Work through the steps below in order.

## 1. Understand the product (read first — about 45 minutes)

In `process/primers/`:

- **`FDE_PRIMER_sections_1_to_7.md`** — what the product is, the nine principles that explain its behaviour, and the foundation (connection, data model, field-mapping engine, idempotency, the integration contract). Read this start to finish.
- **`FDE_PRIMER_section_8_masters.md`** — the Master Sync model and the first master, Location (8a). Read after the foundation primer.

## 2. Learn how to test

In `process/test_scripts/`:

- **`HOW_TO_RUN_FDE_TESTS.md`** — setup, the staging site, the sandbox login, how to run a script, and how to raise an issue on failure. Read this before running anything.

## 3. Run the test scripts, in build order

Also in `process/test_scripts/`, run these in sequence:

1. `foundation_section_3_and_4.md`
2. `section_5_field_mapping.md`
3. `section_6_idempotency_replay.md`
4. `section_7_contract.md`
5. `section_8a_location.md`

Mark each step Pass or Fail. Raise a GitHub Issue for every Fail using the template at `process/github_issue_template_test_failure.md`.

## 4. Know where things stand

**`process/BUILD_TRACKER.md`** is the live status board — what's built, what's tested, and what's next. Check it any time.

## Two things to internalise before you start

These are in the primers, but they matter most:

- **A failure you can *see* is often the system working correctly.** It's designed to stop and surface a problem rather than guess. A rejected save, a record landing in "Failed" on a discrepancy, an automatic retry — read the test script's **Expected** column before deciding something is a bug.
- **When something looks wrong, go to the logs first** — the EasyEcom **API Call** and **Webhook Event** records. The full audit trail is there by design; it answers "what actually happened?" without guessing.

## Please don't

- **Don't edit the spec or build code** — your role is to verify, not develop. Changes to the spec and codebase go through a single controlled path; an unsolicited edit, however well-meant, breaks that discipline.
- The full spec (`docs/EasyEcom_Integration_Specification_v1.2.docx`) is **reference only** — open it when a test step points you there, not as a starting read.

## Questions

Reach out on [ your channel / contact — fill in ].
