# Section 9 — Buying / GRN — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers §9's full buying loop: PO push (content + status channels), GRN pull → Purchase Receipt with qc_fail split, completion echo, drift handling for unknown-PO GRNs, and the pause kill-switch across all three po_status pushes. Derived from `docs/SPEC.md` §9 (post §9 closeout) and `spec_sections/section_9_buying_packet.md`.

> **First time?** Read `HOW_TO_RUN_FDE_TESTS.md`, then the §9 primer (`../primers/FDE_PRIMER_section_9_buying.md`).

> **The model in one line.** ERPNext PO → CreatePurchaseOrder (content, keyed `referenceCode`=PO name) + updatePoStatus (status, keyed `po_id`); EE-side GRN → ERPNext PR with `received_qty=received_quantity`, `rejected_qty=qc_fail`, accepted derived; completion echoes back po_status=5.

> **Status:** live-verified across 5 rounds against the Harmony sandbox (disposable) plus two corrective-commit re-smokes. Pre-existing §8 test failures (24) are unrelated to §9 and tracked separately.

> **Scope:** §9 covers the buying flow only. §10 (Stock Transfer) reuses the GRN pull machinery but is its own flow with its own test script.

**Build under test:** commit / branch ____________ · **Deployed to:** ____________ · **Tester:** ____________ · **Date:** ____________

---

## Section 1 — Preconditions and pre-go-live precheck

### 1.1 Run the buying precheck
**Do:** EasyEcom Account → run `precheck_buying_go_live`. Inspect `{ok, blockers, warnings, checked}`.
**Confirm:** Output covers Stock Settings (enable_serial_and_batch_no_for_item=1, use_serial_batch_fields=1), default_rejected_warehouse per Company, EE-mapped warehouses with resolvable addresses, grn_receipt_trigger_status set, grn_pull_high_watermark set.
**Good:** `ok: true` with empty blockers. Warnings (if any) are acceptable to ship.
**Failure looks like:** Blockers present → fix and re-run before continuing.

### 1.2 Confirm cron stays unwired until go-live
**Do:** Inspect the scheduler config and run `pytest -k TestGRNPullSchedulerIntentionallyUnwired`.
**Confirm:** The GRN-pull cron is not scheduled to fire automatically; the guard test is green.
**Good:** No auto-fire; manual `bench execute` invocation works.
**Failure looks like:** Cron fires automatically with NULL `grn_pull_high_watermark` (cold-start hazard — STOP and report).

---

## Section 2 — PO push (content channel)

### 2.1 Create + push a clean PO
**Do:** Create a PO in ERPNext: EE-mapped warehouse, mapped Supplier (Supplier Map exists with `ee_vendor_id`), all items have Item Map rows, HSN present on items. Submit.
**Confirm:** Within seconds, `EasyEcom PO Map` row created (`ECS-PO-{po_name}`); `reference_code` = PO name; `ee_po_id` populated from EE's `data.poId`; status = Mapped. The CreatePurchaseOrder Sync Record is Success.
**Good:** PO Map row complete, both keys captured. EE-side PO visible in Harmony.
**Failure looks like:** PO Map missing → Gate 0 didn't fire (warehouse not EE-mapped — verify §8a); PO Map present but status = Flagged-Not-Created → check `flag_reason` (Supplier Map miss, Item Map miss, missing HSN).

