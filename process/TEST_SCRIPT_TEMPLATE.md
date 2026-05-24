# Test Script Template

Copy this to `process/test_scripts/section_<N>.md` for each section when it reaches the team-test stage. Written for an **ERPNext-fluent FDE** testing on **Frappe Cloud staging** — assume they know ERPNext (DocTypes, submit/cancel, the desk) but have **not** read the full spec. Every step states what to do and exactly what correct looks like, so a failure is objective: "step 4 expected X, I saw Y."

Derive the steps from the section's **Acceptance criteria** block in `docs/SPEC.md`. One acceptance criterion may become one or several test steps.

---

## Section <N> — <Title> — Test Script

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions (set up before testing)
- [ ] _e.g._ EasyEcom Account configured with valid sandbox credentials
- [ ] _e.g._ At least one operational EasyEcom Location mapped to a Company and warehouse
- [ ] _e.g._ Rate-limit tier set to Default
- [ ] _list every state that must exist before step 1 makes sense_

### Steps

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| 1 | _What to do, in ERPNext terms_ | _Exactly what the tester should observe — a value, a state, a record, a banner_ | ☐ P ☐ F |
| 2 |  |  | ☐ P ☐ F |
| 3 |  |  | ☐ P ☐ F |

### Negative / edge cases (the failures matter as much as the happy path)
| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| N1 | _e.g._ Trigger a record that must fail (bad data) | _e.g._ Failed Sync Record appears with a plain-English reason; other records unaffected; job shows Partial | ☐ P ☐ F |

### Overall result
- [ ] **PASS** — every step passed; section behaves as specified
- [ ] **FAIL** — one or more steps failed; issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---

**If a step fails:** raise a GitHub Issue using the bug template. Reference this section and step number, state what you expected (copy the Expected cell), what you actually saw, and attach a screenshot. Do not fix it yourself or describe a fix — describe the gap objectively. Claude Code will be pointed at the issue.
