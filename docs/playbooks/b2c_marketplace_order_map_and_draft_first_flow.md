# B2C Marketplace Order Map + Draft-First SI Flow — Ops Playbook

**Audience**: FDE / Ops maintaining B2C polling on a live client bench
**Scope**: PRs #272–#276 (Aug 2026 rework)
**Related SPEC amendment**: `drafts/spec_sections/spec_12_amendment_restore_mp_order_map.draft.md` (awaiting methodology sign-off; see status below)

---

## 1. What changed and why

The Aug 2026 Puresta reconciliation exercise exposed four gaps in the B2C flow that this rework closes:

| Gap | Fix | PR |
|---|---|---|
| No order-grain bridge between marketplace order and SIs → split-shipment orders can't reconcile | Restored `EasyEcom Marketplace Order Map` + `EasyEcom Order Map Shipment` child | #272 |
| Every SI-creating flow needs to populate the Map for recon to work | `_upsert_marketplace_order_map` in `invoice_builder.py` | #273 |
| No periodic re-check → Drafts pile up, never transition | `pending_manifest_sweeper.py` (hourly cron) | #274 |
| Tax injection used flat `charge_type="Actual"` → India Compliance's IGST/CGST/SGST split never ran → GSTR-1 line detail missing | Switched to IC-native `taxes_and_charges` + per-item `item_tax_template` | #275 |
| Foreign-currency exports (CAD/SGD/NPR) stored as INR → 68x under-count on GL | `SI.currency` + `SI.conversion_rate` from ERPNext Currency Exchange + FX-confirm gate on sweeper | #276 |

Zero new custom fields on `tabSales Invoice` across all five PRs — everything lives on the Map + child, or on native ERPNext fields (`currency`, `conversion_rate`, `taxes_and_charges`).

---

## 2. The lifecycle picture

```
EE getAllOrders poll (every 5 min per Marketplace Account)
    │
    ▼
invoice_builder.build_si_from_ee_order
    ├─ resolves customer (pool, in-state/out-of-state)
    ├─ resolves line items (EE SKU → ERPNext item via Item Map)
    ├─ resolves posting_date (EE invoice_date, GST-legal per PR #270)
    ├─ resolves currency + conversion_rate (PR #276)
    ├─ resolves gst_category (PR #263 — URP vs Registered Regular)
    ├─ resolves place_of_supply (PR #263 — drives IC's tax split)
    ├─ resolves taxes_and_charges template (PR #275 — IC-native)
    ├─ resolves per-item item_tax_template (PR #275)
    └─ inserts SI as Draft (no .submit() call anywhere in the flow)
                │
                ├─ Upserts Marketplace Order Map + Shipment child (PR #273)
                │  Contains: invoice_number, manifest_date, batch_id,
                │  invoice_currency_code, conversion_rate,
                │  conversion_rate_confirmed, IRN/eway placeholders
                │
                └─ Writes EasyEcom Sync Record (audit trail)

    Draft SI sits waiting. Poll cycle ends.

    ────────────────── Hourly sweeper tick ──────────────────
                │
                ▼
    pending_manifest_sweeper.sweep_pending_manifest_sis
    │
    ├─ Query: all Draft SIs joined to Shipment where manifest_date IS NULL
    │  (i.e., "not yet manifested by EE")
    │
    ├─ For each pending row → _fetch_ee_order(invoice_id)
    │  Uses getAllOrders with invoice_id filter (per-record re-check)
    │
    └─ Branch by EE's current state:
       ├─ Manifested + Shipped
       │     └─ (FX gate) If foreign-currency + confirmed=0 → hold
       │     └─ Submit SI  (GL + stock ledger fire)
       │        Update Shipment.manifest_date
       │
       ├─ Manifested + Returned
       │     └─ Same as Shipped, plus create paired Credit Note
       │
       ├─ Cancelled (any manifest state)
       │     └─ Branch by gst_category:
       │        ├─ Unregistered (URP) → submit + immediately cancel
       │        │  Cancelled SI auto-excluded from GSTR-1
       │        │  (India Compliance queries docstatus=1 only)
       │        └─ Registered Regular (buyer has GSTIN)
       │           → submit + create Credit Note pair
       │             Both appear in GSTR-1; buyer reverses ITC
       │
       └─ Still transient (Confirmed / Manifest Scanned)
              └─ No-op. Re-check next hourly tick.
```

