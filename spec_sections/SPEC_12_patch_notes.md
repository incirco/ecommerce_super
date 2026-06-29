# §12 B2C / D2C / Marketplace — SPEC Patch Notes

Inline corrections + architectural decisions captured during the §12
Phase 1 build (2026-06-29) that the methodology team needs to fold
back into `SPEC.md §12`. Same shape as `SPEC_11_patch_notes.md`,
`SPEC_8d_patch_notes.md`, `SPEC_8e_patch_notes.md`, etc.

Per CLAUDE.md rule 0, this build does NOT edit `SPEC.md` directly.
These notes are the methodology team's hand-off; the next build packet
(or §102 backfill, which is the immediate downstream consumer) should
start from a `SPEC.md §12` with these patches folded in.

Each entry: where in SPEC, the defect/decision, the fix shipped, the
SPEC change required.

Tracking: PR #107 (main build), PR #108 (§12.9 1-paisa total
variance follow-up).

---

## 1. `EasyEcom Marketplace Account` DocType built as §12 substrate

**Where in SPEC.md**: §8.6.2 line 1856-1862 explicitly defers this
DocType to the recon/settlement build:

> "Build timing: NOT part of the Channel packet (8b). Every field
> here … is a *settlement/reconciliation* concern, not a channel-
> discovery concern. The Marketplace Account is therefore built when
> reconciliation/settlement is built…"

**The decision.** §12 polling needs a per-(Company, Marketplace) row
to carry: the polling cursor (`last_pull_orders`), routing to the
right EE Account, pool-customer links, enabled/cadence config. Without
this row §12 can't run.

**The fix.** Built `EasyEcom Marketplace Account` as §12 substrate
with a deliberately minimal v1 field set. Settlement-related fields
(`settlement_template`, `rate_card_subscriptions`, full GSTIN) stay
deferred to the recon-engine build as the spec originally intended.

v1 fields shipped:
- `marketplace` (Link → Marketplace)
- `company` (Link → Company)
- `seller_id` (sanity-check string)
- `enabled` (pause toggle)
- `pseudo_customer_in_state` + `pseudo_customer_out_of_state`
  (Customer Link pair — see patch note 6)
- `easyecom_account` (Link → EE Account, drives JWT routing)
- `last_pull_orders` (Datetime cursor)
- `polling_cadence_minutes` (Int, default 5)
- `polling_status_filter` (Data, default "Manifested")
- `last_pull_error`, `last_pull_at` (audit)

Autoname: `format:ECS-MA-{company}-{marketplace}` — (Company,
Marketplace) is the unique key.

**SPEC change required.** Move the §8.6.2 deferral note from "built
with recon/settlement" to "built with §12 as substrate; settlement
fields layered in by recon work". Add a v1 vs v2 field table so
the recon-engine build knows what's already shipped vs what it
extends.

---

## 2. Path 2 tax model — EE-supplied wins; ERPNext is the variance check

**Where in SPEC.md**:
- §12.4 line 2780: "line.tax → (applied via ERPNext Item Tax
  Template, sanity-checked against EE) — **ERPNext-derived tax wins**;
  EE variance > 1% raises Discrepancy"
- §12.5 line 2792: "Tax computation uses ERPNext Item Tax Template —
  **never EE-supplied tax** — to ensure tax correctness"

**The decision** (locked 2026-06-29 design call). The marketplace is
the system that generated the invoice; the buyer was charged whatever
the marketplace computed; settlement will reconcile against the
marketplace's number. Treating ERPNext as the tax authority means
every B2C SI's GL is structurally off by the tax delta from what the
marketplace actually settled — recon variance becomes the norm, not
the exception, and the real signal (actual reconciliation problems)
drowns in noise.

Path 2 inverts spec §12.5:
- **SI.taxes** carries EE-supplied tax (single 'Actual' row) — the GL
  truth, the IRN basis (if IRN ever fires), the settlement target
- **`ecs_erpnext_tax_check_total`** Custom Field stores the ERPNext-
  computed tax (via HSN default rate × line value) as a variance
  signal
- **>1% delta raises an Integration Discrepancy** as an upstream-issue
  alert. The Discrepancy is informational; SI data is **immutable**;
  FDE investigates the upstream cause (HSN misconfiguration, tax
  category drift, marketplace adapter bug)

**The fix.** `_check_variance()` in
`flows/b2c_sales/invoice_builder.py`. Variance computed only when
both ee_tax_total and erpnext_tax_check are non-zero (avoids false
positives on installs without HSN configured).

