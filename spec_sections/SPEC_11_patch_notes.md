# §11 B2B Sales — SPEC Patch Notes

Inline corrections discovered during the §11 Phase 1 build (2026-05-30 …
2026-06-23) that the methodology team needs to fold back into `SPEC.md §11`.
Same shape as `SPEC_8d_patch_notes.md`, `SPEC_9_patch_notes.md`,
`SPEC_8e_patch_notes.md`, `SPEC_8f_patch_notes.md`.

Each item is sourced from a real live-smoke finding against the Harmony
sandbox or a documented build-time decision; the spec wording in
`SPEC.md §11` should be updated to match.

---

## 1. `/webhook/v2/createOrder` payload field rename: `suborders` → `order_items`

**Where in SPEC.md**: §11.3.1 (Async mode) / §11.3.2 (Sync mode) — the
implicit createOrder payload contract.

**The defect.** The Phase 1 build packet assumed EasyEcom's
`/orders/V2/getOrderDetails` response carried a per-line array named
`suborders` (mirroring the createOrder payload's body field). Paste 7
of the live-smoke sequence (2026-06-23 against Harmony, ref
`SAL-ORD-2026-00005`) showed the response actually returns the per-line
array under the key **`order_items`**, not `suborders`.

**The fix.** Renamed across `polling.py`, two test files, the §11 Stage 3
smoke precheck, and one fixture-based assertion. Commit `2d7d011`.

**SPEC change required.** Wherever the §11 packet refers to "suborders"
on the EE response, replace with "order_items". Internal shape (each
entry has `item_quantity`, `cancelled_quantity`, etc.) is unchanged.

---

## 2. State-change history is in-row `easyecom_order_history`, not multi-row response

**Where in SPEC.md**: §11.1 (the SO push lifecycle / status reconciliation
described in the flow diagram), and any place implying EE returns one
row per state transition.

**The defect.** The Phase 1 packet's polling derivation rule-table
docstring described "EE's multi-row response semantic (state-change
history + shipment splits)", implying each historical state would arrive
as a separate row in `response.data[]`.

**Live finding.** EasyEcom returns **one row per Order_id** (occasionally
multiple if the order is shipment-split — same `reference_code`, separate
`invoice_id`). The state-change history is embedded **inside each row**
as the field `easyecom_order_history`:

```json
"easyecom_order_history": [
  {"status": "Assigned",  "status_id": 2, "date_time": "..."},
  {"status": "Cancelled", "status_id": 9, "date_time": "..."}
]
```

`include_ee_history=1` query param controls whether the array is included
in the response.

**Phase 1 impact.** The Phase 1 polling derivation reads top-level
`order_status_id` and `invoice_number` per row, not the history array, so
the derivation logic still works correctly against the real shape — the
fixture-based test (`TestRealHarmonyResponseShape`) proves it.

**Phase 2 enhancement.** A history-aware derivation that walks
`easyecom_order_history` enables detection of intermediate transitions
(Shipped → Returned cycles, multi-step state progressions) which the
top-level-snapshot derivation misses.

**SPEC change required.** Replace "EE returns multiple rows for state
history" wording with "EE returns one row per Order_id; state-change
history is the in-row `easyecom_order_history` array (request with
`include_ee_history=1`)."

---

## 3. `getOrderDetails` requires `reference_code` OR `order_id` OR `invoice_id`

**Where in SPEC.md**: §11.1 / §11.5 — the polling tick / status query
contract.

**The defect.** Phase 1 polling assumed the EE-documented optional date
filters (`start_date`, `end_date`, `updated_after`, etc.) on
`/orders/V2/getOrderDetails` were sufficient on their own.

**Live finding.** Calling the endpoint with only date filters returns
HTTP 200 + `{"code": 400, "message": "Unable to find the
reference_code/order_id/invoice_id"}`. At least one of those three
identifiers is mandatory in practice.

**Phase 1 impact.** None — the per-Map polling tick already keys on
`reference_code = <SO.name>` per the §11 packet. The constraint just
means we cannot bulk-walk via dates alone.

