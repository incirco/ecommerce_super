# SPEC_12 §5 amendment — RESTORE EasyEcom Marketplace Order Map

**Status**: PROPOSAL — awaiting methodology sign-off
**Author**: Claude Code (drafted from Aug 2026 Puresta reconciliation exercise)
**Reverses**: PR #107 (commit `1153070`) drop-MP-Order-Map decision
**Related code**: PR feat/restore-marketplace-order-map (parent app), pending recon-app read-target update

---

## What SPEC_12 §5 says today

> "**`EasyEcom Marketplace Order Map` DocType dropped; per-order data on SI + Sync Record**
> Map DocType deleted entirely. Settlement lifecycle moved to SI Custom
> Fields in a new "EasyEcom Settlement" collapsible section:
> `ecs_settlement_status`, `ecs_expected_settlement_date`,
> `ecs_settlement_completed_at`. EE payload audit moved to
> `EasyEcom Sync Record`. Zero new DocTypes for per-order recon overhead."

Rationale table given at time of drop:

| Reason kept the Map | Held up? |
|---|---|
| Lifecycle separation | ⚠️ Weak — `update_modified=False` works |
| Payload audit (SI bloat) | ✅ Legit — but Sync Record already handles this |
| Future-proof for split orders | ❌ Doesn't matter — 1:1 with SI since dedup on Invoice_id |

## What has changed since (2026-08 Puresta reconciliation)

The **split-order dismissal ("1:1 with SI since dedup on Invoice_id")** is now falsified by production evidence, and the **SI bloat argument has flipped direction**:

### 1. Split-shipment is real, not theoretical

Aug 2026 Puresta EE tax export vs `getAllOrders` reconciliation, one full month:

- **6,001** distinct marketplace order references
- **6,048** distinct EE `invoice_number` values
- **47** marketplace orders had 2+ EE invoices = 47 split-shipment orders / month
- Concentrated in B2B (which we skip in B2C polling) — but non-zero in retail (Shopify has partial-shipment orders too)

The dismissal assumed dedup on `invoice_id` would keep it 1:1. That's true — but it hides that ONE marketplace order maps to MULTIPLE `invoice_id`s. The recon engine joins at MARKETPLACE ORDER grain (RECON_SPEC §6.2), and needs a doc listing all shipment SIs for one marketplace order.

Without the Map, recon can't natively answer "give me all SIs for `SQ-440390821`" — it has to scan all SIs, which is expensive and race-prone.

### 2. SI row-size budget is exhausted; can't add SPEC_12's SI fields

The SPEC_12 refactor moved settlement fields to SI Custom Fields (`ecs_settlement_status`, `ecs_actual_net`, `ecs_variance_*`). Since then:

- **PR #266** moved 13 address fields **off** SI to `EasyEcom Address` child (MariaDB row overflow — 65,535-byte limit)
- Puresta prod SI row is still tight; adding new custom fields for recon writeback risks re-triggering the same overflow
- Recon SPEC §2 sanctions writing to `ecs_actual_net`, `ecs_variance_amount`, `ecs_variance_pct`, `ecs_settlement_status` on SI — these don't exist as shipped fields today because there's no room

The Map hosts these fields at order grain, off `tabSales Invoice` entirely. Same principle that motivated PR #266's child-table refactor.

### 3. RECON_SPEC §2 already reads from Marketplace Order Map

The recon app was built against the ORIGINAL SPEC that had Marketplace Order Map. RECON_SPEC §2 explicit contract: `Recon reads: Marketplace, Marketplace Account, Marketplace Order Map, ...`. RECON_SPEC §6.2: `join — via Order Map, at order grain... Order Map gives every shipment's SI — one order → many SIs`.

So the recon app was built assuming the Map exists. SPEC_12's drop was inconsistent with recon's requirements — this amendment reconciles the two.

## Amended §5 text

Replace SPEC_12 §5 in full with:

> **`EasyEcom Marketplace Order Map` DocType RESTORED (Aug 2026, reversing PR #107).**
>
> Restored per the following observations from production data:
>
> 1. Split-shipment is a real ~0.8%/month pattern (Puresta Aug 2026: 47 of 6,001 orders). Recon joins at order grain (RECON_SPEC §6.2); a Map giving `marketplace_order_id → [SI, SI, SI]` is the natural join target.
> 2. `tabSales Invoice` row-size budget is exhausted (see PR #266 for the address-fields precedent). Recon writeback fields (`actual_net`, `variance_amount`, `variance_pct`, `settlement_status`, `expected_settlement_date`, `settlement_completed_at`) cannot be added to SI as originally planned. The Map hosts them at order grain instead — zero new columns on `tabSales Invoice`.
> 3. Per-shipment EE metadata (invoice_id, invoice_number, batch_id, manifest_no, invoice_currency_code, conversion_rate, sales_channel, payment gateway fields, IRN cross-ref, e-way bill cross-ref) lives on a new `EasyEcom Order Map Shipment` child table on the Map. Multi-shipment orders naturally carry multiple child rows. Zero new columns on `tabSales Invoice`.
>
> **Uniqueness**: composite key `(marketplace, marketplace_order_id, company)`. Enforced in controller `validate()` since Frappe's `unique=1` is single-column only.
>
> **Interface contract with recon app**: unchanged — RECON_SPEC §2 remains authoritative. Ecommerce_super writes everything except the recon-writeback fields; recon writes `settlement_status`, `actual_net`, `variance_amount`, `variance_pct`, `settlement_completed_at`. Dependency stays one-way.
>
> **Sync Record**: retained for per-sync audit (as SPEC_12 originally established). Sync Record is entity-centric (one per doc-per-direction); Map is order-centric (one per marketplace order regardless of shipment count). Both coexist without overlap.
>
> Settlement/net-receivables surface (§12.11): "lists open **Marketplace Order Maps** grouped by marketplace and expected_settlement_date." Not by SI — a split-shipment order should show as one row, not N rows.

## Fields on the restored Map (parent doc)

```
naming_series               MOM-.YYYY.-.#####
marketplace_order_id        Data, R, search_index
marketplace                 Link Marketplace, R
marketplace_account         Link EasyEcom Marketplace Account, R
company                     Link Company, R
easyecom_order_id           Data (EE internal numeric)
settlement_status           Select: Forecast/Partial/Settled/Disputed (recon writes)
expected_settlement_date    Date
settlement_completed_at     Datetime (recon writes)
actual_net                  Currency (recon writes)
variance_amount             Currency (recon writes)
variance_pct                Percent (recon writes)
total_shipments             Int (auto-computed from len(shipments))
shipments                   Table → EasyEcom Order Map Shipment
```

## Fields on the child (`EasyEcom Order Map Shipment`)

```
sales_invoice                    Link Sales Invoice, R    (the shipment's SI)
ecs_easyecom_event_type          Select: Sold / Sold(cancelled) / Returned / RTO
original_sales_invoice           Link Sales Invoice       (for CN/SR back-ref)

# EE identifiers
invoice_id                       Data                     (EE numeric)
invoice_number                   Data, search_index       (EE alphanumeric, appears on PDF)
easyecom_invoice_pdf_url         Small Text

# Manifest / dispatch
manifest_date                    Datetime, search_index
manifest_no                      Data
batch_id                         Data
batch_created_at                 Datetime
sales_channel                    Data
awb_number                       Data, search_index
courier                          Data

# Currency (native, with conversion rate to INR)
invoice_currency_code            Data, default INR
conversion_rate                  Float, default 1
total_amount_native              Float
total_tax_native                 Float

# Payment
payment_gateway_name             Data
payment_gateway_transaction_number  Data

# GST compliance cross-references (from EE side; cross-check vs India Compliance)
irn                              Data
ack_no                           Data
ack_date                         Datetime
eway_bill_number                 Data
eway_bill_date                   Datetime
```

## What SPEC_12 sections need to be updated

- **§12.8 lines 2810-2817**: restore the Marketplace Order Map description with the new field set (above)
- **§12.11 line 2835**: recon join is via Map (`Settlement Line.marketplace_order_id → Marketplace Order Map → shipments[].sales_invoice`), not directly against SI
- **§12.11 line 2837**: "Net Receivables view lists open Marketplace Order Map records" (restore original text)
- **§12.5 (SI custom fields section)**: remove `ecs_settlement_status`, `ecs_expected_settlement_date`, `ecs_settlement_completed_at` (they now live on the Map, not SI). Retain `ecs_marketplace_order_id`, `ecs_marketplace`, `ecs_marketplace_account` on SI as the SI → Map join key.
- **SPEC_12_patch_notes.md §5**: append a "SUPERSEDED by Aug 2026 amendment" note pointing to this file

## Impact on already-shipped code

- `add_b2c_sales_invoice_fields` patch (which added `ecs_settlement_status` on SI) — leave the field in place for backward compatibility, but recon writeback moves to Map. Optional follow-up patch can hide the field.
- `b2c_sales/invoice_builder.py` line 501 comment ("Sync Record — audit trail (replaces Marketplace Order Map)") — update to reflect that both Sync Record and Map exist with distinct roles.
- `hooks.py` line 625 (Marketplace Order Map reference) — likely intact from before, verify.

## Recommendation

Approve the amendment. Ship the code PR (`feat/restore-marketplace-order-map`) after approval. Follow up with:
- PR B: populate the Map in b2c invoice_builder + credit_note builder
- PR C: draft-first + terminal-state polling (independent, but benefits from Map presence)
- PR D: multi-currency handling (writes to Map Shipment child)
- PR F: backfill patch for existing SIs

## Sign-off

- [ ] Methodology lead: approve amendment text
- [ ] Recon-team lead: confirm no recon-side changes needed (RECON_SPEC §2 already matches this shape)
- [ ] Engineering lead: approve the paired code PR

Once all three, move this file into `spec_sections/SPEC_12_patch_notes.md` as an amendment appendix and merge the code PR.
