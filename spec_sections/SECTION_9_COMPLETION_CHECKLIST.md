# §9 Buying / GRN — Completion Checklist

*State map for §9 as of 2026-05-28. Lives in spec_sections/ alongside the §9 packet. Tells anyone picking this up exactly what's done, what's pending, and what's deferred past §9.*

## Build stages — ALL DONE

| Stage | What | Status | Commit(s) |
| --- | --- | --- | --- |
| 1 | Substrate (PO Map, GRN Map, Sync Record Line, settings, ruleset repoints) | ✅ committed | `18fcc77` |
| — | §23 Integration Discrepancy stub (frozen-contract, unblocks Stage 3 link) | ✅ committed | `0090a32` |
| 2 | PO push — content (CreatePurchaseOrder) + status (updatePoStatus) channels | ✅ committed | `5851e4b` |
| 3 | GRN pull → Purchase Receipt + status reconciliation | ✅ committed, live-verified (5 Harmony rounds) | `df8464f`…`e041bbb` |
| 4 | UI / workspace / scheduler + hardening (test-isolation, address precondition) | ✅ reported done | (Stage 4 commit) |

**No fifth build stage. §9 is structurally complete.**

## Outstanding before §9 closeout

### 1. Corrective commit (prompt: section_9_corrective_commit_prompt.md) — NOT a new stage
- **Fix 1 (high, stock-affecting):** unknown-PO GRN → DRIFT (no auto-PR). Resolution is FDE-driven, ERPNext-side only: "Create PR from this GRN" action builds a STANDALONE PR (no purchase_order link by default; FDE may optionally link an existing PO). GRN data preserved on the Map row. NO PO ever created or pushed to EE. Supersedes the Stage-3-coded behaviour (auto-PR then Discrepancy).
- **Fix 2 (lower, pause-contract):** completion push (po_status=5) respects pause_all_auto_push — defers when paused, fires on un-pause.

### 2. Harmony re-smokes (both fixed paths were live-verified in their OLD behaviour)
- [ ] Unknown-PO GRN → confirm NO auto-PR + drift → invoke "Create PR from this GRN" → confirm standalone PR with correct qty/qc_fail split.
- [ ] Completion-push pause-defer → confirm no po_status=5 sent during pause, fires on un-pause.

### 3. Closeout artifacts
- [ ] `process/primers/FDE_PRIMER_section_9_buying.md` (own primer — §9 is a flow, not a master)
- [ ] `process/test_scripts/section_9_buying.md`
- [ ] `spec_sections/SPEC_9_patch_notes.md` (rewrites the stale SPEC.md §9.1–§9.12 with payload-grounded reality + all corrections)
- [ ] BUILD_TRACKER §9 entry (design → build → live-verified, with carry-forwards)
- [ ] docx regen from SPEC.md

## Deferred PAST §9 — by design, not balance work

- **GRN-pull cron stays UNWIRED until go-live** sets `grn_pull_high_watermark`, then wire it. Go-live runbook step. Auto-firing on a NULL watermark would drag in EE's 7-day backstop (cold-start hazard). Handler is built + smoke-proven; only auto-fire is gated. Guard test: `TestGRNPullSchedulerIntentionallyUnwired`.
- **STN routing live-verification = §10 PREREQUISITE.** The `vendor_c_id == inwarded_warehouse_c_id` check is code-correct but never fired on real data (no self-GRN sample on this tenant; no vendor maps to any known company_id). Before §10: FDE triggers a real self-GRN on Harmony (internal inward / opening-stock on a mapped WH), inspects `vendor_c_id`; if it equals that location's company_id → check correct, §10 builds on it; if not → adjust before §10.

## Watch-item — unit-verified, NOT live-smoked

- **Multi-GRN partial cumulative tolerance** — only full receipts smoked in Stage 3. The cumulative path (GRN1 receives 6 of 10, GRN2 receives 4) is unit-verified but unexercised against real EE. Not a blocker. Closeout test script must flag it as a watch-item for the first real partial receipt on a client.

## §8d standing failures (carried since §8, unrelated to §9)
- `test_item_pull_stage2`, `test_item_lifecycle_drift_stage5` — pre-existing, tracked, untouched by §9. (Part of the broader ~24 pre-existing §8 failures confirmed stash-verified as not §9-caused.)
