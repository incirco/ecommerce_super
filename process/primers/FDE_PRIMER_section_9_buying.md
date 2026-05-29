# FDE Primer — Section 9: Buying / GRN (operational flow #1)

**Who this is for:** an FDE who has read the foundation primer (`FDE_PRIMER_sections_1_to_7.md`) and the masters primer (`FDE_PRIMER_section_8_masters.md`). This is the first **operational flow** primer — §9 builds on the masters, doesn't recap them.

**Read the masters primer first.** Especially Part K (Supplier) for the two-key model (`ee_vendor_id` write key vs `ee_vendor_c_id` read key), and Part L (round-2 hardening) for the Go Live ceremony, pause kill-switch, and discover-async semantics. §9 reuses all of that.

**The model in one line.** ERPNext PO → EE PO (content + status channels, two keys) → EE-side GRN happens → ERPNext Purchase Receipt (with qc_fail-split into accepted/rejected) → completion-push echoes back.

---

## Part A — Where §9 sits in the integration

§9 is the first business flow. Everything before it (§3–§8) was substrate: connection, mapping engine, idempotency, contract, the six masters. §9 is where the integration starts doing *work for the business* — pushing real Purchase Orders to EE, pulling real Goods Receipt Notes, creating real Purchase Receipts.

The masters were *what to know about*. §9 is *what happens when goods are bought*. If a master is missing (no Supplier Map row for a vendor, no Item Map row for a SKU), §9's preconditions catch it cleanly. So the master investment pays off here.

§9 is **opt-in by warehouse**. Every PO and every GRN is checked at Gate 0: is its target warehouse EE-mapped? If not, the integration is silently inert — not failed, not flagged, not logged: simply absent, as if not installed. A non-EE warehouse PO behaves in ERPNext exactly as it would without the integration. This is the most important invariant for an FDE to understand: §9 doesn't touch what isn't its business.

## Part B — The two PO push channels

EE has two separate endpoints for POs, and they take different keys. The integration uses both, by design.

**Content channel: `CreatePurchaseOrder`** — creates and updates POs. Keyed on `referenceCode`, which equals the ERPNext PO name (stable, ERPNext-born, never changes). Returns `data.poId` (an EE-side int) which gets captured to the PO Map row.

**Status channel: `updatePoStatus`** — handles every state transition (submit, completion, cancel). Keyed on `po_id`, which is the EE-returned int from the content channel. So the status channel can only fire *after* the content channel has succeeded at least once (otherwise we have no `po_id` to pass).

The PO Map carries **both** keys — `reference_code` (set on first push) and `ee_po_id` (set when EE returns it). Same two-key bridge pattern as Supplier (§8f).

**Cancel goes via `updatePoStatus` with po_status=7**, not via the content channel's `isCancel` flag. Channel separation is deliberate: content for create/update, status for everything else. The `isCancel` flag exists on the content endpoint but the integration deliberately doesn't use it. Don't try to wire it.

### What gets pushed when

- **ERPNext PO on_submit** (post-approval) → push po_status=3 (Approved). EE PO state flips from default 2 (Waiting for Approval) to 3.
- **ERPNext PO fully receipted** (cumulative received ≥ ordered, modulo under-receipt tolerance) → push po_status=5 (Completed). Fires from the GRN pull handler, not from the PO itself — the PO doesn't know it's been fully received; the GRN tells us.
- **ERPNext PO force-closed** via the Close button with under-receipt → push po_status=5 with `markPoComplete=1`. EE then closes the under-received PO on its side too.
- **ERPNext PO on_cancel** → push po_status=7 (Cancelled).

That's it. EE-internal statuses 1 (Open), 4 (Rejected), 6 (Pending on Supplier), 8 (Payment Pending), 9 (Payment Done), 11–16 (fulfillment lifecycle) are EE's business — the integration neither pushes them nor reacts to them as drift. It observes them and records them on the PO Map for FDE visibility, but never overwrites ERPNext PO state from them.

