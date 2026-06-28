# FDE Primer — §12 B2C / Marketplace Sales

For Forward-Deployed Engineers operating §12 in production. Companion
to `SPEC.md §12` and the §11 primer family. Phase 1 of §12 ships
the polling → SI creation flow; webhooks are deferred to Phase 2.

---

## Part A — What §12 does, in one paragraph

A B2C marketplace order (Amazon, Flipkart, Myntra, etc.) is born on
the marketplace, lands in EasyEcom as an EE order, and is dispatched
from a warehouse EE manages. **ERPNext never sees a Sales Order** — by
design, per `SPEC.md §12.2`. When EE manifests the order (the
operationally-meaningful "ready to dispatch" moment), the §12
polling cron pulls it and creates a **Sales Invoice directly** in
ERPNext. The SI carries the marketplace identifiers + EE-supplied
financial values + a Marketplace Order Map row that bridges to future
Settlement Lines for recon.

## Part B — The data spine

- **`EasyEcom Marketplace Account`** (new §12) — per-(Company, Marketplace)
  seller configuration. Carries the polling cursor (`last_pull_orders`),
  the pseudo-customer link, and routing (`easyecom_account`).
- **`EasyEcom Marketplace Order Map`** (new §12) — one row per
  EE shipment (Invoice ID). The recon engine's primary join target
  for future Settlement Lines.
- **Sales Invoice with `ecs_marketplace*` Custom Fields** — the actual
  financial record. EE values stored separately for variance / recon.
- **Per-(marketplace × Company) Customer** — bootstrapped automatically
  when an FDE creates the Marketplace Account. Every B2C SI for that
  marketplace points at this pooled Customer. The actual buyer's
  address goes on the SI's Shipping Address, not the Customer record.

## Part C — Setup (per Company, per marketplace, ~10 min each)

```
On the ERPNext side:
  1. Verify §8b Marketplace rows exist for the marketplaces this
     client sells on (e.g., "Amazon.in", "Flipkart"). Pull from EE
     if missing.
  2. Verify the EE Account has a default_location_key set (this
     drives the polling JWT routing).
  3. Open Desk → EasyEcom Marketplace Account → New
       - Marketplace: <pick one>
       - Company: <client's Company>
       - Seller ID: <marketplace's seller_id>
       - EasyEcom Account: <pick the EE Account>
       - Save
  4. Verify: a Customer named "<Marketplace> B2C Pool - <Company>"
     was auto-created (after_insert hook). The Marketplace Account's
     "B2C Pool Customer" field is auto-populated.
  5. Repeat per marketplace this Company sells on.
```

That's it. The polling cron picks the Account up on the next */5
tick. First poll initialises `last_pull_orders` to NOW — forward-only
cutover. Historical orders are §102 backfill territory.

## Part D — Operator-visible surfaces

| Surface | Where | When you use it |
|---|---|---|
| **EasyEcom Marketplace Account list** | `/app/easyecom-marketplace-account` | Configure new marketplaces; flip `enabled` to pause polling for one |
| **EasyEcom Marketplace Order Map list** | `/app/easyecom-marketplace-order-map` | See every B2C SI's recon-bridge row + settlement status |
| **Sales Invoice list filtered by `ecs_marketplace`** | `/app/sales-invoice?ecs_marketplace=2` | Channel-specific SI views |
| **EasyEcom Integration Discrepancy list** | Standard Discrepancy list with `kind = "B2C tax variance — EE vs ERPNext > 1%"` | Variance alerts for upstream investigation |

## Part E — Path 2 — the tax model (locked 2026-06-29)

**Why this matters.** The marketplace is the system that generated
the invoice; it computed the tax the buyer paid; settlement will be
against that tax. So we treat EE's tax as the source of truth for the
SI's GL impact. ERPNext computes its own tax separately, purely as a
**variance check** to surface upstream issues.

