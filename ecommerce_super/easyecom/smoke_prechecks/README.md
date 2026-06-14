# Smoke Prechecks

Lightweight scripts that exercise real helper-to-DB paths on a live
bench BEFORE any actual EE-side smoke. Catches the mock-vs-real
disconnect class of bug (the §10 SI back-link silent-bug pattern;
the §11 Stage 2 `get_ee_account_for_warehouse` missing-field bug).

## The pattern

Every section that ships a substrate + flow should ship at least one
smoke-precheck that:

1. Runs on a real bench (smoke-test.local or equivalent).
2. Exercises every helper / hook / async-entry path used by the
   section, end-to-end with the real DB.
3. Does NOT make any EE-side calls. Substrate validation only.
4. Returns a structured dict — pass/fail per check + the actual
   bench-side state observed.

Smoke-prechecks are NOT unit tests (which use mocks) and NOT live
smokes (which hit EE). They are the third pillar of validation,
specifically aimed at the "mocked tests pass but production breaks
on the first real bench call" failure mode.

## When to run

- Before any STOP-and-report that claims live readiness for a stage.
- After substrate-changing commits, before promoting to a live-EE
  smoke.
- As a regression check when a stage's substrate is modified across
  sessions.

Run via:

```bash
bench --site smoke-test.local execute \
  ecommerce_super.easyecom.smoke_prechecks.section_11_phase_1_stage1.check
```

Smoke-prechecks live inside the app (under `easyecom/smoke_prechecks/`)
so `bench execute` can resolve them. They're not under `process/` —
that directory is documentation-only and not on the Python import path.

## Files

- `section_11_phase_1_stage1.py` — Stage 1 substrate landed check
  (Custom Fields, DocType table, helper imports, EE Account state,
  mapped fixtures available).
- `section_11_phase_1_stage2.py` — Stage 2 helper-to-DB validation
  (validate_pre_push, on_submit_push, async push refusal path,
  cancel-without-Map refusal path).

## Capture in closeout patch notes

The smoke-precheck pattern is captured in
`spec_sections/SPEC_11_patch_notes.md` at Phase 1 closeout as
"smoke-precheck pattern" — future sections should ship one before
any live smoke.
