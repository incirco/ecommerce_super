# §12 B2C / D2C / Marketplace Sales — Completion Checklist

State map for §12 Phase 1 as of 2026-06-29. Mirrors the format of
`SECTION_11_COMPLETION_CHECKLIST.md` / `SECTION_9_COMPLETION_CHECKLIST.md`
/ `SECTION_10_COMPLETION_CHECKLIST.md`.

## Status: ✅ §12 PHASE 1 BUILD COMPLETE · ⏳ LIVE-SMOKE PENDING · ✅ CLOSEOUT DONE

Phase 1 covers the §12 spec from §12.0 (canonical EE order hierarchy)
through §12.11 (recon enablement) with six by-design deviations
captured in `SPEC_12_patch_notes.md`. Builds the manifest-driven SI
pull flow end-to-end: polling cron → per-order SI creation → recon
bridge (settlement state on SI Custom Fields) → variance alerts.

## Build stages — ALL DONE

| Stage | What | Status | Commit(s) / PR |
|---|---|---|---|
| 1 | Substrate — `EasyEcom Marketplace Account` DocType + 9 B2C Custom Fields on SI + `after_insert` pseudo-customer bootstrap | ✅ committed | PR #107 |
| 2 | Polling walker — `flows/b2c_sales/polling.py` (`reconcile_all_marketplace_accounts` scheduler entry, per-Account `getAllOrders` walker, cursor advance, idempotency on EE Invoice_id) | ✅ committed | PR #107 |
| 3 | SI builder — `flows/b2c_sales/invoice_builder.py` (pool customer resolution, Item Map lookup, EE-tax-wins per Path 2, ERPNext tax cross-check, Sync Record audit, Discrepancy raising) | ✅ committed | PR #107 |
| 4 | Refactor — drop Marketplace Order Map DocType + split pseudo_customer into in-state / out-of-state (GST tax-split fix) | ✅ committed | PR #107 commit `1153070` |
| 5 | §12.9 1-paisa total variance follow-up — `_check_total_variance` + 6 tests | ✅ committed | PR #108 |
| 6 | Closeout artifacts — patch notes, this checklist, primer cross-refs | ✅ committed | PR #109 |
| 7 | Harmony live smoke + 11 fix-forward findings (tax category names, leaf-group filtering, location_key resolution, suborders field, marketplace_id + order_type_key guards, cursor advance, ERPNext v16 schema, SoT Map field type, flat-address state, total_amount field) | ✅ committed | PR #110 |
| 8 | Patch notes for the 4 EE-contract grounding corrections from smoke (notes 7-10) + live-smoke status update on this checklist | ✅ committed | this PR |

## Closeout artifacts — ALL SHIPPED

- ✅ `spec_sections/SPEC_12_patch_notes.md` (10 entries — 6 original
  by-design deviations + 4 EE-contract grounding corrections from
  the Harmony smoke; methodology fold-back reference)
- ✅ `spec_sections/SECTION_12_COMPLETION_CHECKLIST.md` (this file)
- ✅ `process/primers/FDE_PRIMER_section_12_b2c_marketplace.md` (FDE
  setup checklist, Path 2 tax model, failure-modes table, explicit
  non-goals)
- ✅ §11 primer cross-references updated to point at §12 sub-primer
- ⏳ docx regen — USER runs the unpack/edit/pack pipeline locally
  against the patched SPEC.md once methodology folds the patch notes

## Tests at merge

- **75 unit tests, all green** (0 regressions on §11):
  - 18 — `test_b2c_marketplace_account_bootstrap.py` (2-customer
    creation, tax_category resolution, naming fallbacks, edge cases)
  - 44 — `test_b2c_invoice_builder.py` (pool resolution, state helpers,
    GSTIN code mapping, Item Map resolution, tax variance check, total
    variance check, posting date / payload hash helpers)
  - 13 — `test_b2c_polling.py` (response shape extraction, per-order
    idempotency, per-record failure isolation, graceful Stage 3 absence
    handling)
- Run via `bench --site <site> run-tests --app ecommerce_super
  --module ecommerce_super.tests.unit.test_b2c_*`

## Live-verified state

