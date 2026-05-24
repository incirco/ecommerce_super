# Section 5 — Field Mapping Engine — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the Field Mapping engine: the DocTypes, the translation engine (Test Mapping), the expression security sandbox, compile-time validation, versioning/rollback, and the FDE editing surface. Derived from the acceptance requirements in `docs/SPEC.md` §5 (especially §5.9 execution semantics and the packet's SECURITY section).

> **First time running these?** Read `HOW_TO_RUN_FDE_TESTS.md` (same folder) — it explains setup, where everything is, how to execute this script, and how to raise issues on failure.

> **Important — read before the Test Mapping block.** This engine only *translates* between shapes; it does not yet sync anything to EasyEcom (the flows that consume it are Part III, not built). When you run Test Mapping against real EasyEcom payloads, a **"required field produced no value" error is usually NOT an engine bug** — it almost always means the shipped ruleset's field paths don't match the real EE payload, or EE simply doesn't supply a field ERPNext requires. That's a *ruleset-tuning finding* for the relevant flow section (e.g. §8 for Item), not a §5 engine defect. See the dedicated block below for how to tell the difference.

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` app installed on the staging site, migrated clean
- [ ] You have System Manager access on the staging site
- [ ] You have access to a user with the **EasyEcom FDE** role but **not** System Manager (for the permissions spot-check)
- [ ] A real EasyEcom payload sample on hand for at least one entity (e.g. a single product object from `/Products/GetProductMaster`) — for the real-payload Test Mapping block

### Steps — DocTypes & fixture library exist

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| L1 | Open the EasyEcom Field Mapping list | The shipped fixture rulesets are present (≈23): Item-Sync, Item-UOM-Sync, Item-Alias-Sync, Customer-Sync, Customer-Anon-Pull, Supplier-Sync, Warehouse-Pull, Tax-Category-Sync, Channel-Pull, PO-Push, GRN-Pull, SO-Push, Order-Pull, etc. | ☐ P ☐ F |
| L2 | Open EasyEcom-Item-Sync | Rules table populated; entity_type Item, direction Bidirectional; readable rules with erpnext_path / easyecom_path / transforms | ☐ P ☐ F |
| L3 | Confirm the child DocTypes exist | EasyEcom Field Mapping Rule, EasyEcom Computed Field, EasyEcom Field Mapping Version all present in the DocType list | ☐ P ☐ F |
| L4 | Open 2–3 different rulesets (an Item one, a Customer one, Channel-Pull) | All open intact, no broken/empty rulesets; Channel-Pull references the flat Marketplace doctype (not a Marketplace-Channel hierarchy) | ☐ P ☐ F |

### Steps — Test Mapping engine (core translation)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| T1 | Open any ruleset → Test Mapping action; paste a small valid sample for that direction; run | Returns translated output **plus a per-rule trace** showing what each rule did | ☐ P ☐ F |
| T2 | Run Test Mapping with a sample where an optional field is absent (ruleset in Permissive mode) | Engine completes; absent optional field handled by identity-default / omitted, not a crash | ☐ P ☐ F |
| T3 | Run Test Mapping with array iteration (a sample with a child array, e.g. items[] / sub_products) | The per-row rule applies to each element; trace shows per-element application | ☐ P ☐ F |
| T4 | Show Computed Mapping action on a ruleset | Expands implicit identity matches inline — you see the full *effective* mapping, not just the explicit rules | ☐ P ☐ F |

### Steps — Test Mapping against REAL EasyEcom payloads (read the note at top)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| R1 | Paste a **single** real product object (one element from `data[]`, NOT the whole `{data:[...], nextUrl:...}` envelope) into Item-Sync Test Mapping, direction Pull | Engine reads it and maps the fields it can. NOTE: pasting the envelope instead of a single object will (correctly) map nothing — the products are nested under `data[]` | ☐ P ☐ F |
| R2 | Observe the result | One of two valid outcomes: (a) full translated output + trace, OR (b) a precise "Required rule N produced no value at '<field>'" error naming the missing field. **Both are correct engine behaviour.** | ☐ P ☐ F |
| R3 | If R2 gave a required-field error: judge what it means | If the missing field genuinely isn't in the EE payload, or the ruleset's easyecom_path doesn't match the real EE field name → this is a **ruleset-tuning finding for the flow section (e.g. §8 Item)**, NOT a §5 engine bug. Raise it as a finding tagged for that section, attaching the real payload. Do NOT raise it as a §5 engine failure. | ☐ P ☐ F |
| R4 | Confirm the error is precise and safe | The error names the rule number and target field, and the engine aborts cleanly (no partial garbage record, no silent default of a tax-relevant field) | ☐ P ☐ F |

### Steps — Expression security sandbox (highest priority — test by hand)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| S1 | Create a new Field Mapping; add a rule with condition `__import__('os').system('ls')`; try to **Save** | **Rejected at save** with a clear error pointing at the offending rule. Must NOT save. | ☐ P ☐ F |
| S2 | Add a condition `source_doc.__class__.__mro__[1].__subclasses__()`; try to Save | Rejected at save (dunder traversal blocked) | ☐ P ☐ F |
| S3 | Add a computed field with expression `open('/etc/passwd').read()`; try to Save | Rejected at save | ☐ P ☐ F |
| S4 | Add a condition referencing a disallowed name (e.g. `frappe`, `os`, `eval`); try to Save | Rejected at save — only documented names (source_doc, source_payload, get_path/sum_path/filter_path, value) are allowed | ☐ P ☐ F |
| S5 | Add a valid condition `source_doc.customer_type == 'B2B'`; Save | Saves successfully — legitimate expressions using allowed names work | ☐ P ☐ F |

### Steps — Compile-time validation

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| C1 | Add a rule with a malformed path (e.g. `items[?.bad` or `items[`); try to Save | Rejected at save with a path-syntax error naming the rule | ☐ P ☐ F |
| C2 | Add a rule with a nonexistent transformer name; try to Save | Rejected at save (transformer name doesn't resolve) | ☐ P ☐ F |
| C3 | Create a circular composition (ruleset A composes B, B composes A) or exceed max-depth 5; try to Save | Rejected at save (cycle / depth-limit error) | ☐ P ☐ F |
| C4 | Reference a computed field name in a rule that isn't declared in the Computed Fields table; try to Save | Rejected at save (unresolved computed reference) | ☐ P ☐ F |

### Steps — Versioning & rollback (§5.12)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| V1 | Open a ruleset, edit a rule's notes, Save with a change reason | A new EasyEcom Field Mapping Version snapshot row is created; version increments | ☐ P ☐ F |
| V2 | Diff Against Version action; pick the prior version | Shows added (green) / removed (red) / modified (yellow) rules | ☐ P ☐ F |
| V3 | Rollback action; pick a prior version; preview; confirm | Ruleset restored to that version; the rollback itself creates a NEW version with reason "Rollback to v<n>", author = you | ☐ P ☐ F |
| V4 | Confirm change_reason is enforced | Saving without a change reason is blocked (required on save) | ☐ P ☐ F |

### Steps — FDE editing surface (§5.10)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| U1 | List view columns | Mapping Name, Entity Type, Direction, Active, Version, Last Modified By visible; filter by Entity Type / Direction / Active works | ☐ P ☐ F |
| U2 | Bulk action: select 2 rulesets → Deactivate, then Activate | Active flag toggles on both | ☐ P ☐ F |
| U3 | Export a ruleset to JSON, then re-import / confirm round-trip | Export produces valid JSON that represents the ruleset faithfully | ☐ P ☐ F |
| U4 | Rule editor: transformer dropdown + transform_args | Dropdown lists the closed vocabulary; transform_args validates against the selected transformer's schema | ☐ P ☐ F |

> **Known deferred (do NOT raise as failures):** path **autocomplete** in the rule editor (§5.10.3) is not built — server-side compile rejection (block C) is the safety net. **Import-from-JSON** as a list action is deferred — use Frappe's standard data import; Export is wired. **Coverage History tab** depends on §26 (not built). **Configuration Audit** row on save depends on §28 (not built) — the Version snapshot is the §5-level audit.

### Overall result
- [ ] **PASS** — every step passed (deferred items above excluded)
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary | §5 engine bug, or ruleset/flow finding? |
| --- | --- | --- | --- |
|  |  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number, the Expected cell, what you saw, and a screenshot. **For Test Mapping required-field errors (block R): first decide whether it's an engine bug or a ruleset-tuning finding** (see the note at top and step R3) and tag the issue accordingly — ruleset findings go to the owning flow section (e.g. §8 Item), not against §5.
