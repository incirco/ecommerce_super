# Section 8c — Tax (EasyEcom Tax Rule → Item Tax Template mapping) — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the third master: the **EasyEcom Tax Rule Map** (FDE-configured, per (rule, company)), the **Test Resolve** verification UI, the resolver behaviour, and the To Configure → Configured workflow. Derived from `docs/SPEC.md` §8.5 and the 8c packet.

> **First time?** Read `HOW_TO_RUN_FDE_TESTS.md`, then the masters primer (`../primers/FDE_PRIMER_section_8_masters.md`, Part H) — it explains why EasyEcom tax rule names are meaningless labels and how the mapping mirrors EasyEcom's Tax Master.

> **The model in one line.** EasyEcom attaches a tax *rule* (an opaque name) to each product; you map each rule, per company, to the matching ERPNext Item Tax Template row(s) — one row for a flat rule, multiple banded rows (Min/Max Net Rate) for a price-slab rule. ERPNext resolves the band natively at invoice time. There is **no tax pull** — this is configuration.

> **Not a pull.** Unlike Location/Channel there is no "Discover" button — EasyEcom has no tax API. You read each rule from EasyEcom's Tax Master (Masters >> Tax Master) and reproduce it here. The resolver is exercised by Item sync (next master); here you verify it via **Test Resolve**.

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` installed on staging, migrated clean
- [ ] System Manager + an **EasyEcom FDE** user; plus a no-role user (negative role-gating)
- [ ] A Company exists with India Compliance GST Item Tax Templates present (e.g. GST 5/12/18/28% - {abbr}, Exempted)
- [ ] You can view the sandbox's EasyEcom Tax Master (to read real rule definitions), or use the example rules below

### Steps — Create a mapping (flat rule)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| T1 | New EasyEcom Tax Rule Map: tax_rule_name e.g. "5", pick a company | Saves; lands in workflow state **To Configure**; unique on (tax_rule_name, company) | ☐ P ☐ F |
| T2 | Add one Taxes row: a GST 5% template, leave Min/Max Net Rate blank | Row saves (a flat rule = one row, no bands) | ☐ P ☐ F |
| T3 | Try to **Configure** (workflow) | Allowed now that the Taxes table is non-empty | ☐ P ☐ F |
| T4 | Create another map but try to **Configure** with an empty Taxes table | Blocked — Configure is gated on at least one Taxes row | ☐ P ☐ F |

### Steps — Create a mapping (slab rule) + Test Resolve

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| S1 | New map: tax_rule_name "GST", a company; add two Taxes rows — GST 5% [Min 0, Max 2500] and GST 18% [Min 2500, Max blank] | Both banded rows save | ☐ P ☐ F |
| S2 | Click **Test Resolve** (toolbar; appears after save) | Dialog opens with sample tax_rate + sample cess inputs | ☐ P ☐ F |
| S3 | Enter rate 0.05, Resolve | Result pane: rows-to-stamp shows both bands; **green / reconciled** (0.05 is the low band's rate) | ☐ P ☐ F |
| S4 | Enter rate 0.18, Resolve | **Green / reconciled** (matches the high band) | ☐ P ☐ F |
| S5 | Enter rate 0.12, Resolve | **Red / discrepancy** — 12% is not one of the mapped bands; the message states the rate-vs-bands mismatch | ☐ P ☐ F |
| S6 | Leave rate blank, Resolve | **Grey** — preview-only, reconciliation skipped (shows which rows would stamp, no pass/fail) | ☐ P ☐ F |
| S7 | Enter a cess value, Resolve | The cess that would apply to the item is shown | ☐ P ☐ F |
| S8 | Change the rate and Resolve again without closing | Dialog stays open; re-resolves against the same map | ☐ P ☐ F |

> **What Test Resolve guarantees:** it runs the *same* logic the real Item sync uses (shared pure functions), so what you see here is exactly what products will get. A green here means the live sync will reconcile; a red means it would raise a discrepancy.

### Steps — Workflow + role gating

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| W1 | Filter Tax Rule Maps to workflow_state = To Configure | Your worklist of rules awaiting setup | ☐ P ☐ F |
| W2 | Configure a fully-set-up map → then **Mark Ignored** on an irrelevant rule | Configured / Ignored states reachable; reverse transitions exist | ☐ P ☐ F |
| W3 | As the no-role user, open a map | Workflow action buttons not available — transitions are FDE-gated (System Manager inherits) | ☐ P ☐ F |

### Steps — Resolver safety (verify via Test Resolve / reasoning; full live path is exercised by Item sync)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| R1 | On a map whose Taxes table is empty, Test Resolve with a rate | **Orange / empty** — nothing to stamp; flagged, not a silent pass | ☐ P ☐ F |
| R2 | (Confirm by reasoning) a product whose tax_rule_name has no map for its company | When Item sync runs, it will auto-create a To Configure map for that (rule, company) and alert you — never a silent default. (Fully testable once the Item master is live.) | ☐ P ☐ F |

> **Known deferred (do NOT raise as failures):** there is **no tax pull / Discover button** — by design (EasyEcom has no tax API). The resolver's full live behaviour (stamping onto real items, auto-creating To Configure on unmapped rules during a real sync) is exercised by the **Item master (next)** — here you verify the mapping + Test Resolve. CESS is per-product (not configured in the map). 8c does not create Item Tax Templates — you select from India Compliance's existing ones.

### Overall result
- [ ] **PASS** — every applicable step passed (deferred items excluded)
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number and the Expected cell. Remember: a Configure blocked on an empty Taxes table, a red discrepancy in Test Resolve for an out-of-band rate, an empty-map orange flag, and an unmapped rule auto-creating a To Configure task are all the system **working** — only raise a failure when the Expected behaviour didn't happen (e.g. Test Resolve shows green for a rate that isn't in any band, or a duplicate (rule, company) is allowed).