Plus follow-on PR #108: `_check_total_variance()` for the 1-paisa
order-total check per §12.9 — independent of the tax check, catches
discount mishandling + missing line items + rounding mode bugs.

**SPEC change required.**
- Rewrite §12.4 line 2780 to reflect EE-tax-wins
- Rewrite §12.5 entirely around Path 2; the "ensure tax correctness"
  justification was correct in intent (recon shouldn't have to absorb
  silent tax drift) but the mechanism was inverted (the recon system
  needs to MATCH what the marketplace charged, not OVERRIDE it)
- Document the variance-as-upstream-alert pattern: Discrepancy fires
  when ERPNext computation diverges from EE by >1% (or 1 paisa on
  total); SI is never amended; FDE investigates upstream cause

---

## 3. Polling-only; webhook receivers deferred

**Where in SPEC.md**: §12.3.2 lines 2756-2760:

> - Polling cron every 5 minutes per operational location
> - Webhook of type `ready_to_dispatch` knocks the polling cycle into
>   immediate execution
> - Webhook of type `manifested` is the canonical trigger

**The decision.** Webhook receivers were deferred across all of §11
(see SPEC_11_patch_notes entries 2 and 9 — polling is the recovery
path for state changes). §12 inherits the same deferral: webhooks
are Phase 2+ across the integration.

**Phase 1 impact.** None — polling at the spec's 5-minute cadence
satisfies the operational need. The webhook would just reduce
latency-to-SI from ~5 min worst case to ~5 seconds. Phase 2 wiring
plugs into the existing handler functions unchanged.

**The fix.** Polling-only at `*/5 * * * *` per Marketplace Account.
Scheduler entry: `flows.b2c_sales.polling.reconcile_all_marketplace_accounts`.
Per-tick eligibility: `enabled=1` AND `easyecom_account` set AND
cursor past per-Account cadence.

**SPEC change required.** §12.3.2 should reflect that polling is the
sole trigger in v1, with a note that webhook trigger is Phase 2+
across §11 and §12 (deferred consistently).

---

## 4. No e-invoice IRN minting from §12; marketplace/EE owns it

**Where in SPEC.md**: §12.6 lines 2796-2800:

> - B2C invoices ≥ ₹50,000 require e-invoice IRN per Indian GST rules
> - India Compliance app handles IRN generation
> - Integration triggers IRN generation immediately after SI submit,
>   before EE acknowledgement
> - If IRN generation fails: SI is in Submitted status with no IRN…

**The decision** (2026-06-29 design call). B2C marketplace orders
rarely cross the ₹50k threshold; when they do, the marketplace or
EE-side adapter handles IRN minting (Amazon's TaxInvoice service,
marketplace-side GSP integrations, etc.). If we mint IRN from
ERPNext as well, we get duplicate IRNs on NIC IRP — which cannot be
deleted, only cancelled via NIC support.

**The fix.** §12 SI builder does **not** call
`generate_e_invoice`. SI is created in Draft state and remains there
until standard ERPNext submit (or stays Draft per workflow). If EE's
order payload carries an IRN, we mirror it to the SI as a field (the
same pattern §11.5.2 Mode 2 uses for B2B EE-generated invoices).

**SPEC change required.** Rewrite §12.6 to:
- IRN minting is NOT part of §12 (Phase 1 or Phase 2)
- If EE sends an IRN in the payload (marketplace minted it), the
  integration mirrors it as a Custom Field on SI; never re-mints
- If a future client needs us to mint IRN for B2C, that's a separate
  build (probably gated on a per-Marketplace-Account toggle, similar
  to the §11.5.1 `gsp_mint_einvoice` toggle pattern)

---

## 5. `EasyEcom Marketplace Order Map` DocType dropped; per-order data on SI + Sync Record

**Where in SPEC.md**: §12.8 lines 2810-2817 — describes a separate
`Marketplace Order Map` DocType as the bridge between SI and
Settlement Lines for recon:

> "Created at SI creation time. Fields: marketplace,
> marketplace_order_id, channel, marketplace_account, sales_invoice
> (Link), settlement_status (Forecast / Partial / Settled / Disputed)"

Plus §12.11 line 2837: "Net Receivables view (PRD Section 10.3.1)
lists open Marketplace Order Map records grouped by marketplace and
forecast settlement date".

