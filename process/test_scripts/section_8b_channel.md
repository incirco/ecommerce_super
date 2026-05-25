# Section 8b — Channel (Marketplace) Discovery & Classification — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the second master: channel discovery via a **per-location sweep** of `/current-channel-status`, dedupe into one catalogue, and the FDE classification workflow. Derived from `docs/SPEC.md` §8.6 / §31.2.18 and the 8b packet.

> **First time running these?** Read `HOW_TO_RUN_FDE_TESTS.md` first, then the masters primer (`../primers/FDE_PRIMER_section_8_masters.md`, Part G) — it explains the per-location sweep and the classification workflow.

> **The model in one line.** EasyEcom answers "which channels are live" **per location**, so discovery sweeps **every** discovered location (any state), unions the results, and **dedupes by EasyEcom marketplace id** into one channel catalogue. A channel is active if it's active on any location. You then classify each channel (Unclassified → Classified → Active, or Ignored).

> **Prerequisite:** Locations must be discovered first (8a). Channel discovery sweeps locations; with none, there's nothing to sweep (the system guards this with a "discover locations first" message).

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` installed on staging, migrated clean
- [ ] System Manager access; plus an **EasyEcom FDE** (non-System-Manager) user; plus a plain user with neither role (for negative role-gating)
- [ ] EasyEcom Account configured against the sandbox; Test Connection successful
- [ ] **8a already run** — locations discovered (at least a couple of EasyEcom Location rows exist, in any workflow state)
- [ ] You know roughly which channels to expect from the sandbox (the sample had ~16: Flipkart, meesho, Amazon, TaTa Cliq, own storefronts, connectors, etc.)

### Steps — Discovery sweep + dedupe

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| C1 | On the EasyEcom Account form, click **Discover → Channels** | The sweep runs and reports an outcome (how many channels created/updated) | ☐ P ☐ F |
| C2 | Open the Marketplace list | One row per channel; new rows in workflow state **Unclassified** | ☐ P ☐ F |
| C3 | Confirm **dedupe** — a channel that exists on multiple locations (e.g. Flipkart) | Appears **exactly once** in the catalogue, not once per location | ☐ P ☐ F |
| C4 | Inspect a channel row | marketplace_id (the EE numeric id), marketplace_name, is_active populated; channel_type blank (not yet classified) | ☐ P ☐ F |
| C5 | Check `is_active` reflects active-on-any-location | A channel that's Active on at least one location shows is_active set; one Inactive everywhere shows unset | ☐ P ☐ F |
| C6 | (If you can run with a location in To Map / Skipped state) confirm its channels still came in | The sweep polls ALL locations regardless of state — channels from unmapped locations are present | ☐ P ☐ F |

### Steps — The "discover locations first" guard

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| C7 | (On a fresh site with NO locations, if testable) click Discover → Channels | A friendly message tells you to run Discover → Locations first — not a confusing empty result or an error dump | ☐ P ☐ F |

### Steps — The classification workflow

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| K1 | Filter the Marketplace list to `workflow_state = Unclassified` | Your worklist — the freshly discovered channels | ☐ P ☐ F |
| K2 | On an Unclassified channel, try **Classify** without setting `channel_type` | Blocked — Classify requires channel_type set first | ☐ P ☐ F |
| K3 | Set `channel_type` (e.g. B2C Marketplace for Flipkart), then **Classify** | Moves to **Classified** | ☐ P ☐ F |
| K4 | From Classified, click **Activate** | Moves to **Active** | ☐ P ☐ F |
| K5 | On a connector artifact (something that isn't a real sales channel), **Mark Not Relevant** | Moves to **Ignored** | ☐ P ☐ F |
| K6 | Confirm reverse transitions exist | An Active channel can go back to Classified; an Ignored one back to Unclassified | ☐ P ☐ F |
| K7 | As the non-FDE / no-role user, open a channel | The workflow action buttons are **not** available — transitions are FDE-gated (System Manager inherits) | ☐ P ☐ F |

### Steps — is_active vs workflow_state are independent

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| X1 | Find a channel that is EE-Active but still Unclassified | This is a valid, normal state — EE has it live, you haven't classified it yet. Not an error | ☐ P ☐ F |
| X2 | Classify and Activate it; is_active unchanged | Your workflow state advancing does not change is_active (EE's status) — they're separate axes | ☐ P ☐ F |

### Steps — Re-pull (steady state)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| R1 | Run Discover → Channels again after some are classified/active | Existing channels are **skipped** — their workflow_state and channel_type are preserved (a re-pull does not reclassify or reset your work) | ☐ P ☐ F |
| R2 | (If a new channel can be added in the sandbox) re-sweep | The new channel appears fresh in Unclassified + you're alerted; existing ones untouched | ☐ P ☐ F |

### Steps — One location's failure doesn't sink the sweep (per-record isolation)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| P1 | (If you can induce one location to fail — e.g. a location whose token can't be acquired) run the sweep | The failing location is recorded as failed, but the sweep continues and channels from the healthy locations still land — one location's failure does not abort the whole sweep | ☐ P ☐ F |

> **Known deferred (do NOT raise as failures):** the **Marketplace Account** (seller id, GSTIN, settlement template) is NOT part of 8b — deferred to reconciliation; don't look for it. `reporting_parent` is optional/FDE-set — blank is fine. The new-channel **alert** is a basic desk notification (richer alerts → §18). Per-location channel status is not tracked (catalogue is "active somewhere") — by design. No push of channels to EE. No custom classification UI — standard form + workflow buttons. How a channel's `channel_type` actually routes an order (B2C vs B2B flow) is exercised in §11/§12, not here.

### Overall result
- [ ] **PASS** — every applicable step passed (deferred items excluded)
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number, the Expected cell, what you saw, and a screenshot. Remember: a channel sitting Unclassified, a blocked Classify (no channel_type), or one location failing while the sweep continues are all the system **working** — only raise a failure when the Expected behaviour didn't happen (e.g. a duplicate channel row, or one location's failure aborting the whole sweep).
