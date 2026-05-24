---
name: Test failure (FDE staging test)
about: Raise when a test-script step fails during FDE testing on Frappe Cloud
title: "[S<section>/step<n>] <short summary>"
labels: test-failure
---

<!--
Raise one issue per distinct failure. Be objective: state what the script told you
to expect and what you actually saw. Do not propose a fix — describe the gap.
Claude Code will be pointed at this issue by the spec owner.
-->

**Section / step:** Section ___ , step ___ (from `process/test_scripts/section_<N>.md`)

**Build under test:** commit / branch ___________  ·  **Environment:** Frappe Cloud staging

**What the script said to do:**
> (copy the Action cell)

**Expected result (from the script):**
> (copy the Expected cell)

**What actually happened:**
> (describe precisely — values, states, error text, what was missing)

**Screenshot / evidence:**
> (attach)

**Reproducibility:** ☐ every time ☐ intermittent ☐ once

**Blocking?:** ☐ blocks the section passing ☐ cosmetic / minor