**The decision** (2026-06-29 review). Three reasons for keeping the
Map as a separate DocType were considered; only one held up under
scrutiny:

| Reason | Holds up? |
|---|---|
| Lifecycle separation (settlement state mutates over weeks; SI is immutable post-submit) | ⚠️ Weak — `update_modified=False` writes are already used elsewhere (e.g. §11.6 dispatch status on SI). Same pattern works |
| Payload audit (storing full JSON on SI bloats the SI table) | ✅ Legit — but `EasyEcom Sync Record` (§6/§7 substrate) already exists for exactly this purpose across every other flow |
| Future-proof for split orders (one Order_id → N Invoice_id → N SIs) | ❌ Doesn't matter — already 1:1 with SI since dedup keys on Invoice_id |

Refactor in same iteration (commit `1153070`):
- Map DocType deleted entirely
- Settlement lifecycle moved to SI Custom Fields in a new
  "EasyEcom Settlement" collapsible section:
  - `ecs_settlement_status` (Select)
  - `ecs_expected_settlement_date` (Date)
  - `ecs_settlement_completed_at` (Datetime)
- EE payload audit moved to `EasyEcom Sync Record` per polled order
  (`direction = "Pull"`, `entity_type = "Sales Invoice"`,
  `last_response_payload` carries the JSON,
  `pull_payload_hash` carries SHA-256)

**Net result.** Zero new DocTypes for per-order recon overhead.
Recon engine joins Settlement Lines → SI directly:
```
Settlement Line.marketplace_order_id → SI.ecs_marketplace_order_id
```
The Map's role as a join target is preserved (it just lives on the SI
itself now); the recon engine reads `ecs_settlement_status` /
`ecs_expected_settlement_date` directly from the SI for grouping.

**SPEC change required.**
- Rewrite §12.8 around the SI-as-the-Map model (the SI Custom Fields
  ARE the bridge; no separate DocType)
- Update §12.11 line 2837 ("Net Receivables view lists open Marketplace
  Order Map records") to read "lists open SIs grouped by ecs_marketplace
  + ecs_settlement_status"
- Update §12.11 line 2835 to clarify the recon join is direct:
  Settlement Line → SI via `ecs_marketplace_order_id`

---

## 6. Two pseudo-customers per Account (in-state + out-of-state) for GST split

**Where in SPEC.md**: §12.2 line 2743:

> "Customer is anonymised. Per Section 8.2.1, marketplace orders use
> a **per-marketplace pseudo-customer** (Amazon FBA Buyer Pool etc.)."

(Singular — one pseudo-customer per marketplace.)

**The decision** (2026-06-29 review). Even though Path 2 uses EE's
tax amount directly (the SI's tax_amount value comes from EE), the
SI still needs the right `tax_category` on the linked Customer so
the GST split lands in the correct account heads:

- Intra-state shipment (buyer in same state as seller) → CGST +
  SGST split required for GSTR-1 / GSTR-3B
- Inter-state shipment (different states) → IGST required

With a single pseudo-customer per marketplace, you can't differentiate
— you'd dump everything into one tax_category, and the resulting
return filings break (or require post-hoc fixing).

**The fix.** Marketplace Account holds TWO `Customer` Link fields:
- `pseudo_customer_in_state` — auto-created as
  `<Marketplace> B2C In-State - <Company>` with
  `tax_category = "In-State"` (India Compliance default)
- `pseudo_customer_out_of_state` — auto-created as
  `<Marketplace> B2C Out-of-State - <Company>` with
  `tax_category = "Out-of-State"`

Both bootstrap in the `after_insert` controller hook. Idempotent
(re-links existing Customer rows). Tax category falls back to None
if the Tax Category doesn't exist on the bench (FDE re-points
manually if a different naming scheme is used).

SI builder's `_resolve_pool_customer`:
1. Resolves Company's state via `Company.state` OR derives from the
   first 2 chars of GSTIN (Indian state-code map embedded in builder)
2. Resolves shipping address state from the EE payload (scans 5
   plausible field names: `shipping_address.state`,
   `shippingAddress.stateName`, `shipping_state`, etc.)
3. Case-insensitive comparison → picks `in_state` or `out_of_state`
4. Defaults to in-state pool when shipping state can't be resolved
   (safer to over-charge CGST+SGST — variance surfaces — than
   under-charge IGST silently)
5. Raises B2CBuilderError if the required pool is missing (FDE re-
   bootstraps by resaving the Marketplace Account)

**SPEC change required.** Rewrite §12.2 line 2743 to describe the
two-pool model (in-state + out-of-state) with the GST-split
rationale. Cross-reference §8.2.1 (Pseudo-Customers) — that section
likely needs an aligned update to describe per-(marketplace ×
Company × in/out-state) granularity instead of per-marketplace.

Also worth adding: a note that the actual buyer's shipping address
goes on the SI's standard Shipping Address (separate Frappe Address
doctype), NOT the Customer master. The pseudo-customer is purely a
tax-category carrier; the per-order buyer address is captured on
the SI itself.

