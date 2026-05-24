# How to Run the FDE Staging Tests — A Guide

This guide is for the FDE (Forward-Deployed Engineer) who will test a built section on the
Frappe Cloud **staging** site. It explains what you're testing, where everything lives, how to
set up, how to run a test script, and what to do when a step fails. Read it once before your
first run; after that the test script itself is enough.

You do **not** need to have built anything or read the full SPEC to run these tests. You need
ERPNext fluency, the staging URL, and an EasyEcom sandbox login.

---

## 1. What you're testing, in one paragraph

This is an ERPNext-native app (`ecommerce_super`) that integrates ERPNext with EasyEcom (the
marketplace order/inventory platform) and reconciles marketplace settlements. It is built and
shipped **section by section**. Your job is to confirm a freshly-built section actually works on
a real (staging) site against a real EasyEcom **sandbox** account — not to read the code, and not
to fix anything. You run a checklist (the "test script"), mark each step Pass or Fail, and raise a
GitHub Issue for every Fail. That's it.

---

## 2. Where everything lives

Everything is inside the app repo, under `apps/ecommerce_super/`:

| You need… | It's here |
| --- | --- |
| **The test script** (your checklist) | `process/test_scripts/<section>.md` — e.g. `foundation_section_3_and_4.md` |
| **This guide** | `process/test_scripts/HOW_TO_RUN_FDE_TESTS.md` |
| **The GitHub issue template** (for failures) | `process/github_issue_template_test_failure.md` |
| **What "done" means for the section** | the acceptance criteria referenced at the top of the test script (in `SPEC.md`) |
| **The readable spec** (only if you want context) | `docs/EasyEcom_Integration_Specification_v1.2.docx` |
| **The build/test status board** | `process/BUILD_TRACKER.md` |

You will mostly only ever open **one file**: the test script for the section you're testing.

---

## 3. Before you start — setup checklist

You need four things in place. The test script repeats its own preconditions at the top; this is
the fuller version with the *how*.

1. **Staging site access.** You have the Frappe Cloud staging URL and can log in with a
   **System Manager** account. (Some steps check role-restricted behaviour — you may also be
   asked to log in as a user who has only the *EasyEcom FDE* role but **not** System Manager.
   Have a second such user ready, or create one, before you start the permissions block.)

2. **The app is installed and migrated clean** on that staging site. If you open the site and see
   an **EasyEcom** workspace / Control Panel in the left sidebar, it's installed. If you don't,
   stop — the section hasn't been deployed; tell the spec owner.

3. **EasyEcom sandbox credentials in hand** — `api_key`, `email`, `password`, and (for the
   foundation test) an account with **at least two locations** so primary-vs-operational can be
   exercised. These come from the EasyEcom sandbox portal. Never use a client's *production*
   EasyEcom credentials on staging.

4. **A note of the build under test.** At the top of the test script there's a "Build under test:
   commit / branch" line. Ask the spec owner which commit/branch is deployed to staging and write
   it in. This matters: when you raise an issue, the developer needs to know exactly which build
   you saw it on.

---

## 4. How to read a test script

Every test script is a set of tables grouped into blocks (e.g. "happy path", "negative / edge",
"data model", "permissions", "credential safety"). Each row is one test:

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| D1 | (what you do) | (what should happen) | ☐ P ☐ F |

- **Run every block, in order.** Don't skip a block because it looks like it overlaps another —
  the foundation script, for example, has seven blocks (auth happy-path, negative/edge, §4 data
  model, permissions, credential safety, the accounting dimension) and they test different things.
- For each row: do the **Action**, compare what you see to the **Expected result**, and tick **P**
  or **F**.
- If a row's expected result is unclear, do **not** guess Pass. Treat ambiguity as a finding and
  ask the spec owner — an unclear test is itself worth flagging.

---

## 5. Running it — step by step

1. **Open the test script** for the section (the spec owner will tell you which one;
   for the foundation it's `process/test_scripts/foundation_section_3_and_4.md`).
2. **Fill the header** — build/commit, the staging URL, your name, the date.
3. **Work the preconditions checklist** at the top. If any precondition can't be met, stop and
   tell the spec owner before going further — a failed precondition invalidates the run.
4. **Go block by block, row by row.** Tick P or F on each. Where a row says to log in as a
   specific role, actually switch users — don't assume.
5. For the foundation specifically, pay closest attention to:
   - **Credential safety (block C):** after saving credentials, reload the Account form and
     confirm you genuinely **cannot read the secret back** — masked, set-or-not only. This is the
     single most important check. If you can see a plaintext api_key/password after save, that's a
     serious failure.
   - **Stock-reco / Channel dimension (block A3):** create a Stock Reconciliation with an
     adjustment and leave **Channel blank** — it must save/submit without being blocked.
   - **Test Connection:** the button must *do something* — green success with good creds, a clean
     error with bad creds. A dead button or a raw error trace is a failure.
6. **Record the overall result** at the bottom (PASS only if every step passed) and list any
   issues you raised in the "Issues raised" table.

---

## 6. When a step fails — raise a GitHub Issue

Do this **per distinct failure** — one issue each, not one big issue for everything.

1. Open a new GitHub Issue using the template at
   `process/github_issue_template_test_failure.md` (label: `test-failure`).
2. Fill it objectively:
   - **Section / step** — e.g. "Foundation, step C1".
   - **Build under test** — the commit/branch from your header.
   - **What the script said to do** — copy the Action cell.
   - **Expected result** — copy the Expected cell.
   - **What actually happened** — precise: values, states, exact error text, what was missing.
   - **Screenshot / evidence** — attach one. Almost always worth it.
   - **Reproducibility** — every time / intermittent / once.
   - **Blocking?** — does this block the section passing, or is it cosmetic?
3. **Describe the gap; do not propose a fix.** Your job is to report what you saw vs. what was
   expected. The spec owner points Claude Code at the issue, and the fix comes back through the
   build process. (You may of course add a hunch in a comment, but the issue body stays factual.)
4. Add the issue link to the "Issues raised" table at the bottom of the test script.

---

## 7. What happens after you submit

You don't fix anything and you don't re-run on your own initiative. The flow is:

1. You raise issues → they land in the GitHub **defect queue**.
2. The spec owner triages them and points the developer (Claude Code) at specific issues.
3. Fixes are made, reviewed, and re-deployed to staging.
4. The spec owner asks you to **re-test the failed steps** (and usually a quick pass of the rest to
   catch regressions) on the new build — note the new commit in your header.
5. When every step passes on a build, the section is signed off and marked **Live** on the tracker.

A section can sit "at staging test with open issues" for a while — that's normal and correct. The
section isn't done until its script passes clean.

---

## 8. Do / Don't (quick reference)

**Do:**
- Run every block in order, on the build the spec owner names.
- Switch user roles when a step calls for it.
- Raise one issue per failure, objectively, with a screenshot.
- Stop and ask if a precondition fails or an expected result is ambiguous.

**Don't:**
- Don't fix code or change config to "make a test pass."
- Don't use production EasyEcom credentials on staging.
- Don't mark PASS on a step you're unsure about — flag it instead.
- Don't bundle multiple distinct failures into one issue.
- Don't re-test on a new build until the spec owner tells you it's ready.

---

*Questions about the process (not a specific test) → ask the spec owner. Questions about what the
app is supposed to do → the acceptance criteria at the top of the test script, then the SPEC docx.*
