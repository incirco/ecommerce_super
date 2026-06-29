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
| 6 | Closeout artifacts — patch notes, this checklist, primer cross-refs | ✅ committed | this PR |

## Closeout artifacts — ALL SHIPPED

- ✅ `spec_sections/SPEC_12_patch_notes.md` (6 by-design deviations
  documented for methodology fold-back)
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

⏳ **Not yet live-smoked against Harmony.** Unit tests cover the
internal logic exhaustively but EE-payload-shape assumptions (field
names, breakup math precision, address-state field placement) need
real payloads to validate. The §12 build is "feature-complete pending
live smoke".

**Recommended live smoke** (before first client deployment):
1. Create one `EasyEcom Marketplace Account` row for Harmony's Amazon
   marketplace + Acme test Company; verify both pseudo-Customers
   auto-create
2. Trigger an Amazon order to manifest on Harmony; wait one polling
   tick (~5 min)
3. Verify in ERPNext: a Draft Sales Invoice exists with all 13
   `ecs_*` Custom Fields populated; correct pool customer chosen
   (matches buyer's shipping state); SI.taxes carries EE-supplied
   tax; `ecs_erpnext_tax_check_total` is computed
4. Verify the EE payload landed in an EasyEcom Sync Record row
   (`direction = Pull`, `entity_type = Sales Invoice`)
5. Smoke the variance paths: deliberately set a wrong HSN rate to
   force tax-variance Discrepancy; check the Integration Discrepancy
   list

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