---

## Closeout

Six items — all by-design decisions, each surfaced during the
2026-06-29 build + review cycle and locked at the design call. The
build PR (#107) and follow-up (#108) are both merged to main; the
§12 Phase 1 implementation matches each fold-back description above.

Methodology team's task: rewrite `SPEC.md §12` (and the cross-
references in §8.2.1, §8.6.2, §12.11) to reflect these decisions.
The next downstream build (§102 B2C backfill, which is strict-blocked
on §12 per its draft packet) should start from a patched spec so the
backfill produces records consistent with the live §12 shape.

Live-verification status:
- Phase 1 (unit-test coverage): 74 tests green (initial 75 minus 1
  consolidated during smoke fixes)
- Phase 2 (live smoke against Harmony, 2026-06-29): end-to-end SI
  creation from real EE payloads succeeded (PR #110). 3 Sales
  Invoices minted (SINV-26-00009/10/11) against marketplace_id=10
  retailorder rows. 11 misalignments between code and real bench /
  real EE payloads surfaced and fixed — 4 of which are EE-contract
  grounding corrections worth folding back into SPEC §7 / §12 (see
  patch notes 7-10 below).

---

## 7. EE `getAllOrders` returns `suborders`, not `order_items` (cross-endpoint inconsistency)

**Where in SPEC.md**: §12.3.2 / §12.4 (the EE payload shape
referenced for SI line-item resolution); also §7 (the EE response
shape contract).

**The finding** (live-smoke 2026-06-29 against Harmony, invoice_id
657765959 + retailorder rows 636253870 / 638702157 / 643904104).

EE uses **different field names for the per-line array across
endpoints**:
- `/orders/V2/getOrderDetails` returns `order_items` (per §11
  patch note 1 — renamed during §11 Phase 1 build from the
  originally-assumed `suborders`)
- `/orders/V2/getAllOrders` returns `suborders` (the original name;
  the §11 rename does not apply here)

This is an EE-side inconsistency, not a build defect. The two
endpoints carry the same per-line shape (`sku`, `item_quantity`,
`breakup_types`, `cancelled_quantity`, etc.) — only the parent key
differs.

**The fix.** `_resolve_line_items` in
`flows/b2c_sales/invoice_builder.py` scans `suborders` first, falls
back to `order_items` / `orderItems`. Defensive across both
endpoints. Commit `b79910c`.

**SPEC change required.** Add a note to §7 (EE contract / response
shape) and to §12.4 (SI field-level mapping):
"EE per-line array key is `suborders` on /orders/V2/getAllOrders
and `order_items` on /orders/V2/getOrderDetails. Parser must scan
both for portability across endpoints."

Cross-link this to SPEC_11_patch_notes entry 1 (the original
rename) so the methodology team sees the full picture.

---

## 8. EE `getAllOrders` `status` filter is not strict — non-B2C orders leak through

**Where in SPEC.md**: §12.3.2 (the polling-cron status filter) and
§12.9 (per-record failure handling for unknown marketplace_id).

**The finding.** A 90-day sweep against Harmony with
`status=Manifested` returned **58 orders across 13 windows**, but:
- 55 had `marketplace_id=64` (EE-internal B2B + STN channel — our
  own §11 push test orders + §10 STN test orders)
- 3 had `marketplace_id=10` (Offline retailorder)
- The "marketplace" filter we expected EE to honor was effectively
  ignored — EE returned every order with status Manifested on
  the EE Account, regardless of marketplace or order_type_key

This is an EE-side behaviour; the `status` filter on getAllOrders
filters by manifest status only, not by marketplace.

**Phase 1 impact.** §12 polling cron picks up B2B + STN orders on
benches where the same EE Account hosts both flows. Without per-
record guards, the §12 SI builder would attempt to create marketplace
SIs from B2B / STN orders → builds either fail (validation) or
worse, create incorrect SIs.

**The fix.** Two per-record guards in `build_si_from_ee_order`:
1. `marketplace_id` mismatch guard: raises `B2CBuilderError` when
   the EE row's `marketplace_id` doesn't match the Marketplace
   Account's `marketplace` field. Per-record dispatch catches +
   logs → batch continues.
2. `order_type_key in {'businessorder', 'stocktransferorder'}`
   guard: rejects §10 / §11 territory rows even when the
   marketplace_id matches (defense against FDE misconfiguring a
   Marketplace Account at `marketplace_id=64`).

Both errors yield clean Failed Sync Records with actionable
reasons. Commits `4442f82`, `b79910c`.

**SPEC change required.** §12.3.2 should note that EE's
`status=Manifested` filter is per-manifest-status only, not per-
marketplace; §12.3.2 / §12.9 should describe the per-record
marketplace_id + order_type_key guards as required defenses.

---

## 9. EE `getAllOrders` flattens shipping address into the order row

**Where in SPEC.md**: §12.4 line 2774-2775 (the `shipping_address`
field mapping).

**The finding** (live-smoke 2026-06-29 — retailorder invoice_id
643904104).

EE's `getAllOrders` does **not** nest the shipping address under
a `shipping_address` object. Instead, the address fields are
flat at the order_row root with bare names:
- `state` (e.g. "Uttar Pradesh")
- `state_code` (e.g. "09")
- `city`, `pin_code`, `address_line_1`, `address_line_2`
- `email`, `contact_num`
- `billing_state`, `billing_pin_code`, `billing_address_1`, etc.

`/orders/V2/getOrderDetails` likely uses a different (nested) shape
— but `getAllOrders` is flat. SPEC §12.4 implicitly assumed a nested
`shipping_address` object.

**The fix.** `_resolve_shipping_state` in builder now scans `state`
as a fallback candidate after the spec-era names. Commit `b79910c`.

**SPEC change required.** §12.4 field-mapping table should note
that EE's `getAllOrders` flattens shipping address into the order
row with bare field names, and the integration scans both the
flat-root convention and the nested `shipping_address` convention
for portability.

---

## 10. EE `getAllOrders` uses `total_amount`, not `invoice_amount` / `grand_total`

**Where in SPEC.md**: §12.4 (the SI grand_total mapping) and §7 (EE
response shape contract).

**The finding** (live-smoke 2026-06-29 — retailorder rows).

The original §12 builder's `ee_grand_total` candidate list was
`invoice_amount` / `grand_total` / `total` — none of which are
present in the `getAllOrders` payload. The actual field is
`total_amount` (a Decimal-as-string like `"1000.0000"`).

Builder was capturing `ee_grand_total = 0.0` on every SI from the
smoke until the candidate list was extended. This silently broke
the §12.9 1-paisa total variance check (compared `ee_total=0` against
`SI.grand_total=100/600/40` and reported 0 paise variance because
both compared as zero).

**The fix.** `total_amount` added as the first candidate, ahead of
the original three (kept as fallbacks for portability across
endpoints / EE versions). Commit `b79910c`.

**SPEC change required.** §12.4 field-mapping table should name
`total_amount` as the canonical EE field for SI grand_total in the
`getAllOrders` shape, with the spec-era names as fallbacks.

---

## Closeout (updated)

Ten items now — six original architectural deviations (notes 1-6)
plus four EE-contract grounding corrections from the 2026-06-29
Harmony smoke (notes 7-10). The Harmony smoke that surfaced 7-10
also caught 7 implementation-side bench-portability issues (Tax
Category naming, leaf-group filtering, Location key vs docname,
cursor advance, default_tax_account v15-vs-v16, SoT Map field
type, Customer Group/Territory parents) — all fixed inline in
PR #110, not patch-note-worthy because they're internal
ERPNext/Frappe details rather than spec changes.

All notes are by-design / live-verified. The §12 Phase 1
implementation matches each fold-back description. Methodology
team rewrites `SPEC.md §12` accordingly so §102 B2C backfill
starts from a patched spec.

Live-verification status: ✅ end-to-end smoke against Harmony
2026-06-29 (PR #110). 3 SIs minted, Sync Records written, variance
checks ran. Pending: out-of-state pool selection + ERPNext tax
cross-check, both blocked on bench data (Company.state unset, HSN
rates not configured on test Items) rather than code gaps.
