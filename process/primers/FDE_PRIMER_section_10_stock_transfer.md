# FDE Primer — Section 10: Stock Transfer Flows (operational flow #2)

**Who this is for:** an FDE who has read the foundation primer (`FDE_PRIMER_sections_1_to_7.md`), the masters primer (`FDE_PRIMER_section_8_masters.md`), and §9 Buying (`FDE_PRIMER_section_9_buying.md`). This is the second **operational flow** primer — §10 builds on §9's GRN-pull machinery for inbound, but introduces the Internal Customer/Supplier pattern, auto-Debit-Note, and a new submit-gate invariant.

**Read §9 first.** Specifically Part C (corrected qty model), Part F (unknown-PO drift), and Part G (self-GRN routing — the §9↔§10 boundary). §10's inbound reuses §9's PR-build helpers; its standalone EE-originated path picks up §9's self-GRN routing.

**The model in one line.** ERPNext DN (Internal Customer) → optional auto-drafted SI (different-GSTIN) → EE push (STN or PO branch) → EE GRN-Complete pull → auto-IPR with submit gate → optional auto-IPI + auto-Debit-Note (different-GSTIN). Stock parks in GIT on DN submit; moves GIT→destination on IPR submit.

---

## Part A — Where §10 sits in the integration

§10 is the second operational flow. §9 moved goods *in* (Suppliers → Warehouses). §10 moves goods *between* warehouses — including between different Companies under the same group. This is the integration's stock-transfer business.

The two-level distinction that drives everything in §10:
- **Same GSTIN** (intra-Company *or* inter-Company-same-state-same-GSTIN): pure stock movement. No GL impact beyond inventory cost adjustments. ERPNext doesn't need a Sales Invoice — the goods are still on the same legal entity's books.
- **Different GSTIN** (inter-Company different-state or different-GSTIN-same-state, like SEZ): legal supply between two legal entities. GST applies — output GST on source, input GST credit on destination. ERPNext needs SI (source) + IPI (destination) + optional Debit Note (gap).

The integration creates these documents in **Draft** state. **It never auto-submits financial documents.** ERP users submit; the integration orchestrates. This is the §10 invariant in one sentence.

§10 is **opt-in by warehouse**. Every DN is checked at Gate 0: is it an Internal-Customer DN, and is at least one of source/target an EE-mapped warehouse? If not, the integration is silently inert — exactly like §9's Gate-0 invariant for non-EE warehouses.

## Part B — The Internal Customer / Internal Supplier pattern (N+N model)

ERPNext models inter-Company transactions via Internal Customers and Internal Suppliers. For every Company that needs to transact with another EE-linked Company:

- **One Internal Customer per destination Company.** Named `INTL-CUST-for-{destination}`. Marked `is_internal_customer=1`, `represents_company=<destination>`. The `companies` ("Allowed To Transact With") child table enumerates every source Company permitted to sell to this destination.
- **One Internal Supplier per source Company.** Named `INTL-SUPP-from-{source}`. Marked `is_internal_supplier=1`, `represents_company=<source>`. Symmetric — `companies` child lists every target.

**Cardinality is N+N for N EE-linked Companies, NOT N×(N−1).** ERPNext enforces at-most-one Internal Customer per `represents_company` — multiple per pair is structurally refused. The N+N model with allowance-via-child-table is the ERPNext-idiomatic answer.

