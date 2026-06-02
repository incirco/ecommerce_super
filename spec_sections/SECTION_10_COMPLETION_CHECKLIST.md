# §10 Stock Transfer Flows — Completion Checklist

*State map for §10 as of 2026-06-01. CLOSEOUT COMPLETE + LIVE-VERIFIED + CORRECTIVE COMMIT LANDED. Lives in spec_sections/ alongside the §10 packet.*

## Status: ✅ §10 ACTUALLY CLOSED · LIVE-VERIFIED · CORRECTIVE COMMIT IN

## Build stages — ALL DONE

| Stage | What | Status | Tests |
| --- | --- | --- | --- |
| 1 | Substrate (Transfer Map + IPR Link child + Internal pair machinery + precheck + settings + endpoint constant + back-refs) | ✅ committed | 40/40 |
| 2 | Outbound (DN submit hook + Gate-0 + preconditions + SI auto-draft + STN/PO push routing + pause-defer + cancel/amend stubs) | ✅ committed | 12/12 |
| 3 | Inbound (§9 routing handoff + IPR auto-create + submit gate + IPI/DN auto-draft + multi-GRN cumulative + EE-originated standalone + test-isolation hardening) | ✅ committed | 6/7 (1 documented skip) + 3 isolation guard |
| 4 | Variance/UI/Closeout-items (audit Comment fix + PO-branch wire dispatch + aged GIT cron + list view + §17 cards + form polish + Sync Record filter + status correction) | ✅ committed | 16/16 |
| Live Smoke (2026-06-01) | Case C closed (B2B branch grounded) + 2 latent bug fixes (SI back-link, TM status transition) + Warehouse EE-mapping UX layer | ✅ committed (`cd27d0f` + `cc73de6`) | live-verified on Harmony |

**§10 total:** 61 unit tests + 1 skip + 4 live integration smokes (DN-26-00037, 39, 40 fully clean; DN-26-00036 discovery-phase artifact).

## §10 Decision Matrix — fully grounded (4 branches)

| Source EE-mapped? | Target EE-mapped? | Branch | EE primitive |
| --- | --- | --- | --- |
| ❌ | ❌ | Inert | (no EE call) |
| ✅ | ✅ | STN | `createOrder · orderType=stocktransferorder` |
| ❌ | ✅ | PO | `CreatePurchaseOrder` |
| ✅ | ❌ | **B2B** (Case C — closed 2026-06-01) | `createOrder · orderType=businessorder` |

## Closeout artifacts — ALL SHIPPED + AMENDED

- ✅ `process/primers/FDE_PRIMER_section_10_stock_transfer.md` (Parts A-L + new Part M + historical notes in Part E)
- ✅ `process/test_scripts/section_10_stock_transfer.md` (now 9 sections including new Section 3.5 B2B branch + Section 9 UX surfaces + amended load-bearing checks list)
- ✅ `spec_sections/SPEC_10_patch_notes.md` (original patch notes + 2026-06-01 amendment appendix: B2B branch, EE validation order surprise, SI back-link invariant, status transition fix, UX surface)
- ✅ `process/BUILD_TRACKER.md` (§10 closeout entry + §10 Live Integration Smoke entry appended)
- ⏳ docx regen (USER runs unpack/edit/pack pipeline locally against patched SPEC.md)

## Locked design decisions (recorded for transparency)

- N+N Internal Customer/Supplier cardinality (corrected from packet's N×(N−1) at Stage 1)
- EE-Pushed-but-SI-Pending status overload (ee_order_id disambiguates)
- `ecs_pending_ee_push` for pause-deferred outbound (distinct from §9's multi-state pending)
- PO-branch source-vendor resolution chain (refuse-with-Drift if unresolvable; no EE Vendor auto-creation)
- **B2B branch ee_doctype enum** (added 2026-06-01); orderType=`businessorder`; customerId=wholesale c_id from `/Wholesale/CreateCustomer`
- **SI back-link invariant**: `ecs_section10_transfer_map` MUST be set before SI save (the load-bearing fix of 2026-06-01)
- **TM status transition independent of IPR state** on SI submit (the second 2026-06-01 fix)
- Cancel/amend stub-blockers with explicit user-facing error
- EE-originated standalone = option (ii) — Discrepancy + FDE-driven resolution via §9 action
- Audit Comment lives on Transfer Map (survives DN deletion)
- Submitted-DN-late-GRN placement on §17 FDE Worklist
- Aged GIT idempotency via description-substring matching

## Live-verification status

- ✅ **STN payload contract** — grounded against live Harmony round-trip 2026-05-29.
- ✅ **B2B payload contract** — grounded against live Harmony round-trip 2026-06-01 (DN-26-00040 clean, all three EE IDs captured).
- ✅ **SI back-link end-to-end** — DN-26-00040 confirmed clean (no manual intervention required after bug fix).
- ✅ **TM status auto-transition** — DN-26-00040 reached `EE-Pushed` automatically on SI submit.
- ⏳ **STN self-GRN routing** — still pattern code-correct, mock-tested, NOT live-verified. Was a §9 carry-forward; now overdue.
- ⏳ **Multi-GRN partial cumulative** — unit-verified, not live-smoked.
- ⏳ **PO-branch wire dispatch** — wired against mocks; first non-EE-source-with-EE-target deployment is the live exercise.
- ⏳ **STN cancel/amend endpoint payload** — Stage 2 stub-blocks DN cancel/amend until grounded.

## Carry-forwards past §10 closeout (post-2026-06-01)

| Item | Risk surface | Trigger |
| --- | --- | --- |
| STN self-GRN routing live-verification | §9 self-GRN check assumption | Trigger a real self-GRN; was a §9 carry-forward |
| STN cancel/amend endpoint payload grounding | Stage 2 stub-blocks DN cancel/amend on EE-pushed transfers | First ERP-user cancellation of EE-pushed STN |
| Multi-GRN partial live-smoke | Unit-verified | First real client with multi-receipt scenario |
| PO-branch wire dispatch live-smoke | Wired against mocks | Real non-EE-source-with-EE-target deployment |
| B2B-branch destination GRN flow (purely ERPNext-native by design) | Documentation only | First B2B deployment hits this |
| `_resolve_for_receipt` vs inline §10 resolver divergence | Two code paths | Drift on future §9 fixes |
| `Sales Invoice.on_submit` hook scope guard | §10's hook auto-retries IPRs scoped via back-ref | §11+ adding their own SI hooks |
| Operations Dashboard for ERP users | Deferred per packet | Future polish |
| **Test discipline lesson** (from 2026-06-01) | End-to-end state propagation across submissions | Apply in §11+ test scripts |
| **Future decision matrices must enumerate all quadrants** | "Build complete" assertion | Apply in §11+ design packets |
| **Future orderType grounding uses fresh orderNumbers** | EE validation-order surprise | Apply in §11+ EE-primitive grounding |

## §8d standing failures (carried since §8, unrelated to §10)

24 pre-existing §8 failures + 1 §9 standing failure. Untouched by §10 work. Tracked separately.

## What's next

**§11** — TBD in numbering scheme.
- §10 is no longer pre-live-smoke; the integration smoke is in, corrective is in, primer + test script + SPEC patch notes are amended.
- The §10 → §11 design overlap to plan for: `Sales Invoice.on_submit` hook scope guard pattern (both sections will likely add hooks; must coexist via back-ref scoping).
- §11 packet needs design before parallelization with another agent is viable.
- Apply the new discipline lessons from this corrective: (a) decision-matrix completeness gate, (b) fresh-orderNumber probe convention, (c) end-to-end test discipline for state propagation across documents.