| Field | Source | Purpose |
|---|---|---|
| `SI.taxes` | EE-supplied (single 'Actual' row) | GL impact, NIC IRP (if any), settlement reconciliation |
| `ecs_ee_invoice_total` | EE order header | Recon source-of-truth |
| `ecs_ee_invoice_tax_total` | EE order header | Recon source-of-truth |
| `ecs_erpnext_tax_check_total` | Computed: sum(qty × rate × HSN default GST rate) | Variance signal — never used in GL |

**Variance check:** if `|ee_tax_total - erpnext_tax_check| / ee_tax_total > 1%`,
raises an Integration Discrepancy. The Discrepancy is **informational** —
SI data is never amended. FDE investigates upstream:
  - HSN code on Item is wrong
  - GST HSN Code default rate is misconfigured
  - Marketplace adapter on EE side has a tax-mapping bug
  - Composition / reverse-charge edge case

**No alert raised when** `ecs_erpnext_tax_check_total = 0` — happens
when HSN codes aren't resolved on Items. Better to skip the alert
than flood the FDE with false positives on fresh installs.

## Part F — Failure modes

| Symptom | What it means | Recovery |
|---|---|---|
| Marketplace Account never polls | `enabled = 0` OR `easyecom_account` is null OR cadence not elapsed | Set those fields; wait one tick |
| All orders skip with "Stage 3 builder not yet implemented" | (Now resolved — Stage 3 shipped) | n/a |
| Per-record failure: "EE SKU(s) ... have no EasyEcom Item Map" | Item not synced yet | Run §8d Item Push for the SKU |
| Per-record failure: "Marketplace Account has no pseudo_customer" | Bootstrap hook failed at insert | Resave the Marketplace Account |
| Per-record failure: "Company has no default tax account" | CoA missing 'Output Tax' / 'Sales Taxes' | Configure in CoA |
| Discrepancy: "B2C tax variance > 1%" | EE vs ERPNext tax mismatch (upstream issue) | See Part E |
| Cursor stuck (polling fails repeatedly) | EE auth / connectivity / 5xx | `last_pull_error` carries the message; FDE fixes upstream |

## Part G — What's NOT in this build (by design)

- **No webhooks.** Polling-only (matches §11 Phase 1 deferral pattern).
  EE webhook receivers across all flows are Phase 2+.
- **No backfill.** Forward-only cutover; historical orders are §102
  territory (a separate flow scheduled after §12).
- **No e-invoice IRN minting.** B2C orders rarely need IRN, and when
  they do, the marketplace or EE handles it (per user direction
  2026-06-29). If EE sends an IRN in the order payload, we mirror it
  as a field; we never call NIC IRP from §12.
- **No DN auto-creation.** SI carries `update_stock = True`, so stock
  leaves at SI submit via the standard ERPNext pathway.
- **No Sales Order ever.** This is the §12 architectural axiom (spec
  line 2742). Don't add SOs to "complete the trail" — there isn't one.
- **No per-marketplace tax-source toggle.** Path 2 across the board
  (not Path 3). If a specific marketplace's tax computation is so
  unreliable it pollutes the GL, we'll add the toggle then.

## Part H — Related primers

| Primer | Use when |
|---|---|
| `FDE_PRIMER_section_11_b2b_sales.md` | §11 baseline — different flow (SO → push out), shared substrate (polling cron, EE client) |
| `FDE_PRIMER_section_11_5_1_custom_gsp.md` | Mode 1 Custom GSP — relevant only for B2B; B2C never uses it |
| `FDE_PRIMER_section_11_6_dispatch_status.md` | §11.6 dispatch status — currently only stamps B2B SIs; extending to B2C SIs is a small follow-up |

## Origin

- §12 build: 2026-06-29, PR #107 (this PR — once merged)
- Path 2 decision: 2026-06-29 design call with rishinikhil. SI carries
  EE-supplied tax (marketplace = source of truth); ERPNext-computed
  tax becomes a variance check signal. No per-marketplace election.
- Pseudo-customer bootstrap: auto-trigger on Marketplace Account
  insert (vs. FDE manual or wizard) — locked 2026-06-29.
- Forward-only cutover: locked 2026-06-29. §102 backfill is a
  separate flow scheduled after §12.
- Spec deltas captured: SPEC_12_patch_notes (created with this PR).