**Auto-created at go-live** via `ensure_internal_party_pairs_for_account(account_name)`. Idempotent: re-runs add missing pairs and reconcile the `companies` child additively (won't strip rows an FDE adds manually). The Internal Customer is also auto-pushed to EE via §8e's machinery so `ee_customer_id` is captured — Stage 2's STN push needs that customerId.

**Runtime lookup pattern** (used by the integration internally — FDEs should know it for debugging):
- For a transfer from Company A to Company B: find Customer where `is_internal_customer=1 AND represents_company=B AND A in companies[*].company`.
- Symmetric for Internal Supplier on inbound.

**Why the FDE cares:** if a transfer ends up in `Drift` status with `flag_reason = "Internal Customer pair missing"`, the FDE invokes `ensure_internal_party_pairs_for_account` to fix the fabric, then retries the transfer.

## Part C — Goods-in-Transit (GIT) is always used

Every §10 transfer parks stock in the destination Company's GIT warehouse (`Company.default_in_transit_warehouse`) at DN submit. Stock moves GIT → destination *only* when the IPR submits, *only* for the received qty. The balance (if any) stays in GIT and is the variance signal.

This is true for same-GSTIN transfers too — not just inter-Company. The reason: it gives the FDE a uniform variance-tracking story regardless of whether the transfer crosses a GSTIN boundary. The Transfer Map's `git_balance` field reports the current open GIT balance for that transfer at any moment.

Aged GIT (balance > 0 after `lost_in_transit_threshold_days` from `EasyEcom Account`, default 30) triggers an ERP user nudge — ToDo on the draft DN owner + Comment on the DN. The ERP user decides: submit the draft DN to accept loss, or investigate further (re-pull a missed GRN, etc.). The integration doesn't auto-resolve aged GIT — variance acceptance is a financial decision, not an automation decision.

## Part D — The doctype matrix (what documents get created when)

This is the operational table the FDE refers to most often. **Updated 2026-06-01 — 4-branch matrix** (the original 3-branch version missed the B2B case where source is EE-mapped but target is not):

### The first cut: which EE primitive fires (the 4-branch decision matrix)

| Source EE-mapped? | Target EE-mapped? | Branch | EE primitive |
| --- | --- | --- | --- |
| ❌ | ❌ | **Inert** | (no EE call) — Gate-0 silent skip; pure ERPNext-native |
| ✅ | ✅ | **STN** | `createOrder · orderType=stocktransferorder` |
| ❌ | ✅ | **PO** | `CreatePurchaseOrder` (reuses §9 wire) |
| ✅ | ❌ | **B2B** | `createOrder · orderType=businessorder` |

### The second cut: which ERPNext documents get created (per branch × GSTIN)

| Branch | GSTIN | DN | SI | IPR | IPI | Debit Note |
| --- | --- | --- | --- | --- | --- | --- |
| STN | Same | ERP user creates | — | Auto-submitted | — | — |
| STN | Different | ERP user creates | Auto-drafted, ERP user submits | Auto-submitted after SI submit | Auto-drafted | Auto-drafted on gap |
| PO | Same | ERP user creates | — | Auto-submitted | — | — |
| PO | Different | ERP user creates | Auto-drafted, ERP user submits | Auto-submitted after SI submit | Auto-drafted | Auto-drafted on gap |
| **B2B** | Same | ERP user creates | — | **Pure ERPNext-native receipt on destination** | — | — |
| **B2B** | Different | ERP user creates | Auto-drafted, ERP user submits | **Pure ERPNext-native receipt on destination** | — | — |
| Inert | — | ERP user creates | — | — | — | — |

**Important on the B2B branch:** the destination is OUTSIDE EE's universe. EE issues a B2B order representing the dispatch, but **no EE inbound primitive fires for the receipt** — stock has left EE's universe. The destination-side receipt happens via normal ERPNext UX (Purchase Receipt, Stock Entry, or Material Receipt as appropriate to the deployment). The §10 substrate does NOT create an IPR for B2B transfers. Do not expect auto-magic on the destination side.

The B2B branch also uses a different `customer[].customerId` than the STN branch: it's the Internal Customer's **wholesale c_id** (from `/Wholesale/CreateCustomer`), not the regular customerId. The integration handles this automatically; FDEs don't pick — but worth knowing for debugging.

**The pattern, in words:** ERP user creates the DN. The integration auto-creates Drafts of SI/IPI/DN where needed. The integration auto-submits IPR (when financial preconditions are met AND the branch produces an IPR — B2B doesn't). ERP user submits SI/IPI/DN.

**The auto-Debit-Note is the genuinely novel piece.** It has no §9 analogue. When received < dispatched on a different-GSTIN STN or PO flow, the integration auto-drafts a Purchase Invoice with `is_return=1`, gap-sized lines, `return_against=IPI`. This reverses ITC proportionally on the gap. The ERP user reviews and submits. Multi-GRN scenarios revise (or auto-cancel-with-Comment-on-Transfer-Map) the draft DN as more goods arrive. Note: the auto-DN does NOT fire on the B2B branch (no IPR/IPI to debit against).

## Part E — The IPR submit gate (the §10 invariant)

This is the most important invariant to understand for §10 operations. The integration's IPR auto-creation is unconditional (every routed GRN produces an IPR). But IPR **auto-submit** is conditional:

- **Same GSTIN** → IPR auto-submits. Stock moves GIT → destination.
- **Different GSTIN AND source-side SI is SUBMITTED** → IPR auto-submits.
- **Different GSTIN AND source-side SI is NOT submitted** (Draft or missing) → IPR stays in **Draft**. No stock movement. ERP user is nudged via ToDo + Comment on the IPR. **No Integration Discrepancy raised** — this is ERP-user pending state, not a FDE config issue.
- **Submitted Debit Note exists on the Transfer Map** AND new GRN arrives → IPR stays in Draft regardless of SI state, **Discrepancy raised** (this IS a FDE concern — see Part G).

**The GSTR-2B coherence argument:** for different-GSTIN transfers, the SI must legally crystallise the source-side supply before the destination Company can receipt stock. Auto-submitting the IPR with an unsubmitted SI would create destination-side stock without source-side legal output — a tax-reporting incoherence.

**Auto-retry on SI submit:** when the ERP user eventually submits the previously-drafted SI, a `Sales Invoice.on_submit` doc_event hook auto-retries the drafted IPRs on the linked Transfer Map. If the gate now clears, the IPRs auto-submit and the chain continues (IPI auto-draft, DN auto-draft if gap). Hands-off cascade once the ERP user closes the SI gate.

**Historical note (2026-06-01):** the auto-retry cascade depends on the SI's `ecs_section10_transfer_map` back-link being populated at SI-draft time. Between 2026-05-29 and 2026-06-01, this back-link was inadvertently left NULL on every §10-drafted SI — the `_draft_internal_sales_invoice` substrate had a stale `# back-fill below` comment that was never executed. The cascade was silently neutralised for that window. Fixed in commit `cd27d0f` (`transfer_push.py:221-229`); the back-link is now written immediately after Transfer Map creation. If you're investigating older transfers from that window, the back-link may be NULL and the cascade may not have fired — a one-time manual `db.set_value` retro-patch is the cleanup.

**Status transition note (also 2026-06-01):** Transfer Map.status advances out of `SI-Pending` independently of IPR state. Before the fix, status was stuck at `SI-Pending` indefinitely until a GRN arrived, even after EE push succeeded. Fixed in the same corrective commit (`transfer_inbound.py:1340-1355`).

## Part F — Multi-GRN against the same DN (cumulative arithmetic)

Real deployments have partial receipts. EE sends GRN1 (some lines received), GRN2 (more received later), etc. The integration handles each as a separate event:

- Each GRN creates a new IPR row on the Transfer Map's `internal_purchase_receipts` child table.
- Cumulative received per Item = Σ across all submitted IPRs.
- Gap per Item = dispatched_qty − cumulative_received.
- **Draft DN auto-revises** as cumulative received grows: line qtys shrink to match the new gap. Audit Comment on the Transfer Map records the revision.
- **Draft DN auto-cancels** if cumulative received closes the gap (received == dispatched). The Comment writes to the **Transfer Map** (not the about-to-be-deleted DN), so the audit trail survives the deletion. Look for these Comments on the Transfer Map document when investigating "did this transfer ever have a gap?"

The IPI does NOT revise on multi-GRN — it stays sized to the SI's dispatched qty for full ITC claim. Only the DN reflects the gap.

**Status transitions:**
- After first partial IPR submit: Transfer Map → `Partial-Received`.
- After cumulative receipt closes (cumulative == dispatched, no draft DN remaining): → `Fully-Received`.
- After ERP user submits the draft DN (acknowledging loss): → `DN-Submitted-Locked`. Subsequent GRNs trigger Part G's late-GRN block.

## Part G — Submitted-DN-Late-GRN block (the second §10 invariant)

What happens if a GRN arrives *after* the ERP user has already submitted the draft Debit Note acknowledging the loss?

- New IPR is built but does NOT auto-submit (stays Draft regardless of SI state).
- Integration Discrepancy raised, kind=`"Late GRN after submitted DN"`. **This IS an FDE concern** — surfaces on the §17 worklist.
- Comment on the IPR + ToDo on the ERP user.
- The ERP user must reconcile: they previously took the ITC reversal; now goods have actually arrived; they need to reverse the DN (via a fresh PI or Journal Entry) and then manually submit the IPR.
- Integration does NOT auto-resolve. Hands off — this is a financial reconciliation, not an automation.

The "refuse to auto-submit when financial precondition is broken" invariant manifests twice in §10: (a) SI-not-submitted on different-GSTIN, (b) DN-already-submitted on late GRN. Both follow the same principle.

## Part H — EE-originated standalone IPR (the §9 self-GRN entry point)

If a GRN's `vendor_c_id == inwarded_warehouse_c_id`, the GRN is EE-internal (batch loads, opening stock, EE-side transfers we didn't originate from an ERPNext DN). §9's pull-handler routes these to §10 via `transfer_inbound.handle_ee_originated_grn`.

§10's response: **no auto-IPR.** Frappe refuses to save a PR without a Supplier, and §10 has no Transfer Map to look up an Internal Supplier from. The integration instead surfaces an **Integration Discrepancy** with kind=`"EE-originated transfer (self-GRN)"`. The FDE resolves it via the same `Create-PR-from-GRN` action §9 introduced for unknown-PO drift — pick an Internal Supplier manually, create the PR.

This is the second time §10 leans on §9's drift-resolution path. Pattern: where §10 can't auto-decide (because the EE event has no ERPNext-origin), it falls back to FDE-driven resolution rather than guessing.

**Carry-forward:** the routing check (`vendor_c_id == warehouse company_id`) is code-correct but **NOT live-verified on Harmony as of §10 closeout**. No real self-GRN sample has been triggered on the sandbox yet. The path is mock-tested. Live-verify by triggering a real self-GRN on Harmony and inspecting the payload's `vendor_c_id` field. If the assumption holds, this path is live-correct.

## Part I — Role separation (the operational discipline)

Three roles touch §10. Knowing which is responsible for what saves a lot of confusion:

- **FDE / EasyEcom System Manager** owns integration health:
  - Drift Transfer Maps (config gaps — Internal pair missing, Item unmapped, warehouse Address missing).
  - EE-originated standalone Discrepancies (pick supplier, create PR).
  - Late-GRN-after-submitted-DN Discrepancies (flagged for awareness; ERP user reconciles).
  - Aged GIT scan operational (cron health, not the individual ToDos).
- **ERP user** owns business flow:
  - Creating DNs (the source-side trigger).
  - Submitting drafted SIs (different-GSTIN gate).
  - Submitting drafted IPIs (after IPR submits).
  - Submitting drafted Debit Notes (accepting loss).
  - Aged GIT decisions (submit DN or investigate).
  - Late-GRN reconciliation (reverse DN + submit IPR).
- **EasyEcom Operator** is read-only on §10 surfaces. They see status but don't act.

The §17 FDE Worklist surfaces **integration-health items only**. ERP user concerns are surfaced via ToDos + Comments on the actual documents (DN, IPR, draft DN) — native ERPNext UX, no integration-specific dashboard. The Operations Dashboard for ERP users is explicitly deferred as future polish.

## Part J — Pause respects §10 writes (parity with §9)

The `pause_all_auto_push` kill-switch from round-2 controls applies to §10:
- DN submit during pause → Transfer Map created in pre-push state, SI auto-drafts if different-GSTIN (ERPNext-side, not an EE write), but **no EE push**. `ecs_pending_ee_push=1` on the Transfer Map.
- Aged GIT cron skips paused accounts (the ToDo creation is an integration-driven write).
- IPR creation from GRN pull continues during pause (read-side), but if the pull itself is paused (account disabled), no IPR either.
- Un-pause via `go_live_enable_auto_push(pos=1)` runs `fire_pending_transfer_pushes` automatically, sweeping the pending Transfer Maps and pushing them once.

Latest-state-wins for the binary pending flag — there's no multi-state queue. A pause-and-cancel scenario falls into the Stage 2 stub-blocker (cancel of EE-pushed transfer refused until cancel/amend endpoint is grounded).

## Part K — The cancel/amend stub-blocker (an honest deferred item)

DN cancel or DN amend on a Transfer Map in EE-pushed state is **refused at the ERPNext level** with a clear error:

> "§10 STN cancel/amend not yet implemented — EE cancelOrder endpoint payload ungrounded (§10.G). DN {name} has a Transfer Map row in status {status} with ee_order_id={id}. Cancelling would desync ERPNext from EE. Contact the integration team to schedule the cancel-payload grounding."

DNs in pre-push states (Mapped, Drift, SI-Pending without ee_order_id) cancel cleanly — nothing to desync.

**Why this exists:** EasyEcom's API doc page for createOrder doesn't include the cancel/amend payload. We deferred wiring rather than guess at the payload shape. The stub-blocker prevents silent desyncs.

**To lift:** ground the cancel/amend endpoint payload from EE (doc page or working postman). Then the stub becomes a real wire call. Tracked in §10 BUILD_TRACKER carry-forwards.

## Part L — What goes in the §17 FDE Worklist

The Stock Transfer row on the worklist has number-cards for **integration-health items only**:
- §10 Transfer Maps in Drift (FDE config-resolution work).
- §10 EE-originated Discrepancies (FDE picks Internal Supplier, creates PR via §9 action).
- §10 Submitted-DN-Late-GRN Discrepancies (FDE awareness; ERP user does the reconciliation).

Each card filters the relevant list view. Counts are Company-scoped (multi-Company isolation).

**NOT on the worklist:**
- Drafted SI/IPI/DN pending ERP user submission (operational pending — ERP user surface).
- IPRs in Draft because SI is not submitted (already routed via ToDo).
- Aged GIT cases (own ToDo channel).

The discipline: if a card would put non-integration-health work on the FDE's screen, it doesn't go there. Either it's surfaced to the ERP user via ToDo + native ERPNext UX, or it's surfaced as a Discrepancy where the FDE awareness is genuine.

---

## Part M — UX surfaces (warehouse EE-mapping visibility + branch prediction)

Added 2026-06-01 from a live integration smoke discovery: FDEs creating §10 DNs had no visual cue indicating which §10 branch a chosen (source, target) warehouse pair would route to. The fix is a small UX layer that makes routing decisions visible *before* the DN is submitted.

**Warehouse label (`Warehouse.ecs_ee_location_label`):** every Warehouse linked to a Live + enabled EasyEcom Location now displays a read-only label `"EE: <location_name> (#<location_key>)"`. Non-mapped or non-Live warehouses have empty labels. The label appears in list views, on the Warehouse form, and as the description column in autocomplete dropdowns wherever Warehouse is a Link field (PO, Stock Entry, Material Request, SI — automatic, comes for free with the custom field).

**Bidirectional sync:** the label is kept fresh by hooks on EasyEcom Location:
- `after_save` → recomputes the label on the current mapped warehouse AND any prior mapped warehouse if it was re-pointed (catches re-points without leaving a stale label behind).
- `on_trash` → recomputes on the orphaned warehouse (catches deletion cleanup).
- The gate matches `transfer_push._is_ee_mapped_warehouse` exactly, so the label cannot drift from the routing decision.

**DN-form branch prediction (DN-only by design):**
- The 5 header warehouse fields (`set_warehouse`, `set_target_warehouse`, `ecs_section10_target_warehouse`, `ecs_section10_transfer_from_warehouse`, `ecs_section10_transfer_to_warehouse`) use a custom autocomplete that sorts EE-mapped warehouses first.
- Once both §10 fields are filled, a branch-prediction chip appears on the DN dashboard via `frm.dashboard.add_indicator`: e.g. `§10 branch: B2B · src ✓ EE · tgt — non-EE` in blue.
- An explanation block appears under `is_internal_customer` explaining what branch will fire and what documents will be created.

**Why DN-only:** the chip predicts §10 branches. PO, Stock Entry, Material Request, and SI aren't §10 triggers — putting a chip there would be confusing. If §11 or §12 introduce branch decisions on other doctypes, build the chip there with appropriate scope-guard logic.

**Backfill:** a one-shot patch populates labels on all existing warehouses at deployment time. New warehouses get their label on the next EasyEcom Location save touching them.

---

## Carry-forwards and watch-items

- **STN self-GRN routing live-verification (Part H)** — pattern is code-correct, mock-tested, NOT live-verified. Trigger a real self-GRN on Harmony to confirm `vendor_c_id == warehouse company_id`. First operational deployment hits this.
- **STN cancel/amend endpoint grounding (Part K)** — required before lifting the stub-blockers. Operational once ERP users start cancelling.
- **PO-branch wire dispatch live-smoke** — Stage 4 wired it against mocks. A real non-EE-source-with-EE-target deployment is the first real exercise. Most deployments have both source and target EE-mapped (STN branch); PO branch is the less-common path.
- **B2B-branch destination GRN flow** — purely ERPNext-native by design (stock has left EE's universe). No EE inbound primitive exists; no §10 inbound hook fires for B2B Transfer Maps. ERP user creates a regular Purchase Receipt or Stock Entry on the destination side via standard ERPNext UX. Documented so FDEs don't expect auto-magic.
- **Multi-GRN partial cumulative — live-smoke for §10** — unit-verified across all branches, NOT live-smoked on Harmony with a real partial-then-completion sequence. First real client with a multi-receipt scenario is the live exercise.
- **§9 `_resolve_for_receipt` vs §10 inline Item resolution divergence** — Stage 3 couldn't reuse §9's resolver (short-circuits on supplier_missing; §10 has no Supplier Map by design). Item resolution forked. Watch for drift on future fixes.
- **Operations Dashboard** — deferred as future polish. ERP user surfaces are native ERPNext for now.
- **Test discipline lesson (from 2026-06-01 corrective)** — the two latent bugs (SI back-link not written, TM status stuck at SI-Pending) were unit-test-invisible because Stage 3 tests asserted the cascade behaviour *conditional on* the back-link being set, not that the back-link itself got written. Future test scripts include explicit end-to-end checks of state propagation between document submissions, not only per-document checks. The lesson generalises: when tests mock the inputs to the system under test, they can't catch real callers' bugs that fail to provide those inputs.

**Tested by:** `../test_scripts/section_10_stock_transfer.md`.

---

*§11 (next operational flow — TBD in numbering scheme; likely sales-side) will build on §10's Internal Customer machinery (for B2B sales between Companies) and on §9's GRN-pull infrastructure (if returns route through GRN-like inwards). When §11 ships, its primer joins this one as `FDE_PRIMER_section_11_*.md`.*
