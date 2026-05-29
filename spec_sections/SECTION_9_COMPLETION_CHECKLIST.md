# §9 Buying / GRN — Completion Checklist

*State map for §9 as of 2026-05-29. CLOSEOUT COMPLETE. Lives in spec_sections/ alongside the §9 packet.*

## Status: ✅ §9 BUILD COMPLETE · LIVE-VERIFIED · CLOSEOUT DONE

## Build stages — ALL DONE

| Stage | What | Status | Commit(s) |
| --- | --- | --- | --- |
| 1 | Substrate (PO Map, GRN Map, Sync Record Line, settings, ruleset repoints) | ✅ committed | `18fcc77` |
| — | §23 Integration Discrepancy stub (frozen-contract, unblocks Stage 3 link) | ✅ committed | `0090a32` |
| 2 | PO push — content (CreatePurchaseOrder) + status (updatePoStatus) channels | ✅ committed | `5851e4b` |
| 3 | GRN pull → Purchase Receipt + status reconciliation | ✅ committed, live-verified (5 Harmony rounds) | `df8464f`…`e041bbb` |
| 4 | UI / workspace / scheduler + hardening (test-isolation, address precondition) | ✅ committed | Stage 4 commit |
| Corrective | Unknown-PO drift + pause-respects-all-three + adjacent pause-gap fix | ✅ committed + 2 re-smokes clean | Corrective commit |

## Closeout artifacts — ALL SHIPPED

- ✅ `process/primers/FDE_PRIMER_section_9_buying.md` (Parts A–L)
- ✅ `process/test_scripts/section_9_buying.md` (8 sections + What-passing-means)
- ✅ `spec_sections/SPEC_9_patch_notes.md` (rewrites stale SPEC.md §9.1–§9.12)
- ✅ `process/BUILD_TRACKER.md` (§9 closeout entry)
- ⏳ docx regen (USER runs unpack/edit/pack pipeline locally against patched SPEC.md)

## Live-verified on Harmony

5 Stage-3 smoke rounds + 2 corrective re-smokes. All paths confirmed including: PO push (content + status), real WMS GRN → PR with qc_fail split, native purchase_order_item linkage, idempotency back-ref, tax variance check, Held-Pre-QC transition, completion echo, echo-not-drift, unknown-PO drift + FDE create-PR + dismiss, pause-defer all three po_status pushes.

## Deferred PAST §9 — by design

- **GRN-pull cron stays UNWIRED until go-live** sets `grn_pull_high_watermark`, then wire it. Go-live runbook step. Auto-firing on NULL watermark would drag in EE's 7-day backstop (cold-start hazard). Handler is built + smoke-proven. Guard test: `TestGRNPullSchedulerIntentionallyUnwired`.
- **STN routing live-verification = §10 PREREQUISITE.** Required before §10 Stage 3.

## Watch-item — unit-verified, NOT live-smoked

- **Multi-GRN partial cumulative tolerance** — only full receipts smoked in Stage 3. Flagged in `section_9_buying.md` test script for first real client with a partial receipt.

## §8d standing failures (carried since §8, unrelated to §9)
- `test_item_pull_stage2`, `test_item_lifecycle_drift_stage5` — pre-existing, tracked, untouched by §9. Part of the broader ~24 pre-existing §8 failures.

## What's next

**§10 Stock Transfer Flows.** Packet at `spec_sections/section_10_stock_transfer_packet.md`. Stage 1 prompt: `section_10_stage1_substrate_prompt.md` (in outputs).
