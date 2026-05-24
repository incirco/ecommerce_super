# Section Delivery Process

How we build this integration: **one section at a time, through a fixed loop, in dependency order.** We do not perfect the whole spec before building. We make one section confident, build it, test it locally, deploy it to staging, have the FDE team test it against a script, fix what they find, and close it — then move to the next section.

This document is the process. It does not change per section; only the section being worked changes.

---

## The two trackers (do not conflate them)

- **`process/BUILD_TRACKER.md`** — the **frontier**. One row per section, one column per stage. Answers "what section are we on and what stage is it at." Single owner (the spec owner) updates it by hand. This is sequencing, not defects.
- **GitHub Issues** — the **defect queue**. One issue per concrete problem found during testing. Answers "what specific things are broken in the thing we're testing." The FDE team raises them; Claude Code fixes them when pointed at a specific issue.

A Markdown file tracks the frontier because the work is sequential and single-owner. GitHub Issues track defects because that is where the team and Claude Code interact. Keep them separate.

---

## The environments

| Environment | What happens there | Who |
| --- | --- | --- |
| **Local** (your machine: bench + Claude Code) | Build, first-pass automated + smoke test, commit | You + Claude Code |
| **GitHub** | Code spine; Issues are the defect queue the team raises and Claude Code fixes | Everyone |
| **Frappe Cloud** (staging) | The FDE team tests deployed functionality against the section's test script | You deploy; FDE team tests |

---

## The loop (per section)

Each section moves through nine stages. A section does not advance until its stage's exit gate is met. **Building stays single-threaded** — do not start building section N+1 until section N is at least through human test. (Reading ahead / getting the *spec* of N+1 ready in parallel is fine; building is not.)

| # | Stage | Owner | Exit gate |
| --- | --- | --- | --- |
| 1 | **Spec ready** | You + Claude (spec) | You have read the section closely and believe it is correct and complete enough to build. Amend the spec if not. |
| 2 | **Define done** | Claude (spec), you approve | The section has an "Acceptance criteria" block — the concrete, testable list of what "built correctly" means. You approve it. |
| 3 | **Sign off** | You | Tracker row → Approved, with date. This **freezes the spec section** for build. |
| 4 | **Local build** | Claude Code | Claude Code builds to the section + its acceptance criteria, commits to GitHub. Tracker → Built. |
| 5 | **Local test** | Claude Code + you | Claude Code writes and runs automated tests against the acceptance criteria (green); you smoke-test on the local bench. Tracker → Local test ✅. |
| 6 | **Deploy to staging** | You | The built functionality is deployed to Frappe Cloud. Tracker → Deployed. |
| 7 | **Team test** | FDE team | The team runs the section's **test script** on staging, records pass/fail per step, and raises a GitHub Issue for every failure (using the issue template). Tracker → Team testing. |
| 8 | **Fix loop** | Claude Code + team | You point Claude Code at specific GitHub Issues; it fixes locally, you redeploy, the team re-runs the failed steps. Repeat until the script passes clean. |
| 9 | **Go live / close** | You | Merged/deployed to production. Tracker row → Live, with date. |

---

## What each section needs before it can be signed off

Produced just-in-time, when you reach the section — **not** pre-written for all sections:

1. **The spec section itself** (already in `docs/SPEC.md`), read and confirmed correct.
2. **An Acceptance criteria block** appended to that section in the spec — "Section N is done when…". This is what Claude Code builds toward and what the test script is derived from.
3. **A team test script** in `process/test_scripts/section_<N>.md`, derived from the acceptance criteria, written for an ERPNext-fluent FDE who has not read the whole spec. Numbered steps, expected result per step, pass/fail boxes.

---

## Build order is not negotiable

Incremental delivery changes *how much we perfect upfront*, not *the order we build in*. The dependency order from the spec holds:

- **Foundation first** (Sections 3–7: Auth → Data Model → Field Mapping → Idempotency/Queue → **the Integration Contract**). The contract (Section 7) must be built and confident before any integration.
- **Then the integrations** (Sections 8–13: Master Sync → Buying → Stock Transfers → B2B → B2C → Returns), each an implementation of the contract.
- **Then the operational surface and the rest** (Sections 14+).

Walk the sections roughly in document order. Do not cherry-pick a flashy flow before its foundation exists.

---

## One safety rule for the GitHub → Claude Code flow

Claude Code does **not** autonomously scan GitHub Issues and act on whatever it finds. An issue is text from outside the trusted loop. **You** point Claude Code at a specific issue ("fix issue #14"); Claude Code reads that issue and fixes it. This keeps a human between "someone filed an issue" and "code changed in response." It is the difference between a tool and a liability.