**SPEC change required.** Add a note under §11.5 that
`getOrderDetails` is a single-order lookup endpoint (keyed by
reference_code / order_id / invoice_id), not a date-window walk.
For bulk discovery use `/orders/V2/getAllOrders` (covered by the
Stage 3 endpoint probe).

---

## 4. `getAllOrders` 7-day-cap finding (Stage 3 endpoint probe)

**Where in SPEC.md**: §11.1 / §11.3 — bulk discovery context.

**Finding.** `/orders/V2/getAllOrders` enforces a hard cap of **7 days**
on the `start_date` / `end_date` window per call (date span > 7 days
returns 400). Captured in commit `0266935`.

**Phase 1 impact.** None — Phase 1 polling is per-Map (single-order
lookups via `getOrderDetails`). Documented for whoever wires bulk
discovery in a later phase.

**SPEC change required.** Add a note under §11.3 that bulk-walking
EE orders requires page-cursoring with sliding 7-day windows.

---

## 5. `marketplaceId` filter on `getOrderDetails` — single-customer
deployments hint at marketplace boundary

**Where in SPEC.md**: §11.1 — the Customer ↔ Channel mapping (per the
§8b open item at line 1876).

**Finding.** On a Harmony tenant running both B2B and B2C orders, the
`marketplaceId` query parameter on `getOrderDetails` discriminates
which marketplace's orders are returned. For MMPL, `marketplaceId=65`
returns the B2B-marketplace orders.

**Phase 1 impact.** Validated as a workable discriminator for the
"Customer→Channel mapping for B2B" open item raised in §8b. The Phase 1
polling reconciler keys on `reference_code` so the marketplace filter
isn't strictly needed at the per-Map level, but the FDE's bulk views
and the Phase 2 invoice-flow design will need it.

**SPEC change required.** Document the marketplace_id discrimination in
§11.1 and resolve the §8b open item — the marketplace lookup
mechanism for B2B is "the EE Account's configured B2B marketplaceId,
queryable via `getMarketplaceList`."

---

## 6. The §11 module discriminator (`Old B2B` vs `New B2B`)

**Where in SPEC.md**: §11.3.1 — the createOrder endpoint contract.

**Finding.** EasyEcom has two architecturally distinct B2B push paths,
both live concurrently on Harmony:

- **Old B2B** — `/Wholesale/createOrder`, the legacy wholesale order
  endpoint.
- **New B2B** — `/webhook/v2/createOrder`, the unified order endpoint
  (also handles STN orders via `orderType = "stocktransferorder"` per
  §10.G).

Phase 1 substrate routes per `EasyEcom Account.ecs_b2b_module` (Select:
`Old B2B` / `New B2B`) and dispatches to the matching `build_*_payload`.
Live-verified for both during the build.

**SPEC change required.** §11.3.1 should explicitly enumerate the two
endpoints and the Account-level discriminator, instead of describing
a single createOrder path.

---

## 7. §10's `/webhook/v2/createOrder` endpoint is shared with §11 New B2B

**Where in SPEC.md**: §10 vs §11 architectural relationship.

**Finding.** §10 STN's createStockTransfer + §10 B2B branch and §11
New B2B push all hit the same EasyEcom endpoint
(`/webhook/v2/createOrder`); discrimination is by the body field
`orderType`:

| orderType | Flow |
|---|---|
| `"stocktransferorder"` | §10 STN branch |
| `"businessorder"` / `"B2B"` | §10 B2B branch + §11 New B2B push |
| `"B2C"` | §12 (later) |

**SPEC change required.** Add a cross-reference note in both §10
and §11 that the createOrder endpoint is shared and that the
`orderType` body field is the load-bearing discriminator. Old B2B
(§11) is the only path that doesn't share this endpoint.

---

## 8. `orderDate` requires explicit `+05:30` IST offset in createOrder body

**Where in SPEC.md**: §11.3.1 / §11.3.2 — the `/webhook/v2/createOrder`
body builder for New B2B and `/Wholesale/createOrder` for Old B2B.

**The defect.** Phase 1 sent `orderDate` as a naive ISO datetime
(no timezone suffix). EE's UI display of the order timestamp depends
on the client's expected timezone; Harmony showed orders pushed at
14:00 IST as 08:30 UTC in its lists.

