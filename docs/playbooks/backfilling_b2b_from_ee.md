# Backfilling B2B from EE — Ops Playbook

**Audience**: FDE onboarding a client mid-year, or backfilling a historical B2B window on a live client.
**Scope**: any client, any date range (July, June, whenever). Not Puresta-specific.

---

## What it does

For each B2B invoice EE returned in the target date window:

1. Ensures customer master is fresh (calls `customer_pull.pull_customers` for the marketplace_account's EE Account)
2. Resolves the customer by `buyer_gst` → ERPNext Customer
3. Resolves items via `EasyEcom Item Map` (existing SKU → Item mapping)
4. Creates a Sales Order (transaction_date = EE order_date, delivery_date = EE invoice_date)
5. Submits the SO **with the EE-push hook suppressed** (invoice already exists on EE — don't duplicate)
6. Creates an `EasyEcom B2B Order Map` linking SO ↔ ee_invoice_id
7. Calls `invoice_mirror._mirror_invoice(map, ee_row)` → creates the SI via ERPNext-native `make_sales_invoice`

Result: Sales Invoices for each B2B invoice, indistinguishable from live-flow SIs downstream (recon, GSTR-1, GL, everything).

## Invocation

```
POST /api/method/ecommerce_super.easyecom.flows.b2b_sales.backfill_from_ee.backfill_b2b_from_ee_api
Content-Type: application/x-www-form-urlencoded
Authorization: token <api_key>:<api_secret>

marketplace_account=ECS-MA-Puresta-B2B
&invoice_date_start=2026-07-01
&invoice_date_end=2026-07-31
&dry_run=true
```

Returns a summary dict:

```json
{
  "marketplace_account": "ECS-MA-Puresta-B2B",
  "invoice_date_start": "2026-07-01",
  "invoice_date_end": "2026-07-31",
  "dry_run": true,
  "customer_discovery": {"created": 3, "updated": 8, "skipped": 0, "errors": 0},
  "invoices_pulled": 275,
  "created_sos": 0,
  "created_sis": 275,
  "skipped_already_exists": 0,
  "skipped_no_customer": 0,
  "skipped_no_items": 0,
  "errors": 0,
  "error_details": []
}
```

## Recommended workflow

### 1. Dry-run first

Always. Confirms customer master resolution, item mapping, and per-row outcomes without touching anything.

```
&dry_run=true
```

Review the outcome counts. Common issues:
- `skipped_no_customer > 0` → some `buyer_gst` values aren't in Customer master. Either wait (customer_discovery runs first, might catch new ones next dry-run), or manually create the missing Customer records.
- `skipped_no_items > 0` → some SKUs aren't in `EasyEcom Item Map`. Run item pull first (§8d) or add mappings manually.

### 2. Live run

Once dry-run counts look right:

```
&dry_run=false
```

Wait for the response. For 275 invoices, expect ~2-5 minutes (SO insert + submit + SI mirror per invoice).

### 3. Verify

```sql
SELECT ecs_marketplace,
       COUNT(*) as si_count,
       ROUND(SUM(base_grand_total), 2) as total_inr
FROM `tabSales Invoice`
WHERE posting_date BETWEEN '2026-07-01' AND '2026-07-31'
  AND ecs_easyecom_invoice_id IS NOT NULL
GROUP BY ecs_marketplace;
```

Compare against the EE tax export CSV totals. Retail marketplaces (from the B2C flow) + B2B (from this backfill) together should match the EE tax export.

## Idempotency

Every invoice: checked against `SI.ecs_easyecom_invoice_id`. If present → `skipped_already_exists`. Safe to re-run the same window multiple times.

## Failure isolation

Per-invoice `try/except` with rollback on that invoice's savepoint. Errors don't stop the batch; they're logged with `ee_invoice_id` and returned in `error_details`.

## Resume after partial failure

If backfill hits a hard error (bench timeout, network drop mid-batch), resume via:

```
&resume_from_invoice_id=687108535
```

Skips everything up to and including that invoice_id, starts from the next. Combined with idempotency: even if `resume_from` is wrong, invoices before it that were already created will be skipped safely.

## Multi-currency (foreign B2B — CAD, SGD, NPR)

For invoices with `invoice_currency_code != INR`:
- SO is created with the native currency + `conversion_rate=1` (backfill-time default)
- Invoice_mirror inherits the SO's currency
- If Currency Exchange rows exist for the invoice_date, ERPNext computes correct `base_grand_total`
- If not, base amounts land wrong — same as fresh-poll behavior; add Currency Exchange rows for the affected dates first

Puresta July 2026: 4 foreign B2B invoices (CAD × 2, SGD × 1, NPR × 1). Add rows:
- CAD → INR @ 68.21 on 2026-07-31 (for BHR52627-489, BHR52627-494)
- SGD → INR @ 74.19 on the SGD invoice's date
- NPR → INR @ 0.63 on the NPR invoice's date

## When to skip customer discovery

Default: customer_pull runs first (a few seconds).

Skip when:
- You just ran customer_pull manually and know the master is fresh
- The client's EE tenant has no update to customer master (rare)

```
&skip_customer_discovery=true
```

## Recovery — bad SI created

If a backfilled SI turns out wrong (bad customer, wrong tax, etc.):

1. **Draft or Submitted**: use standard ERPNext Cancel + Delete via the desk
2. **Sale linked to Payment**: cancel Payment Entry first
3. **Re-run backfill**: with `resume_from_invoice_id` set to the invoice BEFORE the bad one, then let it re-process from the bad one onward. Idempotency will skip good SIs and re-create the bad one.

## Related PRs

- Uses the pattern established in PRs #272-#280 (Aug 2026 B2C rework)
- Depends on `invoice_mirror._mirror_invoice` (b2b_sales/invoice_mirror.py)
- Depends on `customer_pull.pull_customers` (customer_pull.py)
- Depends on `EasyEcom Item Map` (SKU → Item mapping)

## Not covered

- **Delivery Notes**: this backfill creates SO + SI only. If the client needs matching DNs, that's a separate pass (typically DNs are derived from stock movement / manifests, not the invoice level).
- **Payment Entries**: not created. When customer pays, ops books the Payment Entry manually against the SI.

---

*Last updated: 2026-08-17*