### Pause respects all three pushes

The round-2 `pause_all_auto_push` kill-switch defers **every** EE write, including all three status pushes. When paused:
- Submit (3), completion (5), cancel (7) → recorded as `ecs_pending_po_status_push` on the PO Map; nothing sent to EE.
- Re-enable via `go_live_enable_auto_push(pos=1)` → pending statuses fire once and clear (idempotency guard ensures no double-fire).
- Latest-state-wins: submit then cancel during the same pause window leaves pending=7 (the more recent state), no stale 3 sent on un-pause.

So a pause is a *real* pause. Nothing sneaks through. This was a hole closed during §9's corrective commit.

## Part C — The GRN pull and the corrected qty model

This is where the most important load-bearing payload-grounded correction lives.

EE's GRN payload reports per-line:
- `received_quantity` — what physically arrived (the receipt event).
- `qc_fail` — what QC rejected (the reject event, settled only at status 3).
- Plus a long list of *post-receipt inventory buckets*: `available, reserved, sold, damaged, qc_pass, lost, transfer, …`.

**The buckets are NOT receipt facts.** They are EE's live warehouse state at the moment of the pull, and they drift continuously after inward. A line that received 100, then 60 sold off via ERPNext-side sales-channels-mirrored-by-EE, shows `received_quantity: 100, available: 40, sold: 60` on a later pull. If we mirrored `available` into ERPNext's accepted qty, we'd be losing 60 units of stock-in that genuinely arrived.

So the integration takes only what's payload-immutable:
- PR line **received_qty** = `received_quantity`
- PR line **rejected_qty** = `qc_fail` (QC-conditional — see Part D)
- PR line **accepted_qty** = `received_quantity − qc_fail` (derived; not lifted from `qc_pass`, which is also a drifting bucket)
- Accepted → mapped warehouse via §8a `location_key`
- Rejected → `default_rejected_warehouse` from settings (load-bearing — must be configured if any GRN can have qc_fail > 0)

All other buckets — read but **never posted**. ERPNext owns stock movement after receipt; mirroring EE's `sold` would double-count against ERPNext's own Sales Invoices.

### `grn_detail_price` is a line total, not a unit price

Stage 3 live discovery: EE returns `grn_detail_price` as the **whole line value**, not the per-unit rate. So `PR line rate = grn_detail_price / received_quantity`. Confirmed across 10 real Harmony GRNs spanning 90 days; the doc samples that looked unit-priced were coincidental (where qty=1 made the two readings identical).

## Part D — When the receipt actually fires (the QC trigger)

A GRN moves through EE's lifecycle: 1 CREATED → 2 QC Pending → 3 QC Complete. (4 Deleted is a terminal off-ramp.) The integration's `grn_receipt_trigger_status` setting (account-wide, default **3 QC Complete**) controls when a GRN actually becomes a Purchase Receipt.