---

## 3. Ops daily checklist

### Poll health
- **Dashboard**: EasyEcom Workspace → EasyEcom Marketplace Account list. Look for `last_pull_orders` older than the account's `polling_cadence_minutes`.
- **Alert**: `EasyEcom Integration Discrepancy` rows with severity=High from `_check_variance` or `_check_total_variance` — these fire when EE's amounts don't match ERPNext's computed amounts within tolerance.

### Draft SI review (per Marketplace Account)
Query:
```sql
SELECT COUNT(*) FROM `tabSales Invoice`
WHERE docstatus = 0
  AND ecs_marketplace IS NOT NULL
  AND creation < NOW() - INTERVAL 24 HOUR;
```
If > 0 for more than 24 hours: something's stuck. Possible causes:
- Manifest never came (EE hung the shipment) — check the marketplace's own portal
- FX confirmation pending (foreign-currency invoice) — see §5 below
- Sweeper cron paused (check `bench schedule status`)

### FX confirmation queue (foreign-currency invoices only)
Query:
```sql
SELECT sh.parent AS mp_order_map, sh.invoice_number,
       sh.invoice_currency_code, sh.conversion_rate,
       sh.sales_invoice
FROM `tabEasyEcom Order Map Shipment` sh
JOIN `tabSales Invoice` si ON si.name = sh.sales_invoice
WHERE si.docstatus = 0
  AND sh.invoice_currency_code != 'INR'
  AND sh.conversion_rate_confirmed = 0;
```
For each row: verify `conversion_rate` against EE's tax export or portal for that `invoice_date`. If it matches, tick `Conversion Rate Confirmed by Ops` on the Shipment child row. Save the parent Map. Next hourly sweep will submit.

---

## 4. Recon reconciliation (per RECON_SPEC §2)

The recon app (`ecommerce_super_recon`) reads from ecommerce_super via a fixed interface:

| Source doctype | Fields recon reads | Populated by |
|---|---|---|
| `EasyEcom Marketplace Order Map` | `marketplace_order_id`, `marketplace`, `marketplace_account`, `company` | PR #273 (`_upsert_marketplace_order_map`) |
| `EasyEcom Order Map Shipment` | `sales_invoice`, per-shipment metadata | PR #273 (`_build_shipment_row`) |
| `Sales Invoice` | `ecs_marketplace_order_id`, `ecs_marketplace`, native `grand_total`/`base_net_total`/`items[]` | PR #263 + native ERPNext |
| `Sales Invoice` (recon writes) | `ecs_actual_net`, `ecs_variance_amount`, `ecs_variance_pct`, `ecs_settlement_status` | Recon engine (writes only) |

Anything else recon needs beyond this is a new interface contract — not a change to ecommerce_super's write pattern.

---

## 5. Foreign-currency (multi-currency) recovery procedures

### 5a. Currency Exchange row missing at SI insert
**Symptom**: `B2CBuilderError: no Currency Exchange rate for CAD → INR on 2026-07-31` in the polling log.
**SI status**: not created (poll aborted for that row).
**Fix**:
1. Setup → Currency Exchange → New
2. Fields: `from_currency=CAD`, `to_currency=INR`, `date=<invoice_date>`, `exchange_rate=<rate>`
3. Save
4. Re-poll the marketplace account (or wait for next cadence tick)

### 5b. Our rate differs from EE's rate (ops confirmation flow)
**Symptom**: Draft SI created; Shipment child has `conversion_rate=<our>`, `conversion_rate_confirmed=0`. Sweeper won't submit.
**Fix**:
1. Fetch EE's rate for that invoice_date (from EE tax export CSV or portal)
2. Open the Marketplace Order Map, navigate to the Shipment child row
3. If our rate is wrong: update `conversion_rate` to EE's rate, cascade to SI's `conversion_rate` via the parent Map's save hook
4. Tick `Conversion Rate Confirmed by Ops`
5. Save. Next hourly sweep will submit.