**The fix.** Format `orderDate` as `YYYY-MM-DDTHH:MM:SS+05:30` so the
date *and* time match what the client typed in ERPNext. Commit
`b5471ad` (PR #100).

**SPEC change required.** Replace any "ISO 8601 datetime" wording in
the createOrder payload tables with "ISO 8601 datetime including
explicit IST offset (`+05:30`)". A naked Z-suffix UTC ISO is also
acceptable but loses the wall-clock alignment in EE's UI for IST
clients.

---

## 9. Polling must backfill missing `OrderID` / `SuborderID` / `InvoiceID` on the Map

**Where in SPEC.md**: §11.3 (polling) and §11.1 (Map population
semantics).

**The defect.** New B2B push returns `"Successfully Queued"` with
no IDs in the response body. The Map row is created with empty
`ee_order_id` / `ee_suborder_id` / `ee_invoice_id`. The
Phase 1 polling derivation focused on status transitions
(Cancelled / Invoice Pending / partial-cancel) and silently skipped
ID backfill when no transition was needed — so the Map sat with null
IDs until status changed, indefinitely breaking the §17 worklist card
"New B2B orders missing IDs (2h+)" against healthy orders just
waiting in Pushed/Queued state.

**Live surfacing.** Thuraya end-to-end smoke for
`SAL-ORD-2026-00022` (2026-06-28). EE `getOrderDetails` returned the
real OrderID/SuborderID/InvoiceID but our polling derivation returned
`("no_change", None)` and left the Map row's IDs as null.

**The fix.** New function `_backfill_ee_ids_if_missing` runs before
derivation. Inspects the EE response, writes missing IDs back onto
the Map row. Idempotent (only writes when local is null). PR #101 /
commit `2642842`.

**SPEC change required.** Add a note to §11.3 polling description:
"On every poll tick, before status derivation, missing
`OrderID` / `SuborderID` / `InvoiceID` are backfilled onto the Map
row from the EE response — separate from status transitions so
healthy New B2B orders don't sit with null IDs."

---

## 10. Fast-confirm queue check for New B2B push (60× latency reduction)

**Where in SPEC.md**: §11.3.5 (newly identified subsection — currently
covered only obliquely in the New B2B push lifecycle description).

**The defect.** New B2B push returns just a `queueId`; the
OrderID/SuborderID/InvoiceID are populated only when EE processes
the queue. Phase 1 relied on the */5 polling cron to backfill,
which meant a typical New B2B push sat in `Queued` state for ~2-5
minutes before IDs landed — making fast follow-on flows (Custom GSP
invoice generation, cancellations) wait that long for the Map to
be queryable.

**The fix.** Immediately after the createOrder response, poll
`/getQueueStatus?queueId=<id>` with a tight backoff (1s, 2s, 4s,
8s — cap ~15s total) within the same SO Push Queue Job. When EE
confirms processing, capture the IDs synchronously and update the
Map. This shrinks the Pushed → Queued → ID-populated window from
minutes to seconds. PR #102 / commit `0bd0bd7`.

**Phase 1 impact.** The */5 polling backfill (patch note 9) remains
as the safety net for queue checks that exceed the inline cap — so
the system stays correct even when EE's queue processing is slow.

**SPEC change required.** Add §11.3.5 ("Fast-confirm queue check —
New B2B only") describing the synchronous queue-poll inside the push
job. Document the cap, the fallback to */5 polling, and the
correctness/latency trade-off.

---

## 11. §11.5.2 Mode 2 (Branch B) shipped — EE-generated invoice mirror

**Where in SPEC.md**: §11.5 / §11.5.2 — the Branch B invoice mirror
flow listed in §11.1's flow diagram as "EE dispatch event → ERPNext
creates Sales Invoice mirroring EE invoice / Stock Reservation
released, Delivery Note created, stock moves".

**Built in PR #103** (commit `6b5b68a`).

**Deviations from the spec wording:**
- **Polling-driven, not webhook-driven.** Phase 1 webhook receivers
  are not built (patch note 9 establishes the polling-as-recovery
  pattern). When EE's polling response carries `invoice_number` on a
  businessorder row, derivation returns `("transition_to", "Invoice
  Pending")` and the mirror function runs inline. EE webhook
  `invoice.generated` is a Phase 3 enhancement that would plug into
  the same mirror function.
- **No Delivery Note auto-creation.** The mirror creates a Draft
  Sales Invoice; it does not create a DN. Stock movement happens
  through the standard SI submit pathway. DN auto-creation was
  explicitly out-of-scope per the design call.
- **1% variance check.** The mirror computes ERPNext-side totals
  (from resolved Customer Map + Item Map + Tax Templates) and
  compares against EE's reported `grand_total`. >1% diverge raises
  an `InvoiceMirrorVariance` Discrepancy but persists the Draft SI
  so the FDE can review.
- **Idempotency** keyed on `EasyEcom B2B Order Map.sales_invoice`
  (one mirror per Map; re-poll skipped) plus
  `Sales Invoice.ecs_easyecom_invoice_id` lookup as a second guard.
- **EE invoice_id source.** EE sends the invoice metadata via
  multiple plausible fields across payloads (`invoice_id`,
  `invoiceId`, `docs.invoice_id`, etc.) — the IRN extractor scans
  candidate field names in a deterministic order.

**SPEC change required.** Rewrite §11.5.2 around the polling-driven
flow (not webhook), explicitly call out the no-DN choice, document
the 1% variance threshold + Discrepancy class, and reference the
candidate-field-scanning pattern in `_extract_irn_fields`.

---

## 12. §11.5.1 Mode 1 (Branch A) shipped — Custom GSP, ERPNext mints IRN

**Where in SPEC.md**: §11.5 / §11.5.1 — the Branch A invoice-request
flow listed as "EE webhook: invoice_request received → ERPNext
generates Sales Invoice → e-waybill via india_compliance → returns
to EE".

**Built in PR #104** (commits `8fdee33` + toggle/print-format
follow-ons `8b6aa5e`, `1e862f5`, `60c76c9`).

**Architectural shift from spec wording — we are the GSP, not a
consumer of EE's GSP.** EE's "Custom GSP" feature lets EE call YOUR
configured endpoint as if you were a third-party GSP. We expose
three whitelisted endpoints (`/gettoken`, `/einvoice/update`,
`/ewaybill/update`) per EE's contract; EE invokes us when an EE-side
user clicks "Generate Invoice". This is functionally Branch A but
the trigger is EE → us (synchronous HTTP), not the EE webhook
described in spec line 2534.

**What ships:**
- Three whitelisted endpoints with `allow_guest=True`
- Bearer auth: Basic auth on `/gettoken` → mints a 1-hour Bearer
  (`EasyEcom GSP Token` DocType, SHA-256 hash-only storage)
- SI find-or-create via `find_or_create_si_for_gsp` — 3-tier
  lookup (idempotency anchor → Map.sales_invoice → reference_code +
  mirror)
- `mint_irn_for_si` / `mint_eway_for_si` invoke India Compliance's
  `generate_e_invoice` / `generate_e_waybill`
- Per-Account toggles `gsp_mint_einvoice` / `gsp_mint_ewaybill`
  (both default ON; OFF skips the NIC IRP / NIC EWB call — SI is
  still created/submitted, response carries empty IRN/EWB fields
  but populated PDF URL)
- Per-Account `gsp_print_format` / `gsp_ewaybill_print_format`
  Link fields → Print Format (blank → "Standard" / "e-Waybill")

**Six new Custom Fields on EasyEcom Account** (in a collapsible
"Custom GSP (§11.5.1 Mode 1)" section):
`gsp_basic_auth_secret`, `gsp_mint_einvoice`, `gsp_mint_ewaybill`,
`gsp_print_format`, `gsp_ewaybill_print_format` + a section break.

**One new DocType:** `EasyEcom GSP Token` (Bearer hash storage, FDE
read-only, daily scheduler purges past-expiry tokens after a 7-day
audit window).

**Sync Record `direction` enum extended** with `Inbound API`
(EE → us calls) and `Cancel` (already used by the cancellation flow).

**Defaults to mint-on for backwards compatibility.** A pre-toggle
client picks up Mode 1 with full NIC IRP + NIC EWB minting on the
first polling cycle after migrate — the toggles only matter when a
client explicitly turns them off.

**SPEC change required.** Rewrite §11.5.1 around the
we-are-the-GSP-not-the-consumer model. Document the three
endpoints, the auth flow (Basic → Bearer), the four-toggle matrix
(both ON / E-inv only / EWB only / both OFF) with the response
shape per combination, and the Print Format selector. Cross-link
the comprehensive guide
(`process/primers/GUIDE_custom_gsp_invoice_flow.md`) which is the
operating manual.

---

## 13. §11.6 shipped — lightweight Custom Fields on SI, not workflow status

**Where in SPEC.md**: §11.6 ("SI to delivery completion", lines
2637-2643) and §11.1 flow diagram (lines 2539, 2543-2544).

**Built in PR #105** (commit `a856003`).

**The spec wording vs what we built:**

| Spec line | Spec says | We built |
|---|---|---|
| 2640 | "ERPNext SI moves to Delivered status (**custom workflow status**; standard SI doesn't have Delivered)" | Custom Field `ecs_easyecom_dispatch_status` (Select), NOT a Frappe Workflow |
| 2544 | Branch B: "**Delivery Note created**, stock moves" | No DN creation. Stock moves on SI submit per the existing pathway |
| 2639 | "fires **confirmation webhook**" | Polling-driven (every 5 min). Webhook is Phase 2+ across all of §11. |
| 2642 | "Stock Reservation Entry is **consumed automatically**" | No SRE handling — §11.4 (SRE mirror) was explicitly parked |

**Why lightweight, not workflow + DN:** The heavier mechanism
(Frappe Workflow with state transitions + role permissions + DN
auto-creation with warehouse/item resolution + accounting hooks) was
discussed during the design call and deferred. Clients want
visibility into EE-side fulfilment state; they don't (yet) want a
new actor in their inbox creating DNs they didn't trigger. The
lightweight option (Custom Fields + a report) ships the visibility
without the workflow surface area.

**What ships:**
- 4 Custom Fields on Sales Invoice in the existing "EasyEcom
  Integration" section: `ecs_easyecom_dispatch_status`,
  `ecs_easyecom_dispatched_at`, `ecs_easyecom_delivered_at`,
  `ecs_easyecom_tracking_url`
- `DISPATCH_STATUS_BY_ID` mapping for EE status_id 1-7/9/30
- `TRACKING_URL_CANDIDATE_KEYS` defensive scan (5 plausible field
  names — `tracking_link`, `tracking_url`, `track_link`,
  `shipping_track_link`, `courier_tracking_url`)
- `_stamp_dispatch_status_on_si` in `polling.py` runs after
  `_apply_decision` on every poll (so Shipped → Delivered
  transitions land even on quiet `no_change` ticks)
- `B2B Dispatch Status` Script Report (Pending → Shipped →
  Delivered sort, red age-days >= 7 on stuck rows)
- Silent on failure — never breaks polling if the SI lacks the
  Custom Fields (rolling deploy) or the DB write errors

**SPEC change required.** Rewrite §11.6 around the lightweight
Custom-Fields model. Move the "custom workflow status" and "DN
auto-creation" wording into a Phase 3 enhancement note. Update §11.1
flow diagram lines 2539 / 2544 to show "EE polling tick → SI
dispatch fields stamped" instead of webhook + DN creation.

---

## Closeout

Patch notes 1-7 are Phase 1 grounding corrections (2026-05-30 …
2026-06-23). Patch notes 8-13 are Phase 2 build deltas (2026-06-28 …
2026-06-29) — orderDate offset, polling backfill, fast-confirm,
Mode 2 mirror, Mode 1 Custom GSP, §11.6 lightweight dispatch.

Folding all 13 items into `SPEC.md §11` is a methodology-team task
(this build does not edit `SPEC.md` per CLAUDE.md). The Phase 3
build packet (webhook receivers, SRE mirror, multi-warehouse split,
sync push mode, workflow-status + DN heavier options) should start
from a `SPEC.md §11` that already has these patches applied so the
assumptions don't recurse.