✅ **Smoked end-to-end against Harmony on 2026-06-29 (PR #110).**

The smoke surfaced **11 fix-forward issues** between the unit-
tested code and real bench / real EE payloads. All 11 fixed in
PR #110; SI creation now works end-to-end.

**What ran:**
- One `EasyEcom Marketplace Account` created for `_Test Company` ×
  marketplace_id=10 (`Offline`, the only non-B2B marketplace with
  data on Harmony — see note below)
- Both pseudo-Customers auto-bootstrapped with correct tax
  categories (In-State, Out-State)
- 5 polling iterations across a 35-day historical window pulled
  54 orders total: 51 correctly skipped (B2B / STN on marketplace
  64), 3 retailorder rows on marketplace_id=10 → Sales Invoices
- Three Draft Sales Invoices minted (SINV-26-00009 / 00010 / 00011)
  with all 13 `ecs_*` Custom Fields populated
- Three EasyEcom Sync Records written (direction=Pull, entity_type=
  Sales Invoice, status=Success, correlation_ids threaded through)
- Path 2 tax check ran (0.0% variance on all 3 since HSN rates
  aren't configured on test items — see Watch-items below)
- §12.9 1-paisa total variance check ran (0/0/1 paise — all within
  tolerance)

**What was NOT covered** (bench-data limitation, not code gap):
- Out-of-state pool selection — Company.state is unset on the test
  bench, so `_resolve_company_state` returns None and the resolver
  defaults to in-state pool (safer per spec). Would need
  Company.state set OR a different bench to verify out-of-state
  path.
- ERPNext tax cross-check (`ecs_erpnext_tax_check_total`) returned
  0.0 — no HSN configured on test items + no `GST HSN Code.tax_rate`
  populated. Would need a properly-configured Item + IC config.
- Actual marketplace B2C orders (Amazon / Flipkart / Myntra) —
  Harmony has none. The smoke used marketplace_id=10 (Offline /
  retailorder) as a proxy because it was the only non-B2B
  marketplace with historical order data.

**Recommended next smoke** (before first real client deployment):
1. Get a teammate or vendor to push a real Amazon test order to
   Harmony (manifest it on EE side)
2. Set `Company.state` on the test Company so the in-state /
   out-of-state pool routing exercises both paths
3. Configure HSN rates on the test items via India Compliance so
   the variance check fires meaningfully

## Deferred PAST Phase 1 — by design

Validation surfaced four functional gaps + several Phase 2 items.
Each is intentionally not built, with rationale:

**Functional gaps from spec §12 (deferred):**
- **§12.4 line 2774-2775: billing / shipping address on SI** —
  Phase 1 captures shipping state via the pool-customer resolution;
  full address fields not populated on the SI. Revisit if FDE needs
  buyer-address visibility on the SI form (likely yes for ops; small
  patch).
- **§12.4 line 2779: line-level `discount_amount`** — Phase 1 derives
  per-line `rate` from EE's `breakup_types["Item Amount Excluding
  Tax"] / qty`, so discount is implicitly baked into the rate. Spec
  wants explicit `items[].discount_amount`. Reporting / audit
  nicety; not a recon blocker.
- **§12.9 line 2824: unknown `marketplace_id` → per-record Failed
  Sync Record** — Phase 1 guards this at FDE setup time (Marketplace
  Account creation requires an existing Marketplace row), so runtime
  shouldn't see unknown marketplace_ids. If it does, the SI build
  fails generically; the per-record Failed Sync Record pattern would
  give a cleaner FDE diagnostic.
- **§12.7 line 2808: multi-warehouse per-line split** — One SI with
  multiple lines drawing from different warehouses. Spec describes as
  "rare in B2C but possible"; Phase 1 resolves a single warehouse per
  SI. Surfaces only with real client need.

**Phase 2 items (consistent with §11 Phase 1 deferral pattern):**
- **EE webhook receivers** (`manifested`, `ready_to_dispatch`,
  `inventory.reserved`) — polling is the recovery path across all
  flows; webhook plug-ins are Phase 2+ across §11 and §12.
- **e-invoice IRN minting from ERPNext** — per locked decision
  (patch note 4), the marketplace or EE owns IRN. If a future
  client needs us to mint IRN for B2C, build as a per-Marketplace-
  Account toggle (mirror §11.5.1's `gsp_mint_einvoice` pattern).
- **History-aware polling derivation** — walking EE's
  `easyecom_order_history` for richer state-transition detection.
  Cross-cuts §11 and §12; same deferral as §11.

## Watch-items — unit-verified, NOT live-smoked

- **Item Map lookup pattern** (`EasyEcom Item Map.ee_sku →
  erpnext_name`) — works in §11.5.2 mirror; reused verbatim here.
  First B2C client whose SKUs differ from §11 client base should
  re-verify the mapping.
- **HSN default rate lookup** (`GST HSN Code.tax_rate`) — India
  Compliance versions vary on this field's presence. v1 falls back
  to 0 (skipping the variance signal) when the field is missing;
  worth a sanity check on each new client's IC version.
- **GSTIN state-code map** — Embedded in `invoice_builder.py`
  (`_gstin_state_code_to_name`). Covers Indian state codes per GST
  Council. Hasn't been smoked against unusual GSTINs (UT, Centre
  Jurisdiction codes 97/99).

## What's next

**§12 Phase 1 is complete pending live smoke.** Next focus:

1. **Live Harmony smoke** — single highest-leverage verification; see
   "Live-verified state" section above for the script.
2. **Methodology fold-back** — `SPEC_12_patch_notes.md` (this build)
   needs methodology team to rewrite `SPEC.md §12` to match. The
   immediate downstream consumer (§102 backfill) should start from a
   patched spec.
3. **§102 B2C Order Backfill** — was strict-blocked on §12 per its
   draft packet; **now unblocked.** Issue #96 / #97 follow-ups.
4. **Recon engine work** (PRD §10.3 — Settlement Forecast,
   Net Receivables) — `ecs_settlement_status` and
   `ecs_marketplace_order_id` ready to consume; Marketplace Account
   ready to extend with settlement_template / rate_card_subscriptions
   fields (deferred per patch note 1).
5. **Close issue #97** (§12 tracking issue) — closed by PR #107
   merge; flag for housekeeping.

## Primers / operating manuals shipped

| Doc | Covers | Audience |
|---|---|---|
| `process/primers/FDE_PRIMER_section_12_b2c_marketplace.md` | §12 setup, Path 2 tax, pool-customer model, failure modes, non-goals | FDE / Ops |
| `process/primers/FDE_PRIMER_section_11_b2b_sales.md` | §11 baseline (Phase 1) — separate flow, shared substrate (polling cron, EE client) | FDE |
| `SPEC_12_patch_notes.md` | 6 by-design deviations to fold back into SPEC.md | Methodology lead |
| `SECTION_12_COMPLETION_CHECKLIST.md` (this file) | Build state, test coverage, deferred items, what's next | All |