**Why default 3:** the `qc_fail` reject quantity is only meaningful after QC has run. At status 1, `qc_fail` is structurally 0 (QC hasn't happened yet). Receipting at status 1 means the entire received qty becomes accepted, and any post-receipt QC failures never reach ERPNext.

**Lowering to 1 is acceptable** for deployments that don't run QC in EE — but then `rejected_qty` is always 0, and the rejected-warehouse split becomes a no-op. The setting's description warns the FDE about this.

Until the trigger threshold is met, observed GRNs sit in the GRN Map with status `Held-Pre-QC` (visible but not actioned). Subsequent polls re-evaluate.

## Part E — The Deleted (status=4) edge case

A GRN we already receipted can be flipped to status=4 (Deleted) on the EE side. The integration:
- If we already receipted it → GRN Map flips to **Deleted-Post-Receipt**; Integration Discrepancy raised. **The submitted PR is NOT auto-cancelled.** A submitted PR being silently cancelled by the integration is a hard-rule violation: it would reverse stock movement and reconciliation without an FDE seeing what happened. FDE investigates manually.
- If we never receipted it → quiet skip. No drama.

## Part F — Unknown-PO GRN: drift, not auto-receipt

This is the §9 corrective commit's big decision. **A GRN whose PO did not originate in ERPNext NEVER auto-receipts.**

The earlier behaviour (Stage 3 as initially built) was: if a GRN arrives for a PO that ERPNext doesn't know about, create a standalone PR anyway and raise a Discrepancy. That meant stock could *silently arrive* in ERPNext for goods nobody in ERPNext authorised the purchase of.

The corrected behaviour: GRN drift.
- No auto-PR.
- GRN Map row: status=Discrepancy, `purchase_receipt` empty, `linked_po_map` empty.
- Full GRN payload preserved on the Map row (the new `ecs_grn_payload_json` field).
- Integration Discrepancy raised, kind="GRN for unknown PO".

**Resolution is FDE-driven and ERPNext-side only.** On the drifted GRN Map row, the FDE has two whitelisted actions:

- **Create PR from this GRN** — builds a Purchase Receipt from the preserved payload, using the same qty model as the normal flow (received / qc_fail split / batch). By default the PR is **standalone** (no `purchase_order` link). The FDE may optionally supply an existing ERPNext PO to link if one fits. On success: GRN Map → Receipted, Discrepancy auto-resolved.
- **Dismiss the drift** — for GRNs that shouldn't be received at all (noise, duplicates, EE-side mistakes). Reason required, audit-logged. GRN Map → Dismissed, Discrepancy → Dismissed.

Both actions are role-gated (FDE / System Manager / EasyEcom System Manager — Operator can't), confirm-required, and audit-commented.

Re-pull of an already-drifting GRN is idempotent: no duplicate Discrepancy, just refresh `last_observed_at`.

**No PO is ever created or pushed to EE in the unknown-PO path.** ERPNext is the sole PO origin. The integration only pulls in the receipt direction; you cannot retroactively create-and-push a PO after goods have arrived — that's backwards. If a real EE-origin PO needs an ERPNext counterpart, that's a manual ERPNext step the FDE handles outside the integration.

## Part G — Self-GRNs route to §10 STN (the §9↔§10 boundary)

If a GRN's `vendor_c_id == inwarded_warehouse_c_id`, the GRN is an EE-internal inward — batch loads, opening-stock entries, internal transfers EE recorded as auto-GRNs. The vendor *is* the warehouse, not a real supplier.

§9 routes these to §10 (Stock Transfer Flows) rather than creating a Purchase Receipt. GRN Map row gets `routed_to_stn=1`, status=STN-Routed, no PR, no failure. §10's inbound machinery picks them up.

**One open verification before §10 ships:** this routing check is *code-correct* but never fired on real data — no self-GRN sample exists yet on Harmony. Pre-§10 work includes triggering a real self-GRN to confirm `vendor_c_id == company_id` holds in practice. Until then: the check sits ready, untested-on-real-data.

## Part H — Discrepancy vs Failed vs Held

Three different outcomes, three different meanings. Worth being precise:

- **Failed Sync Record:** the PR couldn't be created. Hard error — unmapped SKU, missing Supplier Map row, transport failure. FDE fixes the cause (creates the missing Map row, etc.) and retries.
- **Discrepancy:** the PR was created (or, for unknown-PO drift, deliberately wasn't), but something needs FDE attention. Examples: over-receipt beyond tolerance, HSN mismatch, tax variance > threshold, GRN for unknown PO, Deleted-Post-Receipt. The Sync Record's status is *Success*; the line-child rows that flagged the discrepancy carry a link to an Integration Discrepancy DocType (§23 stub).
- **Held-Pre-QC:** the GRN was observed but is below the receipt trigger status (typically status 1 or 2 awaiting QC). No PR yet. Next poll re-evaluates.

A Failed Sync Record means "fix this." A Discrepancy means "look at this." A Held means "wait."

## Part I — What goes in the §17 FDE Worklist

The Buying row on the worklist has number-cards for:
- POs Flagged-Not-Created (Supplier or Item Map missing).
- POs in Drift (rare — PO content is push-only and the only drift surface is po_status).
- GRNs Failed (couldn't auto-receipt).
- GRNs in Discrepancy (auto-receipted but flagged, OR drifted as unknown-PO).
- GRNs Held-Pre-QC (waiting for QC trigger).
- GRNs STN-Routed-pending (waiting for §10 to handle).

Each card filters the relevant list view. Counts are Company-scoped (multi-Company isolation).

The worklist is **for integration-health items only** — things that signal something the integration or its config needs the FDE to address. It is *not* a generic "stuff that's open in §9" list. Stuck-but-normal states (open POs, in-transit goods, normal partial receipts) are ERPNext's standard surfaces, not integration concerns.

## Part J — Pre-go-live precheck

Before turning on auto-push for §9 in a fresh deployment, the precheck (`precheck_buying_go_live`) verifies:
- Stock Settings: `enable_serial_and_batch_no_for_item=1`, `use_serial_batch_fields=1` (required for batch/serial-tracked items).
- `default_rejected_warehouse` configured on Company Settings if any GRN can carry qc_fail > 0.
- All EE-mapped Locations have a resolvable Address (warehouses without address fail the PO push).
- `grn_receipt_trigger_status` is set (default 3 is fine; FDE confirms intent).
- `grn_pull_high_watermark` is set — the cold-start cutoff that prevents the cron from pulling EE's 7-day backstop on first run.

Returns `{ok, blockers, warnings, checked}`. Blockers must be cleared; warnings can ship.

## Part K — The GRN pull cron and the go-live runbook step

The GRN-pull handler ships and is live-verified, but the **cron auto-fire is held until go-live** sets `grn_pull_high_watermark`. Why: a cron firing with a NULL watermark would invoke EE's 7-day default backstop and potentially mass-receipt a week of historical GRNs on first run — exactly the cold-start hazard you don't want.

So go-live for §9 is two steps:
1. Set `grn_pull_high_watermark` on the EasyEcom Account to a cutoff that excludes historical GRNs (typically: "now" at go-live moment).
2. Wire the cron (a small fixture flip or settings field — Stage 4 left this as the explicit go-live action).

A guard test (`TestGRNPullSchedulerIntentionallyUnwired`) ensures the cron stays unwired until this is done — if it accidentally gets wired in code, the test goes red. Until go-live, the pull is manual-invoke-only via `bench execute`.

## Part L — Carry-forwards and watch-items

A few things to know going forward:

- **Multi-GRN partial cumulative tolerance — unit-verified, NOT live-smoked.** Stage 3 only smoked full receipts. The path where one PO line gets received across multiple partial GRNs (GRN1: 6 of 10, GRN2: 4 of 10) is tested in the unit suite but unexercised against real EE. First time a real client has a partial receipt, watch the cumulative arithmetic.
- **STN routing live-verification is the §10 prerequisite** (Part G above). Trigger a real self-GRN on Harmony, inspect `vendor_c_id`, confirm equals warehouse company_id.
- **STN cancel/amend endpoint** is undocumented and will be a Stage 2 STOP-and-ask item when §10 starts.

**Tested by:** `../test_scripts/section_9_buying.md`.

---

*§10 (Stock Transfer Flows) is the next operational flow. It builds on §9's GRN pull machinery (the inbound IPR uses the same qty model and rejected-warehouse split), introduces the Internal Customer / Internal Supplier pattern for inter-GSTIN branch transfers, and adds the auto-Debit-Note mechanism for short receipts. When §10 ships, its primer joins this one as `FDE_PRIMER_section_10_stock_transfer.md`.*