### 5c. Post-submit FX delta discovered
**Symptom**: Foreign-currency SI already submitted; later ops discovers EE's rate differs from ours.
**Fix**: This is FX gain/loss booking territory. Options:
- **Small delta**: leave the SI; book a monthly aggregate FX Gain/Loss Journal Entry against a dedicated GL account
- **Large delta**: cancel the SI (creates docstatus=2), re-poll after fixing the Currency Exchange row

### 5d. Long-term fix (EE side)
Once EE adds `conversion_rate` to `getAllOrders` response:
- Update `_resolve_currency_conversion_rate` to read `order_row.get("conversion_rate")` first, before falling back to ERPNext Currency Exchange
- Update `_build_shipment_row` to set `conversion_rate_source = "EE API (native)"` and auto-confirm
- FX gate becomes a no-op for all newly-polled orders

### 5e. Credit Note currency drift (post-audit MEDIUM-1)
**Symptom**: rare — a Credit Note's Shipment child has `invoice_currency_code` differing from the parent Marketplace Order Map's original SI.
**When it happens**: EE's payload on the return / cancel event carries a different `invoice_currency_code` than the payload on the original order. Extremely rare (EE normally emits currency consistently across an order's lifecycle) — but observed when the marketplace re-issues an invoice mid-cycle after a currency conversion policy change on their side.
**Impact**: the CN's `conversion_rate` was inherited from the original SI's rate — safe unless the currency itself changed. If EE now reports a different currency, the inherited rate is nonsense.
**Fix**:
1. Query the anomaly:
   ```sql
   SELECT sh.parent AS mp_order_map, sh.sales_invoice, sh.invoice_currency_code,
          orig.invoice_currency_code AS original_currency
   FROM `tabEasyEcom Order Map Shipment` sh
   JOIN `tabEasyEcom Order Map Shipment` orig ON orig.name = (
       SELECT o2.name FROM `tabEasyEcom Order Map Shipment` o2
       WHERE o2.parent = sh.parent AND o2.original_sales_invoice IS NULL
       LIMIT 1
   )
   WHERE sh.original_sales_invoice IS NOT NULL
     AND sh.invoice_currency_code != orig.invoice_currency_code;
   ```
2. For each result: verify with finance which currency is authoritative
3. Cancel the CN (if Draft: delete; if Submitted: cancel + re-issue via backfill flow with correct currency)
4. Manually re-book the reversal with the correct currency + rate

### 5f. Legacy foreign-currency Drafts after backfill (post-audit MEDIUM-2)
**Symptom**: pre-PR-#276 Draft foreign-currency SIs (created before the multi-currency wiring existed) get Shipment rows via the backfill patch with `conversion_rate_confirmed=0`. Sweeper won't submit them.
**Why**: at the time these SIs were created, our code didn't know to set `SI.currency` or `SI.conversion_rate` — foreign-currency amounts landed as if they were INR (the 68x under-count problem PR #276 fixed). The backfill can only mirror what's on the SI; it can't retroactively fix the currency handling.
**Recovery options**:

**Option A — cancel + re-issue (recommended)**
1. Query the affected SIs:
   ```sql
   SELECT sh.sales_invoice, sh.invoice_currency_code, sh.conversion_rate,
          si.grand_total, si.creation
   FROM `tabEasyEcom Order Map Shipment` sh
   JOIN `tabSales Invoice` si ON si.name = sh.sales_invoice
   WHERE sh.conversion_rate_source = 'Backfill from existing SI'
     AND sh.invoice_currency_code != 'INR'
     AND si.docstatus = 0
     AND sh.conversion_rate_confirmed = 0;
   ```
2. For each row: verify EE's current state via `bench execute ecommerce_super.easyecom.smoke_prechecks._puresta_probe_one_order.run --kwargs '{"invoice_id": <n>}'`
3. Delete the Draft SI (Draft = safe to delete)
4. Re-poll the marketplace account (or the specific `invoice_id` via a targeted probe) — new SI lands with correct multi-currency handling per PR #276