### 2.2 Amend a PO (tax change → updateTaxRate=1)
**Do:** Take an EE-pushed PO, amend it (e.g. change a line's Item Tax Template). Submit the amendment.
**Confirm:** A second CreatePurchaseOrder push fires with `createOrUpdate: "U"` and `updateTaxRate: 1`. PO Map's `ee_po_id` unchanged.
**Good:** Update push succeeds, EE-side PO reflects the new tax.
**Failure looks like:** No update push (auto-push toggle OFF — check `auto_push_pos_on_save`); `updateTaxRate: 0` despite tax change (signature comparison bug — report).

### 2.3 Non-EE warehouse PO is silently inert
**Do:** Create a PO with target_warehouse pointing at a warehouse with no §8a Location mapping. Submit.
**Confirm:** No PO Map row created. No Sync Record. No EE call. The PO lifecycle in ERPNext is exactly as it would be without the integration installed.
**Good:** Integration is silent (the Gate-0 invariant).
**Failure looks like:** Map row created, or any EE call fired — report immediately (Gate-0 leak is the most important invariant).

---

## Section 3 — PO push (status channel)

### 3.1 on_submit → po_status=3 (Approved)
**Do:** Submit a PO (covered in 2.1). Inspect the PO Map's `last_pushed_po_status` after the push lands.
**Confirm:** `last_pushed_po_status = 3`. The updatePoStatus push body was `{po_id: <ee_po_id>, po_status: 3, markPoComplete: 0}`. EE-side PO state advances from 2 (Waiting for Approval) to 3 (Approved).
**Good:** Status push fired once on submit.
**Failure looks like:** No status push (queue/worker issue — check `frappe.utils.background_jobs`); `last_pushed_po_status` stuck at NULL.

### 3.2 Idempotent re-submit
**Do:** Save the submitted PO again (any way that doesn't change status). Or re-run the push manually.
**Confirm:** No second updatePoStatus push fires. `last_pushed_po_status` stays at 3.
**Good:** Idempotency guard works (same-status no-op).
**Failure looks like:** Second push fires, EE sees a duplicate status=3 call — report (idempotency broken).

### 3.3 on_cancel → po_status=7 (Cancelled)
**Do:** Cancel an EE-pushed PO in ERPNext.
**Confirm:** updatePoStatus push fires with `po_status: 7`. `last_pushed_po_status` advances to 7. PO Map status reflects cancellation. **The `isCancel` flag on CreatePurchaseOrder is NOT used** — cancel goes via status channel only.
**Good:** Cancel propagates to EE as status=7.
**Failure looks like:** Any CreatePurchaseOrder call with `isCancel: 1` (channel separation broken — report).

---

## Section 4 — GRN pull → Purchase Receipt

### 4.1 GRN at QC Complete → PR auto-created
**Do:** On the EE side (Harmony), complete a GRN against a pushed PO: GRN created → QC Pending → QC Complete (Mark GRN Complete). Trigger the manual pull via `bench execute easyecom.flows.grn_pull.pull_grns_for_account`.
**Confirm:** Within the pull, a Purchase Receipt is created and submitted. `EasyEcom GRN Map` row created (`ECS-GRN-{ee_grn_id}`); status = Receipted; `purchase_receipt` linked to the PR. The PR carries back-refs `ecs_easyecom_grn_id`, per-line `ecs_easyecom_grn_detail_id` and `ecs_easyecom_po_detail_id`. The native `purchase_order_item` link is set on each PR Item (so the PO's `per_received` updates live).
**Good:** PR submitted; stock landed; back-refs complete.
**Failure looks like:** PR not created (check Sync Record for kind=Failed); GRN Map status = Held-Pre-QC (trigger setting too high — check `grn_receipt_trigger_status`); PR created but `purchase_order_item` not linked (PO.per_received won't update — report).

### 4.2 Qty model: received_quantity vs the post-receipt buckets
**Do:** Find a real GRN in Harmony where `received_quantity` is positive and at least one of the post-receipt buckets (e.g. `available`, `sold`) is *different* from received_quantity. Pull it.
**Confirm:** PR line `received_qty = received_quantity` (the actual receipt event). None of the post-receipt buckets (`available, reserved, sold, damaged, qc_pass, lost, transfer, return_*, near_expiry, expiry`) leak onto the PR or its line. Inspect the PR Item DocType: no field matches an EE bucket name.
**Good:** Receipt qty is the receipt event; buckets are read-and-discarded.
**Failure looks like:** PR received_qty equals `available` instead of `received_quantity` (cardinal error — silently loses stock-in — STOP and report).

### 4.3 qc_fail split → rejected_warehouse
**Do:** On Harmony, complete a GRN where the line carries qc_fail > 0 (e.g. received 10, qc_fail 2). Pull it.
**Confirm:** PR line `received_qty=10, rejected_qty=2, accepted_qty=8`. Accepted (8) goes to the mapped warehouse; rejected (2) goes to `default_rejected_warehouse` on Company Settings.
**Good:** Standard ERPNext two-warehouse split applied; settings honoured.
**Failure looks like:** `rejected_qty` not populated (qc_fail mapping broken); accepted goes to wrong warehouse (location_key resolution wrong); PR submit fails on missing rejected_warehouse (settings not configured — fix Company Settings, retry).

### 4.4 `grn_detail_price` is line total → rate derivation
**Do:** Pull a GRN where you know the unit price independently (e.g. you set it on the PO). Inspect the PR line.
**Confirm:** PR line `rate = grn_detail_price / received_quantity` (not `grn_detail_price` directly). Total line value matches the GRN line total.
**Good:** Rate derived correctly from line-total payload.
**Failure looks like:** PR rate inflated by qty factor (the unit-price-vs-line-total bug — STOP and report; this is the §9 corrective commit's most-important fix).

---

## Section 5 — Completion echo (status=5)

### 5.1 Full receipt → po_status=5 fires
**Do:** Receipt a PO fully via GRN(s) such that cumulative `received_quantity` ≥ `original_quantity` across all PO lines.
**Confirm:** After the final IPR submits, an updatePoStatus push fires with `po_status: 5, markPoComplete: 0`. PO Map's `last_pushed_po_status` advances to 5. EE-side PO state shows Completed.
**Good:** Completion echoes back to EE once.
**Failure looks like:** No completion push (cumulative arithmetic bug); push fires repeatedly on subsequent GRN pulls (idempotency broken — `last_pushed_po_status` should prevent re-fire).

### 5.2 Force-close (under-receipt → markPoComplete=1)
**Do:** Take a partially-received PO. Click Close in ERPNext on the PO doc.
**Confirm:** updatePoStatus push fires with `po_status: 5, markPoComplete: 1`. EE closes the partial PO on its side.
**Good:** Force-close propagates correctly with the explicit flag.

---

## Section 6 — Unknown-PO GRN drift (the corrective commit)

### 6.1 GRN for unknown PO → no auto-PR
**Do:** On Harmony, create a GRN whose `po_ref_num` and `po_id` do NOT correspond to any ERPNext-created PO (e.g. an EE-origin PO created directly in Harmony, not via our push). Pull it.
**Confirm:** **No Purchase Receipt is created.** `EasyEcom GRN Map` row written with status = Discrepancy, `purchase_receipt` empty, `linked_po_map` empty. The full GRN payload is preserved on the Map row's `ecs_grn_payload_json` field. An Integration Discrepancy is raised with kind="GRN for unknown PO".
**Good:** Drift behaviour as specified — stock does NOT move silently for unknown-PO GRNs.
**Failure looks like:** A PR was auto-created — STOP and report immediately (this is the §9 corrective commit's most-important invariant).

### 6.2 FDE creates PR from drifted GRN (standalone)
**Do:** On the drifted GRN Map row, invoke `easyecom.api.grn_drift_resolution.create_pr_from_grn(grn_map_name, confirm=True)` (no purchase_order arg). FDE/System Manager role.
**Confirm:** A standalone Purchase Receipt is created and submitted. The PR has NO `purchase_order` link. Qty model is identical to the normal flow: received_qty, rejected_qty (qc_fail), accepted_qty derived, rejected → default_rejected_warehouse. GRN Map status → Receipted. Integration Discrepancy → Resolved (auto). Audit Comment on both the PR and the GRN Map.
**Good:** FDE-driven receipt path works; PR is standalone; audit trail present.
**Failure looks like:** Action refused without confirm (good); action available to Operator role (BAD — report the role-gate leak).

### 6.3 FDE creates PR from drifted GRN (with optional PO link)
**Do:** Drift a GRN (different ee_grn_id from 6.2). Invoke `create_pr_from_grn(grn_map_name, purchase_order="PO-XXXX", confirm=True)` providing an existing ERPNext PO that fits.
**Confirm:** PR created with `purchase_order = PO-XXXX`. Otherwise identical to 6.2.
**Good:** Optional PO link works when supplied.

### 6.4 FDE dismisses drifted GRN
**Do:** Drift a GRN (different ee_grn_id). Invoke `dismiss_grn_drift(grn_map_name, reason="Test dismissal", confirm=True)`.
**Confirm:** No PR. GRN Map status → Dismissed. Integration Discrepancy → Dismissed. Audit Comment with the reason.
**Good:** Dismiss path works.
**Failure looks like:** Dismiss refused without reason (good — it should be); dismiss without confirm succeeds (BAD).

### 6.5 Re-pull idempotency on drift
**Do:** After 6.1 (GRN drifted), trigger the pull again. Confirm via the same `ee_grn_id`.
**Confirm:** No duplicate Integration Discrepancy raised. No duplicate Sync Record. The existing drifted GRN Map row's `last_observed_at` is refreshed; status stays Discrepancy.
**Good:** Drift is idempotent under re-pull.

---

## Section 7 — Pause kill-switch across all three status pushes

### 7.1 Pause defers po_status=3 (submit)
**Do:** Invoke `pause_all_auto_push(reason="test", confirm=True)`. Confirm all four toggles (Items / Customers / Suppliers / POs) flip OFF. Submit a PO that would otherwise push.
**Confirm:** The PO submits in ERPNext but **no EE call fires**. PO Map row's `ecs_pending_po_status_push` field is set to 3.
**Good:** Pause defers submit.

### 7.2 Pause defers po_status=7 (cancel)
**Do:** While still paused from 7.1, cancel the submitted PO.
**Confirm:** No EE call. `ecs_pending_po_status_push` field on the PO Map updates to 7 (latest-state-wins overwrites the 3).
**Good:** Cancel during pause overwrites pending submit.
**Failure looks like:** Old behaviour — cancel always fires regardless of pause (report — this was the §9 corrective commit's other big fix).

### 7.3 Pause defers po_status=5 (completion)
**Do:** Set up: have a PO ready to be fully received. Pause. Pull a GRN that would complete the PO.
**Confirm:** IPR submits (read-side pull still runs during pause), but the completion push (po_status=5) is deferred — `ecs_pending_po_status_push` on the PO Map = 5.
**Good:** Pause defers completion echo.

### 7.4 Un-pause → pending fires once
**Do:** Invoke `go_live_enable_auto_push(pos=1, confirm=True)`. This re-enables the PO toggle and invokes `fire_pending_po_status_pushes`.
**Confirm:** The pending status (whatever was last recorded — 7 from 7.2, or 5 from 7.3) fires exactly once to EE. The `ecs_pending_po_status_push` field clears. The idempotency guard (`last_pushed_po_status`) prevents any subsequent sweep from re-firing the same status.
**Good:** Single fire on un-pause; cleared; no duplicates.
**Failure looks like:** Fires more than once (idempotency broken); doesn't fire at all (un-pause hook didn't invoke `fire_pending_po_status_pushes` — report).

---

## Section 8 — Edge cases and the §9↔§10 boundary

### 8.1 Self-GRN routes to STN (vendor_c_id == inwarded_warehouse_c_id)
**Do:** On Harmony, create an EE-internal inward GRN (batch load / opening stock entry on a mapped warehouse) where `vendor_c_id` will equal the warehouse's `company_id`. Pull it.
**Confirm:** GRN Map row created with `routed_to_stn=1`, status = STN-Routed. No PR. No Sync Record failure. Integration Discrepancy NOT raised (this is correct routing, not an error).
**Good:** Self-GRNs are recognised and routed to §10.
**Failure looks like:** PR auto-created (the routing didn't fire — check `vendor_c_id == inwarded_warehouse_c_id` semantic).

> **Watch-item:** if your tenant has no self-GRNs in real workflows, this test path can't run. The packet's STN routing verification is a §10 prerequisite. Until you have a real self-GRN to inspect, treat this routing as code-correct-but-untested-on-real-data.

### 8.2 Deleted-Post-Receipt
**Do:** On Harmony, complete and pull a GRN (PR created). Then on the EE side, flip the GRN's status to 4 (Deleted). Pull again.
**Confirm:** GRN Map status flips to Deleted-Post-Receipt. Integration Discrepancy raised. **The submitted PR is NOT cancelled by the integration.** ERP user / FDE investigates manually.
**Good:** Integration flags but doesn't silently reverse stock movement.
**Failure looks like:** PR auto-cancelled by the integration (hard-rule violation — STOP and report).

---

## What passing means

The script passes when: the buying loop works end-to-end (PO push → EE GRN → PR with correct qty model → completion echo); the unknown-PO drift path correctly *prevents* auto-receipt and provides FDE-driven recovery; the pause kill-switch defers all three status pushes uniformly; and the Gate-0 silent-inert invariant holds for non-EE warehouses.

The three load-bearing checks: **4.2 (no bucket leak onto PR), 4.4 (rate from line total not unit price), and 6.1 (no auto-PR for unknown PO)**. If any of these regress, §9 has lost its corrective-commit invariants and the build must STOP before any further changes.
