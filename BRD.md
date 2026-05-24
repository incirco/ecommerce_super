# Business Requirements Document (BRD)

**Version:** 1.0 — Working Document
**Date:** May 2026
**Companion documents:** PRD.md, SPEC.md, CLAUDE.md
**Audience:** Methodology team, FDE leads, Practising CAs, Finance leadership
**Owner:** Methodology team (CA + marketplace operations specialist + senior Frappe consultant)

---

## 0. What this document is

The Product Requirements Document says **what** we're building. The Specification says **how** we build it. This document — the BRD — says **what makes the output correct for the business.**

It is the methodology team's authoritative source of truth for:

- The standard chart of accounts every client deploys with
- The default Account Role Map (which marketplace event posts to which GL account)
- GST disposition rules for every event type
- Recon thresholds, tolerances, and what counts as a Discrepancy
- The eight Discrepancy types and their resolution logic
- Settlement Forecast computation rules
- Rate Card Library structure and refresh discipline
- The month-end close playbook and its sequencing
- Pricing diagnostic thresholds (for the methodology's pricing intelligence module)
- The methodology lifecycle — how a rule moves from proposed to default to deprecated

When this BRD and SPEC.md disagree on a specific value (e.g., "what is the default tolerance for tax variance on a GRN"), this BRD wins. SPEC.md may specify the *mechanism* by which the rule is implemented (a Settings field, a fixture row); this BRD specifies the *value* and the *reasoning*.

When this BRD and PRD.md disagree, the PRD wins on scope and positioning, this BRD wins on operational correctness.

---

## 1. The Standard Chart of Accounts

The chart of accounts shipped with the parent app as a fixture is **opinionated**. Every account exists for a methodology reason; FDEs do not freely add to or rename accounts during onboarding without methodology team review.

### 1.1 The principle

Marketplace business cannot be reconciled cleanly against a generic chart of accounts. The standard P&L of "Revenue / Cost of Goods / Operating Expenses" cannot answer the questions a multi-marketplace seller's CFO asks: *what's my net of fees from Amazon this month, what's my recoverable tax exposure on returned-not-received goods, what's the pre-loss vs. post-loss revenue split, what's locked up in receivables*?

The standard chart of accounts is structured to let those questions be answered automatically, by querying GL on the correct account roles.

### 1.2 The standard chart of accounts (top-level groups)

The accounts below ship as a fixture and form the spine of the methodology. Sub-accounts can be added per client, but the listed accounts must exist.

#### 1.2.1 Income (Revenue)

| Account | Type | Purpose |
| --- | --- | --- |
| Marketplace Revenue (Gross) | Income | Revenue at order list price, before any marketplace fees, returns, or settlement deductions |
| Marketplace Revenue (Net) | Income | Computed view — Marketplace Revenue (Gross) less Returns Reversed |
| Returns Reversed | Income (contra) | Sales reversed when a return is confirmed and goods are received back |
| Direct B2B Revenue | Income | Sales to B2B customers not via a marketplace |
| Direct D2C Revenue | Income | Sales via the seller's own website / direct channel |

#### 1.2.2 Marketplace Fees and Deductions (Expense, sub-grouped per marketplace)

These are the structural backbone for fee recon. Every fee category that any marketplace charges must map to one of these. The Account Role Map (Section 2) handles the marketplace-specific mapping.

| Account | Purpose |
| --- | --- |
| Marketplace Commission | Percentage commission charged by the marketplace on the order amount |
| Marketplace Closing Fees | Fixed-amount fees per order |
| Marketplace Shipping Fees | Forward shipping charges paid to marketplace |
| Marketplace Reverse Shipping Fees | Reverse logistics fees on returns |
| Marketplace Storage Fees | FBA / FBF / FBM storage charges |
| Marketplace Pick Pack Fees | Order processing / pick-pack-ship fees |
| Marketplace Weight Discrepancy Charges | Charges for weight mismatches between declared and measured |
| Marketplace Penalty Fees | Cancellation, late-shipment, quality-related penalties |
| Marketplace Promotion Fees | Sponsored ads, deals, lightning deals (if not booked separately) |
| Marketplace Tax Collection Fee | Marketplace's fee for collecting tax on the seller's behalf |
| Marketplace Other Charges | Bucket for unmapped fees — should always be small; sustained growth here is a methodology failure |

#### 1.2.3 Recoverable Tax (Asset)

ITC (Input Tax Credit) is a real asset the seller can reclaim against output GST. We track it precisely.

| Account | Purpose |
| --- | --- |
| Input GST — Purchases | ITC on inventory purchases |
| Input GST — Marketplace Fees | ITC on marketplace fees, claimable against output GST |
| Input GST — Logistics | ITC on third-party logistics |
| Input GST — Other | ITC on other operating expenses |
| Input GST Reconciled to GSTR-2B | Sub-account; ITC actually appearing on government's GSTR-2B (a recon-result account) |
| Input GST Disputed | Sub-account; ITC we believe we're owed but isn't in GSTR-2B |

#### 1.2.4 Tax Liabilities

| Account | Purpose |
| --- | --- |
| Output CGST / SGST / IGST | Standard ERPNext output GST accounts (per HSN + state) |
| TCS Deducted by Marketplace | TCS the marketplace withheld and remitted (claimable against output) |
| TDS Deducted by Marketplace | TDS the marketplace withheld and remitted (claimable against income tax) |
| GST Payable | Net of output less ITC, payable at filing |

#### 1.2.5 Receivables and Settlement

| Account | Purpose |
| --- | --- |
| Marketplace Receivables (Gross) | Forecast amount due from marketplace at order time, before fees |
| Marketplace Receivables (Net Forecast) | Forecast amount after expected fees, taxes, and tolerances |
| Marketplace Receivables (Realised) | Actual amount settled, posted as Bank Transaction matched |
| Receivables Variance | Forecast vs Realised gap; the recon engine's daily diagnostic |
| Receivables Aged > 30 days | Aging bucket (auto-computed) |
| Receivables Aged > 60 days | Aging bucket — typically indicates a settlement gap worth claiming |
| Receivables Aged > 90 days | Aging bucket — likely lost-in-transit or unclaimed |

#### 1.2.6 Inventory and COGS

Standard ERPNext inventory and COGS accounts plus:

| Account | Purpose |
| --- | --- |
| Returns-In-Transit | Inventory in transit back from customer; valued but not yet received |
| Returns Damaged | Inventory received back but unsellable |
| Returns Lost in Transit | Inventory marked returned by marketplace but never received |
| Inventory Adjustments — Marketplace | Catch-all for marketplace-side stock adjustments outside a normal flow |

#### 1.2.7 Pre-Loss Revenue Reporting (Methodology view)

These are not GL accounts but standard reports the methodology produces:

- **Pre-Loss Revenue**: Marketplace Revenue (Gross), the headline number sellers traditionally report
- **Post-Loss Revenue**: Pre-Loss Revenue less all Marketplace Fees and Deductions, less Returns Reversed, less unrecovered Receivables Variance
- **Realised Margin %**: Post-Loss Revenue divided by Cost of Goods Sold

Sellers who track only Pre-Loss Revenue have no idea what their actual margin is. Forcing the methodology's reports to surface both metrics side by side is a deliberate behavioral choice.

### 1.3 Why this opinionated structure matters

A reseller's accountant might recommend three or four accounts for "marketplace fees." The methodology insists on eleven. The reason: the recon engine's Fee-to-Expense reconciliation, and the pricing diagnostics, can only function if the GL is granular enough to attribute each line on a settlement file to a specific account role. A flat "Marketplace Fees" account makes recon impossible.

If a client pushes back on the structure during onboarding, the FDE explains the recon dependency. **The structure is a hard requirement, not a default the client can override.** Clients who insist on flatter charts of accounts are out of scope for this product.

---

## 2. The Account Role Map

The Account Role Map is the bridge between marketplace-event-types (what arrives in a settlement file) and the standard chart of accounts (where it posts in GL). Every reconciled event runs through the Account Role Map to produce the right Journal Entry / Purchase Invoice posting.

### 2.1 Schema

Conceptually, the Account Role Map is a table of:

```
(Marketplace, Event Type, Account, Side [Dr/Cr], GST Treatment, Notes)
```

Implementation in the parent app: see `SPEC.md` Section 30.2 (Marketplace Account, Tax Mapping, custom fields on Item/Customer). The values shipped as fixtures.

### 2.2 The standard map (excerpts — the methodology's defaults)

Full mapping ships in `apps/ecommerce_super/ecommerce_super/fixtures/account_role_map.json`. The excerpts below show the methodology's reasoning for the most common events.

#### 2.2.1 Order settlement events (across marketplaces)

| Marketplace Event | Account | Side | GST Treatment |
| --- | --- | --- | --- |
| Order amount (gross) | Marketplace Receivables (Realised) | Dr | — |
| Commission | Marketplace Commission | Dr (expense) | ITC claimable |
| Closing fee | Marketplace Closing Fees | Dr | ITC claimable |
| Shipping fee (forward) | Marketplace Shipping Fees | Dr | ITC claimable |
| Pick-pack-ship fee | Marketplace Pick Pack Fees | Dr | ITC claimable |
| Storage fee | Marketplace Storage Fees | Dr | ITC claimable |
| TCS deducted | TCS Deducted by Marketplace | Dr | Asset (recoverable) |
| TDS deducted | TDS Deducted by Marketplace | Dr | Asset (recoverable) |
| Net amount disbursed | Bank | Dr (when settled) | — |

The corresponding Cr leg of each Dr is Marketplace Revenue (Gross) for the order amount, and the relevant GL liability or expense for fees.

#### 2.2.2 Return settlement events

| Marketplace Event | Account | Side | Notes |
| --- | --- | --- | --- |
| Return refund (customer-side) | Returns Reversed | Cr (revenue contra) | Reverses original sale |
| Reverse shipping fee | Marketplace Reverse Shipping Fees | Dr | ITC claimable |
| Goods received (sellable) | Inventory | Dr | Stock returned |
| Goods received (damaged) | Returns Damaged | Dr | Stock written down |
| Goods marked returned but not received | Returns Lost in Transit | Dr | The Return-Marked-Goods-Not-Received pattern |

#### 2.2.3 Penalty and weight events

| Marketplace Event | Account | Side | Notes |
| --- | --- | --- | --- |
| Weight discrepancy | Marketplace Weight Discrepancy Charges | Dr | ITC sometimes claimable, sometimes not — see Section 3 |
| Cancellation penalty | Marketplace Penalty Fees | Dr | ITC typically NOT claimable |
| Late shipment penalty | Marketplace Penalty Fees | Dr | ITC typically NOT claimable |
| Quality failure penalty | Marketplace Penalty Fees | Dr | ITC NOT claimable |

### 2.3 Per-marketplace overrides

Marketplaces use different terminology for the same event. The Account Role Map fixtures include marketplace-specific patterns:

- **Amazon**: "MFN Commission Rate", "FBA Pick & Pack Fee", "FBA Storage Fee", "Refund Commission"
- **Flipkart**: "Marketplace Fee", "Collection Fee", "Pick & Pack Fee", "Storage Fee"
- **Meesho**: "Commission", "Logistic Fee", "Reverse Logistic Fee", "Penalty"
- **Myntra**: "Brand Commission", "Logistics", "Forward Shipping", "Reverse Shipping"

Each marketplace's fixture provides regex patterns or exact-match terms that resolve to the standard account from Section 1. **Adding a new marketplace is not a code change — it's a fixture addition by the methodology team.** SPEC.md Sections 4.7 (Channel master) and 30.2.20 (Marketplace Account) cover the implementation.

### 2.4 Per-client overrides

Some clients have negotiated rate exceptions or special arrangements (e.g., reduced commission on a specific category, custom storage fee for FBA strategic accounts). These are captured in the client app's `account_role_map_overrides.json` fixture, and apply only to that client's site.

The methodology team reviews proposed per-client overrides quarterly and absorbs broadly-applicable ones into the standard map.

### 2.5 The "Marketplace Other Charges" guardrail

If the recon engine encounters an event type that doesn't match any rule in the Account Role Map, it posts to "Marketplace Other Charges" and raises a Discrepancy of type Unclassified Charge. This is a methodology guardrail, not a feature: the goal is to keep the Other Charges balance minimal. **A client whose Marketplace Other Charges balance is consistently growing month over month indicates the methodology team is not keeping the Account Role Map current.** The methodology team's KPI is "Marketplace Other Charges as % of total marketplace fee balance"; target < 0.5% across the FDE fleet.

---

## 3. GST Disposition Rules

For every marketplace event, the methodology specifies the correct GST treatment. This is the most consequential piece of the BRD because GST treatment errors compound monthly and produce material tax liability.

### 3.1 The principle

Indian GST has three rate dimensions: CGST + SGST (intra-state) or IGST (inter-state), applied at the rate matched to the HSN code of the underlying good/service. Marketplace fees and reverse-charge mechanisms layer on top.

The methodology produces clear rules for:

- Whether ITC is claimable on a given fee
- The applicable rate
- Whether the marketplace is the supplier of record (in which case the fee goes through reverse charge for the seller's purposes)
- How to treat fees on intra-state vs inter-state transactions

### 3.2 The standard ITC claimability matrix

| Fee Category | ITC Claimable? | Reasoning |
| --- | --- | --- |
| Marketplace Commission | Yes | Service supplied by marketplace, with GST charged. Claim against output GST. |
| Closing Fees | Yes | Same reasoning as commission |
| Shipping Fees (forward) | Yes | Logistics service with GST |
| Shipping Fees (reverse) | Yes (with caveats) | Some marketplaces include this in commission; if separately billed with GST invoice, claim |
| Pick-Pack Fees | Yes | Service with GST |
| Storage Fees | Yes | Service with GST |
| Promotion Fees | Yes | Advertising service with GST |
| Weight Discrepancy Charges | **Conditional** | Only claimable if marketplace issues GST-compliant invoice. Many marketplaces issue debit notes that are technically not ITC-eligible. The default is "not claimable" — FDE confirms with the marketplace's invoice format during onboarding |
| Cancellation Penalties | **No** | Penalty income to the marketplace, no service provided to seller |
| Late Shipment Penalties | **No** | Same reasoning |
| Quality Penalties | **No** | Same reasoning |
| Refund Commission Reversals | **No** | These are reversals of previously-claimed ITC, not new ITC |

### 3.3 Reverse charge mechanism (RCM)

For specific service categories provided by certain marketplaces (notably some logistics services), GST is paid under reverse charge — the recipient (seller) is liable to pay the GST and then claim it as ITC. The methodology defaults assume marketplaces operate under forward charge unless explicitly configured otherwise.

The RCM toggle is a per-marketplace per-fee-type setting in the Account Role Map. **FDEs do not change this without methodology team review.** Incorrect RCM treatment produces material output tax liability.

### 3.4 Place of supply rules

The methodology applies these rules consistently:

- **Order place of supply**: the buyer's state. Determines IGST vs CGST+SGST split for output GST.
- **Marketplace fee place of supply**: the seller's registered place of business (the GSTIN's state). Determines IGST vs CGST+SGST for ITC purposes.
- **Inter-state transfers between own warehouses**: zero-rated (per ERPNext's standard inter-state stock transfer mechanic).

Mismatches between the order's place of supply and the marketplace's claimed place of supply are recon Discrepancies of type Place of Supply Mismatch.

### 3.5 GSTR-2B reconciliation

Every month, the methodology requires the recon engine's Fee-to-Expense reconciliation to compare claimed ITC against the GSTR-2B downloaded from the GST portal. Mismatches are Discrepancies of type ITC Not in GSTR-2B (potential lost ITC) or ITC in GSTR-2B but Not Claimed (action required to claim).

This is the single most material BRD-driven recon for SME sellers — sustained ITC leakage is often 0.3-0.8% of GMV and can be substantially recovered.

### 3.6 GST treatment of Returns

Return treatment is asymmetric:

- A **return with goods received** reverses the original output GST and the original revenue. The seller's net liability is reduced.
- A **return marked but goods not received** must NOT reverse output GST until goods are confirmed back. Otherwise the seller has reversed the output GST without the corresponding stock reversal — overstated ITC, under-stated tax. Discrepancy type: Return-Marked-Goods-Not-Received.

The methodology's default is to NOT auto-reverse output GST on return-marked events. Only after Goods Receipt confirmation does the reversal post. SPEC.md Section 9.8 covers the implementation.

---

## 4. Recon Thresholds and Tolerances

A reconciliation does not produce a binary "matched" or "unmatched"; it produces a variance against a forecast. The variance becomes a Discrepancy if it exceeds the configured tolerance. The methodology's tolerances ship as defaults; clients tune within bounds.

### 4.1 The tolerance philosophy

Two opposing failure modes:

- **Tolerance too tight**: every minor rounding produces a Discrepancy; the FDE drowns in noise; real issues are missed.
- **Tolerance too loose**: real leakage is absorbed silently; recon claims to work but doesn't actually catch problems.

The methodology calibrates tolerances per recon, per marketplace, per fee type, based on observed real-world variance distributions across the FDE fleet. Tolerances are reviewed quarterly.

### 4.2 The default tolerance table

| Recon | Tolerance | Per-line or batch? | Reasoning |
| --- | --- | --- | --- |
| Order-to-Settlement (amount) | ₹2 or 0.1% (whichever is greater) | Per-line | Floats and rounding tolerance |
| Order-to-Settlement (commission) | ₹1 or 0.05% | Per-line | Tighter — commission rates are deterministic per category |
| Fee-to-Expense (Purchase Invoice posting) | ₹0.50 | Per-line | GL-level precision |
| TCS-to-Government | ₹1 | Per-line | TCS rates are exact |
| Return-to-Credit-Note (amount) | ₹2 | Per-line | Same as Order |
| GSTR-2B match | ₹2 | Per-invoice | GSTR-2B-to-system-of-record matching |
| Inventory variance | 1 unit | Per-SKU | Anything more than 1 unit drift is a Discrepancy |

These are absolute defaults shipped via fixtures. Clients can request tighter tolerances; loosening below default requires methodology team approval.

### 4.3 The aging window

A discrepancy aged > 30 days is automatically escalated in the alert routing. Aged > 60 days, the FDE is required to either resolve or formally write off. Aged > 90 days, methodology team reviews — sustained 90+ day discrepancies indicate a systemic methodology gap.

---

## 5. The Eight Discrepancy Types

The recon engine produces Discrepancies of one of eight types. Each has a clear definition, a default severity, a default disposition rule, and a methodology-level expectation for how often it should occur.

### 5.1 The taxonomy

| Type | Definition | Default Severity | Expected Frequency |
| --- | --- | --- | --- |
| Order Without Settlement | Order shipped/delivered but not appearing on any settlement file beyond the expected window | Critical | <0.5% of orders |
| Settlement Without Order | Settlement line referencing an order not in our system | Error | <0.1% of orders |
| Amount Mismatch | Settlement amount differs from forecast beyond tolerance | Warning | <2% of orders for 99-percentile, <5% peak |
| Commission Mismatch | Commission charged differs from rate-card-derived expected | Warning | <1% — sustained higher indicates rate card stale |
| Place of Supply Mismatch | Buyer's state per order differs from marketplace's claimed | Error | <0.5% |
| ITC Not in GSTR-2B | Claimed ITC not appearing on government's GSTR-2B | Critical | <2% (this is the recoverable opportunity) |
| Return-Marked-Goods-Not-Received | Marketplace marked return but goods never received within window | Critical (financial impact accrues) | <0.5% |
| Unclassified Charge | Settlement event not matched to any Account Role Map rule | Error (becomes Critical at >₹10k) | <0.5% — sustained indicates Account Role Map stale |

### 5.2 Severity overrides via Recon-Aware Alerts

Per `SPEC.md` Section 25 (Recon-Aware Integration Alerts), every Discrepancy carries a financial impact estimate. Severity may be elevated by impact:

- Default severity from this table
- Medium impact (₹10k-100k): elevate one severity level
- High impact (>₹100k): always Critical regardless of base severity
- Very high impact (>₹1L): Critical + finance leadership escalation

This is the methodology's expression of "the financial loss matters more than the type."

### 5.3 Disposition rules per type

Each Discrepancy type has a default disposition flow shipped as a fixture:

- **Order Without Settlement**: open claim with marketplace within 7 days; write off after 90 days if unresolved
- **Settlement Without Order**: investigate for marketplace error; usually a credit note from marketplace; may require Order creation in our system
- **Amount Mismatch**: if pattern (>10 in a month for the same fee type), open methodology review of rate card; otherwise individual claim
- **Commission Mismatch**: open rate card review; potentially recover via claim
- **Place of Supply Mismatch**: contact marketplace to correct; tax implications must be resolved before next GST filing
- **ITC Not in GSTR-2B**: contact supplier to ensure they file; claim ITC even if delayed (within statutory limits)
- **Return-Marked-Goods-Not-Received**: open claim with marketplace within 30 days
- **Unclassified Charge**: methodology team review immediate; new Account Role Map rule or per-client override

---

## 6. Settlement Forecast Computation Rules

Every Sales Invoice on submit produces a Settlement Forecast — the methodology's expected net amount the seller will receive on the settlement file. The forecast is the baseline against which actual settlement is reconciled.

### 6.1 The principle

A seller cannot meaningfully reconcile settlement files without knowing what the settlement *should* have been. Without a forecast, "₹4,73,841 received" has no anchor; the seller sees the number and assumes it's correct.

The methodology requires every order to carry a forecast at order time, computed deterministically from versioned rate cards. The forecast lives on a Settlement Forecast DocType linked to the Sales Invoice.

### 6.2 Computation formula (conceptual)

```
forecast_net = order_amount
             - commission (rate × amount per category)
             - shipping_fee (per rate card)
             - pick_pack_fee (per rate card)
             - storage_fee (allocated per order)
             - closing_fee (per rate card)
             - TCS (per rate card; rate × amount)
             - TDS (per rate card; rate × amount)
             + applicable_GST (per output GST rate)
             - reverse_GST_on_fees (per fee category × applicable rate)
             - estimated_return_loss (per category × historical return rate)
             - tolerance_buffer (per Section 4.2)
```

The reasoning behind each line is documented in `apps/ecommerce_super/ecommerce_super/methodology/forecaster_v0.py` (the methodology team owns this file's correctness).

### 6.3 Rate Card versioning

Rate cards are versioned monthly. Each forecast is computed against the rate card version effective at order time. When a marketplace changes rates mid-month, the methodology team produces a new rate card version with a clear effective_from date. Forecasts already in flight are not recomputed; the variance from the rate change becomes legitimate Amount Mismatch Discrepancies that resolve via marketplace claim.

### 6.4 Forecast variance interpretation

| Variance | Methodology interpretation |
| --- | --- |
| < tolerance | Reconciled clean. No action |
| 1×-2× tolerance, isolated | Likely floats / rounding. Auto-resolve |
| > 2× tolerance, isolated | Discrepancy of appropriate type. Investigate |
| Pattern (>10 in month for same fee) | Rate card review triggered |
| Cumulative > 1% of GMV | Methodology audit triggered |

---

## 7. Rate Card Library Structure

The Rate Card Library is the source of truth for "what the marketplace charges." It's centrally maintained by the methodology team and pushed to each client's site as a fixture refresh.

### 7.1 The schema

A Rate Card is a versioned set of:

- **Marketplace**, **Channel**, **Category** (item-level granularity)
- **Effective from**, **Effective to**
- **Commission rate** (% or fixed)
- **Closing fee**, **Pick-pack fee**, **Shipping forward**, **Shipping reverse**, **Storage**
- **TCS rate**, **TDS rate**
- **Per-fee GST rate** (for the fee, not the underlying good)
- **Notes** (the methodology team's annotation explaining special handling)

### 7.2 The refresh discipline

- Methodology team monitors marketplace fee announcements weekly
- New rate cards are validated against actual settlement files from FDE-fleet clients before publication
- Validation: the new rate card, applied to the previous month's orders, must produce forecasts within tolerance of the actual settlements (excluding known anomalies)
- Published rate cards are immutable; corrections produce a new version with a clear correction note

### 7.3 Per-client overrides

Some clients have negotiated rates. These are captured in the client app's `rate_card_overrides.json` fixture. The override is a delta against the standard rate card — tighter commission, different storage tier, etc.

The methodology team reviews proposed overrides quarterly. Broadly-applicable patterns (e.g., a marketplace introduces a tiered structure across all its sellers) are absorbed into the standard library.

---

## 8. The Month-End Close Playbook

The close playbook is the methodology's prescribed sequence for closing books at month end. The product enforces the sequence — out-of-order operations produce warnings.

### 8.1 The sequence (per Frappe Company)

1. **Day -2 (T-2 of close)**: All EasyEcom syncs current. Sync Records show 0 Failed. Schema drift dashboard reviewed. Configuration changes since last close audited.
2. **Day -1**: All settlement files for the month received and ingested. Fee-to-Expense recon attempted; resulting Discrepancies triaged.
3. **Day 0 (close day)**: Order-to-Settlement recon run; Discrepancies triaged. Return-to-Credit-Note recon run; Discrepancies triaged. ITC reconciliation against GSTR-2B run; Discrepancies triaged.
4. **Day +1**: TCS-to-Government recon run. Bank-to-System recon run. Inventory variance recon run.
5. **Day +2**: All Critical Discrepancies must be either resolved or have a formal write-off journal entry. All Error Discrepancies must be either resolved or formally deferred to next month with explicit rationale.
6. **Day +3**: Methodology team reviews the close report. Sign-off recorded.

### 8.2 The close report

A close report is produced monthly per Company. Methodology team's KPIs are computed from these reports across the FDE fleet. Contents:

- Total Pre-Loss Revenue, Post-Loss Revenue, realised margin %
- Discrepancies opened, resolved, written off, deferred (counts and ₹ value per type)
- Recon coverage % (lines reconciled / lines total) per recon
- ITC claimed vs ITC reconciled to GSTR-2B
- Marketplace Other Charges balance — methodology guardrail KPI
- Any Methodology Drift events (per Section 9)

---

## 9. Pricing Diagnostic Thresholds

The recon engine includes a pricing diagnostics module that compares actual prices realised on settlement files against catalogue prices in ERPNext. The methodology defines thresholds for what constitutes "abnormal" pricing.

### 9.1 The principle

Marketplaces engage in algorithmic repricing. A seller's actual realisation per SKU on Amazon may differ from their catalogue price by ±5% routinely, ±15% during sales events, and >20% in pricing anomalies that may indicate listing errors or marketplace pricing bugs. Detecting these systematically gives the seller a chance to react.

### 9.2 The default thresholds

| Variance from catalogue | Diagnostic |
| --- | --- |
| Within ±2% | Normal |
| ±2% to ±10% | Attention (review trend, not action) |
| ±10% to ±25% | Investigate (likely a sales/promotion event or category-level deal) |
| Below -25% | Critical — listing error or pricing bug suspected; alert finance leadership |
| Above +25% | Critical — possible double-pricing or platform anomaly; verify before claiming |

### 9.3 Per-SKU baselines

The methodology's diagnostic engine maintains a per-SKU rolling 30-day median realised price and uses that as the comparison baseline rather than the static catalogue price. This handles category-level repricing automatically; only SKU-specific anomalies trigger the diagnostic.

---

## 10. The Methodology Lifecycle

How a methodology rule moves from idea to default to deprecated.

### 10.1 The states

| State | Meaning | Permission to use in production |
| --- | --- | --- |
| Proposed | Idea on the methodology team's roadmap | Not yet — internal discussion |
| Pilot | Implemented in code, validated on 1-3 FDE-fleet clients | Yes, with informed consent |
| Default | Validated; ships as a fixture in the parent app | Yes, default behavior |
| Deprecated | Superseded by a new default; existing clients continue using if not yet migrated | Yes, with warning surfacing in Morning Brief |
| Removed | Removed from code; clients on it must migrate | Not for new clients |

### 10.2 The validation bar for "Default"

A proposed rule moves from Pilot to Default when:

- It has been validated against ≥3 FDE-fleet clients for ≥2 months
- The validation report shows: (a) the rule produced the expected behavior, (b) no false positives caused operational issues, (c) recovered/avoided value > implementation cost
- Two methodology team members have reviewed and signed off (the CA and one of the operations specialist or senior consultant)

### 10.3 Deprecation discipline

When a default is replaced:

- The new default ships in the parent app
- Existing clients continue on the old rule until their FDE schedules a migration
- A migration window of 2-3 months is standard
- The Morning Brief shows the deprecation warning to FDEs of clients still on the old rule
- Methodology team provides a migration playbook in `docs/playbooks/methodology_migration_<rule_name>.md`

### 10.4 Per-client variance documentation

When a client requests a deviation from a default rule (and methodology team approves), the deviation is captured in:

- The client app's relevant fixture (the actual override)
- `apps/ecommerce_super_<client>/methodology_addendum.md` — a markdown document explaining what's different and why

The addendum is mandatory; methodology team will not approve a per-client deviation without it. This produces a per-client paper trail that survives FDE turnover.

---

## 11. The Methodology Team's Operating Model

How the methodology team itself is structured and how it makes decisions.

### 11.1 Composition

- **Methodology Lead** — practising CA with 10+ years e-commerce reconciliation experience. Owns GST disposition rules, chart of accounts, ITC rules. Final authority on tax-correctness questions.
- **Operations Specialist** — marketplace operations, 7+ years across Amazon/Flipkart/Meesho. Owns rate card library, Account Role Map, Discrepancy taxonomy.
- **Senior Frappe Consultant** — 8+ years ERPNext and Frappe. Owns the methodology-to-implementation translation; reviews all SPEC.md changes that touch methodology.

### 11.2 Cadence

- **Weekly (1 hour)**: methodology team review of FDE-fleet incident reports, Marketplace Other Charges trends, Discrepancy frequency per client
- **Monthly (2 hours)**: review of close reports across the fleet, methodology-drift events, proposed rule changes
- **Quarterly (1 day)**: methodology audit — review of every default for staleness, per-client overrides for promotion to standard, retired rules for cleanup
- **Annual**: full methodology version bump with a written summary of changes

### 11.3 Decision rights

Decisions that require methodology team approval (one or more team members must sign off, depending on type):

- Adding a new account to the standard chart of accounts: methodology team consensus
- Changing GST treatment of a fee category: Methodology Lead + one other
- Adding a new Discrepancy type: Operations Specialist + Methodology Lead
- Loosening a recon tolerance below the default: Methodology Lead
- Approving a per-client deviation from a default: any team member, with addendum
- Shipping a new Default rule: at least two team members
- Deprecating a Default rule: at least two team members + a migration playbook

### 11.4 What methodology team does NOT decide

- Implementation details (how a rule is encoded in code) — that's engineering
- Per-FDE engagement specifics (client communication, invoicing) — that's commercial
- Spec.md changes that don't affect methodology — that's engineering
- PRD changes — that's product

The methodology team is opinionated on what the answers are, not on how those answers are delivered.

---

## 12. Documentation and audit trail

Every methodology rule, default, and change has a paper trail. This is non-negotiable.

### 12.1 Rule documentation

Every default rule has:

- A markdown document in `docs/methodology/rules/<rule_name>.md` explaining the rule, its rationale, and its evidence base
- A code implementation in `apps/ecommerce_super/ecommerce_super/methodology/`
- A test suite in `apps/ecommerce_super/ecommerce_super/tests/methodology/`
- A fixture in `apps/ecommerce_super/ecommerce_super/fixtures/` if it ships as configurable data

### 12.2 Change history

Every methodology rule change is captured in:

- A git commit with a clear methodology-tagged message: `methodology: <rule_name>: <action> — <rationale>`
- A note in the monthly methodology team review minutes
- A `CHANGELOG.md` entry under the methodology section
- For Default-to-Default changes: a migration playbook for FDEs

### 12.3 Audit-ready

For clients undergoing tax audits or regulatory review, the methodology team can produce:

- The full chart of accounts with rule rationale
- The Account Role Map with all per-marketplace and per-client rules
- The applicable rate cards by date range
- The methodology version active during the audit period (versioned in `methodology/VERSION`)

This is a competitive moat. SME sellers facing audits routinely struggle to explain their reconciliation methodology to auditors. A client on this methodology can produce a complete, signed-off, version-controlled paper trail in hours.

---

## 13. Open methodology questions (currently unresolved)

The methodology is not complete. These are known gaps the methodology team tracks:

- **Multi-currency support**: orders/settlements in non-INR are not currently handled. v0.5+ work.
- **Subscription-style marketplace fees**: monthly platform fees (e.g., Amazon Professional Seller fee) are not yet in the Account Role Map standard. Currently goes to Marketplace Other Charges.
- **Marketplace-financed promotions** (e.g., Amazon-funded discount events): the seller's net realisation differs from list price in ways that need methodology refinement.
- **B2B marketplace channels** (Amazon Business, Flipkart Wholesale): GST treatment is in-scope but tax invoice formatting is currently handled via custom logic per client, not yet in standard.
- **Composition scheme sellers**: the methodology assumes regular GST registration. Composition-scheme sellers are out of scope until methodology v0.7+.
- **GST rate change events**: when government changes a rate mid-month, the methodology's automated handling is not yet defined; currently FDE manual adjustment.

These are tracked in `docs/methodology/open-questions.md` and reviewed quarterly.

---

## 14. Glossary

| Term | Definition |
| --- | --- |
| Account Role Map | Bridge table from marketplace events to standard chart of accounts |
| Discrepancy | A reconciliation result outside tolerance |
| FDE | Forward-Deployed Engineer; the operator who deploys and runs the integration for a client |
| GMV | Gross Merchandise Value; total order value |
| GSTR-2B | Government's auto-generated monthly statement of input tax credit available |
| ITC | Input Tax Credit; recoverable GST on purchases and fees |
| Methodology v0 | The methodology version live at v0.1 product launch; signed off by the CA before code ships |
| Pre-Loss Revenue | Marketplace Revenue (Gross) — the headline revenue number |
| Post-Loss Revenue | Pre-Loss less all marketplace deductions, returns, and unrecovered receivables variance |
| Rate Card | Versioned per-marketplace fee schedule |
| Settlement Forecast | The methodology's expected net amount at order time |
| TCS | Tax Collected at Source; marketplace withholds and remits |
| TDS | Tax Deducted at Source; marketplace withholds and remits |
| Tolerance | Per-recon variance threshold below which a line is considered reconciled |

---

*This BRD is owned by the Methodology team. Edits require sign-off per Section 11.3. Changes are captured in CHANGELOG.md under the methodology section.*
