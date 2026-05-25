# Section 8a — Location Discovery & Mapping — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the first master: location discovery (`/getAllLocation`), the four-state mapping workflow, the company↔state invariant, re-pull behaviour, the Source-of-Truth Map, and the back-fill. Derived from `docs/SPEC.md` §8.4.1/§8.4.2, §31.2.2/§31.2.23, and the 8a packet.

> **First time running these?** Read `HOW_TO_RUN_FDE_TESTS.md` first, then the masters primer (`../primers/FDE_PRIMER_section_8_masters.md`, Part F) — it explains the workflow and why this is your day-one onboarding job.

> **The model in one line.** Locations are **born in EasyEcom, pulled into ERPNext, then mapped by you** — never pushed. A pulled location sits in a visible workflow state (To Map → Mapped but not Live → Live, or Skipped); your worklist is the **To Map** filter.

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` installed on staging, migrated clean
- [ ] System Manager access; plus an **EasyEcom FDE** (non-System-Manager) user; plus a plain user with **neither** role (for the negative role-gating check)
- [ ] An EasyEcom Account configured against the **sandbox**; Test Connection successful (primary location JWT acquired)
- [ ] You know which sandbox locations to expect (e.g. the three: two stock-handling warehouses + one non-stock test account)
- [ ] At least one EasyEcom Location row that was created **manually before** this build, if any exist (for the back-fill check); if none exist, mark the back-fill block N/A

### Steps — Discovery pull (trigger + landing)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| A1 | On the EasyEcom Account form, click **Discover Locations** | The pull runs and reports an outcome inline (how many created/updated) | ☐ P ☐ F |
| A2 | Open the EasyEcom Location list | One row per sandbox location; new rows are in workflow state **To Map** (color-coded) | ☐ P ☐ F |
| A3 | Open one pulled location | Address fields populated: city, state, country, pincode, plus billing and pickup address blocks. (State present — it drives GST.) | ☐ P ☐ F |
| A4 | Check `is_wms_location` against the source | It's 1 for stock-handling locations (EE stockHandle=1) and 0 for non-stock ones — derived automatically | ☐ P ☐ F |
| A5 | Confirm mapping fields are blank | frappe_company and mapped_warehouse are empty on freshly-discovered rows (discovery never guesses the mapping) | ☐ P ☐ F |
| A6 | Confirm `api_token` is absent | No api_token value anywhere on the Location record — it is dropped on the way in | ☐ P ☐ F |
| A7 | Open the API Call list, filter endpoint `/getAllLocation` | The pull is logged; `is_foundational = 1`, company blank; the response payload shows `api_token` as `***REDACTED***` | ☐ P ☐ F |
| A8 | Confirm no Sync Record was created for the pull | Foundational call → no per-company Sync Record (§7.7) | ☐ P ☐ F |

### Steps — The mapping workflow (the four states)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| W1 | Filter the Location list to `workflow_state = To Map` | These are your worklist — the freshly discovered, unmapped locations | ☐ P ☐ F |
| W2 | On a To-Map location, try the **Map** action **without** setting a Company | Blocked — Map requires a Company set first (the transition condition) | ☐ P ☐ F |
| W3 | Set a Company + mapped Warehouse, then **Map** | Moves to **Mapped but not Live** | ☐ P ☐ F |
| W4 | From Mapped but not Live, click **Go Live** | Moves to **Live**; `is_operational` flips to **1** automatically | ☐ P ☐ F |
| W5 | On another To-Map location, **Mark Not Relevant** | Moves to **Skipped** | ☐ P ☐ F |
| W6 | Confirm reverse transitions exist | A Live location can be paused back to Mapped but not Live (is_operational → 0); a Skipped one can return to To Map | ☐ P ☐ F |
| W7 | As the **non-FDE / no-role** user, open a Location | The workflow action buttons (Map / Go Live / etc.) are **not** available — transitions are role-gated to FDE (System Manager inherits) | ☐ P ☐ F |

### Steps — The company ↔ workflow-state invariant

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| V1 | Try to save a **To Map** location with a Company set | Rejected — To Map must have no Company (the invariant working, not a bug) | ☐ P ☐ F |
| V2 | Move a Mapped/Live location to **Skipped** | Company and mapped_warehouse are **auto-cleared** as it enters Skipped (you don't have to blank them manually) | ☐ P ☐ F |
| V3 | Try to leave a **Mapped but not Live** location with no Company | Rejected — mapped states require a Company | ☐ P ☐ F |
| V4 | Confirm a **Live** location always has a Company | No Live location can exist without frappe_company | ☐ P ☐ F |

### Steps — Re-pull (steady state)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| R1 | Run **Discover Locations** again after some are already mapped/Live | Already-known locations update their EE-supplied fields in place; their workflow state is **untouched** (a Live location stays Live, not reset) | ☐ P ☐ F |
| R2 | (If you can add a new location in the sandbox) re-pull | The new location appears fresh in **To Map** and you're notified; existing ones unaffected | ☐ P ☐ F |
| R3 | Confirm unmapped locations are not flagged as errors | To Map / Skipped locations are a normal steady state, not error rows | ☐ P ☐ F |

### Steps — Source-of-Truth Map

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| T1 | Open the Source-of-Truth Map DocType/list | It exists; you can create a row linking a Warehouse + Company to an EasyEcom location | ☐ P ☐ F |
| T2 | Inspect the fields | warehouse, company, easyecom_location_key, is_linked (computed), enabled, plus the authority fields: inventory_master, pr_origination, adjustment_origination, mirror_stock_reservations | ☐ P ☐ F |
| T3 | Create a row and confirm `is_linked` computes | is_linked becomes true once easyecom_location_key is set | ☐ P ☐ F |
| T4 | Try to create a duplicate (same company + warehouse) | Rejected — (company, warehouse) is unique | ☐ P ☐ F |

### Steps — Back-fill of pre-existing manual rows (mark N/A if none)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| K1 | Look at Location rows that existed (manually) before this build | Each is now in a sensible workflow state — none left in a blank/null state | ☐ P ☐ F |
| K2 | Confirm the state matches the prior config | Was mapped + operational → **Live**; mapped but not on → **Mapped but not Live**; otherwise → **To Map** | ☐ P ☐ F |

> **Known deferred (do NOT raise as failures):** the new-location **alert** is currently a basic desk notification — the richer alerting depends on §18 (not built). Locations are **pull-only** — there is no push-to-EE, by design. There is **no custom mapping UI** — mapping is on the standard Frappe form via the workflow buttons. The Source-of-Truth **authority fields** are configurable now but the *behaviour* that reads them (inventory ownership, PR origination, adjustments, reservation mirroring) is built in §9–§11 — don't test their effects yet, only that the fields exist and save. Configuration Audit depends on §28.

### Overall result
- [ ] **PASS** — every applicable step passed (deferred items excluded)
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number, the Expected cell, what you saw, and a screenshot. Remember: a rejected save in block V, or an unmapped location sitting in To Map, is the system **working** — only raise a failure when the Expected behaviour didn't happen.