**Option B — accept + tick (only if the value is close to correct)**
1. Same query as Option A
2. For each row: compute `si.grand_total × <correct EE rate>` vs the current `si.base_grand_total`. If within 5%, may not be worth re-issuing.
3. Update `Shipment.conversion_rate` to the correct EE rate
4. Tick `Conversion Rate Confirmed by Ops`
5. Note: this leaves the SI with wrong item-level `rate` values (still in the pre-#276 mis-interpreted form). Sub-line-level recon will show as mismatched — accept as legacy noise.

**Puresta prod expectation**: ~0-4 rows affected (foreign B2C is rare, and most historical SIs are already submitted, not Draft). Ops action time: ~5 min per row via Option A.

---

## 6. GST compliance considerations

### 6a. URP vs Registered Regular branching (PR #263 + #274)
- **URP** (Unregistered Person): `buyer_gst` is blank / "NA" / "URP" → `gst_category = "Unregistered"` on SI
- **Registered Regular**: `buyer_gst` is a valid 15-char GSTIN → `gst_category = "Registered Regular"`, `billing_address_gstin` populated

**Cancellation behavior differs** — see the branching table in §2 above.

### 6b. Line-level IGST/CGST/SGST split (PR #275)
Every SI now has:
- `taxes_and_charges` = `"Output GST In-state - {abbr}"` (intra-state) or `"Output GST Out-state - {abbr}"` (inter-state)
- Each SI Item has `item_tax_template` = `"GST 5% - {abbr}"` / `"GST 12%"` / `"GST 18%"` / `"GST 28%"` / `"Nil-Rated"` based on EE's per-line tax_rate

**India Compliance's GSTR-1 report** now populates line-level detail correctly.

### 6c. Variance signal (unchanged)
`_check_variance` fires on every SI insert:
- Reads EE's `total_tax`
- Reads `_compute_erpnext_tax_check(line_items)` from ERPNext side
- Raises `EasyEcom Integration Discrepancy` if delta > 1% (configurable)

This is the safety net for template misconfiguration or EE-side computation drift.

---

## 7. Common troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Draft SIs accumulate for a marketplace | Sweeper cron not running | `bench --site <site> execute ecommerce_super.easyecom.flows.b2c_sales.pending_manifest_sweeper.sweep_pending_manifest_sis --kwargs '{"dry_run": true}'` |
| `B2CBuilderError: Sales Taxes Template ... not found` | Template naming mismatch (client-specific abbr) | Verify `Company.abbr` matches the template naming convention |
| `_check_variance` raises Discrepancy on every foreign-currency SI | `conversion_rate` too far from EE's rate | Check Currency Exchange DocType; verify FX confirmation flow (§5b) |
| Recon reports "SI missing" for an order that clearly exists in EE | Marketplace Order Map not populated (pre-PR #273 SI) | Run backfill patch (PR F, when available) |
| SI has zero `taxes[]` rows | Template not shipped on site (both `taxes_and_charges` and `item_tax_template` resolved to None) | Check site has the standard India Compliance templates |
| Foreign-currency SI submitted but INR base amounts wrong | FX confirmation was skipped or rate mis-entered | Cancel the SI, fix Currency Exchange, re-poll |

---

## 8. Related PRs (for archaeology)

- **#263** — normalize state names, derive gst_category, place_of_supply
- **#264** — Credit Note pair for Returned / Cancelled orders (backfill)
- **#266** — move EE address fields to child table (row-size fix)
- **#270** — SI posting_date = EE invoice_date (GST §31 compliance)
- **#272** — restore EasyEcom Marketplace Order Map + child DocType
- **#273** — populate MP Order Map in invoice_builder
- **#274** — pending-manifest sweeper (hourly cron)
- **#275** — fix B2C tax injection to IC-native pattern (CLAUDE.md #206 compliance)
- **#276** — multi-currency support + FX confirmation gate
- **#277** — this playbook (initial version)
- **#278** — backfill patch for pre-#273 SIs
- **#279** — post-audit hotfix bundle (Select-option fix, is_return filters, CN auto-confirm)

## 9. SPEC amendment status

The design decisions in PRs #272–#276 reverse SPEC_12 §5 (which had dropped the Marketplace Order Map in PR #107). The amendment proposal lives at:

`drafts/spec_sections/spec_12_amendment_restore_mp_order_map.draft.md`

Methodology reviews + promotes to `spec_sections/SPEC_12_patch_notes.md` on their own timeline. The code changes are live regardless — the draft explains the "why" for future readers.

---

*Last updated: 2026-08-17 (post-audit revision)*
