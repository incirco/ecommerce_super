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

## Closeout

These seven items are the inline grounding corrections from the
Phase 1 build. Folding them into `SPEC.md §11` is a methodology-team
task (this build does not edit `SPEC.md` per CLAUDE.md). The Phase 2
build packet should start from a `SPEC.md §11` that already has these
patches applied so the assumptions don't recurse.
