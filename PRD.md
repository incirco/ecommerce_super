# ERPNext E-commerce Super-App — Product Requirements Document

**Version:** 2.1 — Working Document
**Date:** May 2026
**Companion documents:** SPEC.md (technical contract), BRD.md (methodology), CLAUDE.md (build guide)
**Audience:** Leadership, prospects, FDE leads, Methodology team

---


# 1. Executive Summary

## 1.1 What this document is

This is the foundational design document for an ERPNext-native financial backbone for Indian multi-channel e-commerce sellers running on EasyEcom. The product is currently codenamed the ERPNext E-commerce Super-App. Sections 1 to 4 are written for executives and prospects; later sections are progressively more technical.

This PRD is companion to two other documents: SPEC.md (the engineering-grade technical contract — every DocType, every API endpoint, every error class), and BRD.md (the methodology embedded as business rules — opinionated chart of accounts, GST disposition rules, recon thresholds). When a question is about "what we're building," this PRD is authoritative. When a question is about "how we're building it," SPEC.md is authoritative. When a question is about "what makes it correct for the business," BRD.md is authoritative.

## 1.2 The problem

Indian multi-channel e-commerce sellers running on ERPNext lose two to five percent of revenue to silent leakage — short payments, weight discrepancies, lost-in-transit shipments, missed claim windows. They cannot close their books cleanly because no system reconciles settlement files into the general ledger automatically.

Existing reconciliation tools (UniReco, EasyReco) handle the mechanics but assume the seller has a competent reconciliation analyst on staff and a defined methodology to apply. SME sellers in our target band have neither. The tools are bought, used briefly, and quietly abandoned.

## 1.3 The product

We ship an opinionated methodology for marketplace reconciliation in Indian e-commerce, embodied in ERPNext-native software, deployed by Forward-Deployed Engineers. The product comprises:

- **A Standard Methodology** — recommended chart of accounts, opinionated tolerance defaults, sequenced month-end close playbook, standard reconciliation rules, GL posting structure with proper GST treatment. Developed and maintained by a small Methodology team (a practising CA, a marketplace operations specialist, and a senior Frappe consultant).
- **Bidirectional EasyEcom integration** — pulls operational data, pushes Sales Orders, POs, Items, Customers, inventory adjustments. Built on a path-based Field Mapping engine that lets FDEs adapt to client-specific payload variants without code changes.
- **A central Rate Card Library** — maintained by us, refreshed when marketplaces change rates, push-and-subscribe to client sites.
- **Order-time forecasting** — every Sales Order on submit produces an expected net settlement, surfaced as a Net Receivables view.
- **Deterministic reconciliation** — five reconciliations with per-line variance computed against the forecast.
- **AI-driven first-pass classification** — every Discrepancy auto-classified with claim narrative drafted before the operator sees it.
- **Operational surface designed for FDEs** — Morning Brief, recon-aware alerts with financial impact, replay tooling with mandatory dry-run, schema drift detection, time travel, error translation library. The integration is observable and operable, not a black box.
- **ERPNext-native GL posting** — reconciled events become Journal Entries, Payment Entries, Purchase Invoices with auditable back-references.
- **Diagnostic Onboarding** — free 2-week pre-contract engagement run by the FDE on the prospect's six-month historical data, producing a leakage report.
Pricing is fixed platform fee plus FDE retainer. No success fee.

## 1.4 The wedge

UniReco and EasyReco are mature reconciliation engines. We will not catch them on reconciliation depth. We win on four things they do not provide:

- **ERPNext-native GL posting.** Their output is a report; ours is a journal entry.
- **Order-time forecasting.** Theirs is backward-looking only; ours produces a CFO-facing daily Net Receivables view.
- **Opinionated methodology.** Theirs are configurable tools that assume the customer brings opinions the customer does not have; we ship the answers.
- **Recon-aware integration alerts.** Every integration alert says "this thing failed AND here is what it cost you in INR." Their alerts are noise channels for engineers; ours are signal channels for finance.
For a seller already on ERPNext and EasyEcom, we are not a worse reconciliation engine — we are a different category of product.

## 1.5 Why this is defensible

- Methodology is the deepest moat. UniReco can copy our software in eighteen months; nobody can copy a methodology refined across hundreds of clients over five years and accepted as the de facto standard by the broader CA community.
- ERPNext is the only mainstream Indian ERP capable of supporting this depth of integration. Tally and BUSY cannot.
- UniReco does not work on EasyEcom — it requires Uniware as the OMS. Our segment cannot use UniReco even if they wanted to.
- Centrally-maintained Rate Card Library means a new client subscribes in thirty minutes; a UniReco onboarding spends two weeks entering rate cards from scratch.
- AI-driven first-pass classification reduces operator skill required by an order of magnitude.
- Diagnostic-first sales motion proves value before the contract is signed.
- Opt-out cross-client benchmarking (DPDP-compliant) feeds the Methodology team with the data flywheel competitors cannot match.
- Parent-app + per-client child-app architecture lets us serve heterogeneous clients without snowflake code.

## 1.6 Phasing

- **v0.1 — 38-46 weeks** with five engineers. Tracer bullet at week 32 (alpha) with the integration mechanics complete and four must-have operational pieces shipped (path-based Field Mapping, recon-aware alerts, Morning Brief, error translation). Final v0.1 at week 46 with the remaining six operational directions (queryable analytics, replay tooling, schema drift, SLA tracking, cross-Company ops, time travel). Methodology v0 signed off by CA before code starts (hard gate).
- **v0.5 — Production for early adopters, 3-4 months after v0.1.** All five reconciliations at scale, multi-marketplace ingestion at production volume, opt-out cross-client benchmarking, three to five clients in production.
- **v1.0 — Productised platform, 6-9 months from v0.1.** Aggregator topology hardened to 10+ Companies per site, self-service onboarding wizard for non-FDE-led integration setup, Frappe Cloud Marketplace listing, partner FDE programme.

## 1.7 Top risks

- **Methodology correctness (most material).** A flawed chart of accounts or miscalibrated default damages every client simultaneously and is not patchable like a software bug.
- **AI first-pass accuracy below 70%.** Would collapse the operator-skill-reduction value proposition.
- **FDE economics.** May not work at sustainable price points if leakage recovery is closer to 0.5% than 2% of GMV.
- **Competitive response.** UniReco or EasyReco extending into ERPNext is plausible within 18-24 months.
- **v0.1 scope.** 38-46 weeks of engineering before a paying client is unprecedented for an SME-targeted SaaS. Larger v0.1 reflects deliberate methodology bet on operational surface as differentiator. Agile fallback (4 must-haves only, ~20 weeks) remains available.
Full risk register in Section 17.

> **Reading note:** Throughout this document, words in monospace such as `DocType`, `hooks.py` refer to specific Frappe Framework artefacts. Words capitalised mid-sentence such as Settlement Forecast or Rate Card Library refer to DocTypes or capabilities introduced by this product.

# 2. Problem Statement and Opportunity

## 2.1 The customer

Our customer is a founder, finance head, or operations head of an Indian D2C or multi-brand seller doing roughly five to two hundred crore rupees of annual GMV across multiple marketplaces. They share a recognisable profile:

- Sell on three or more of: Amazon, Flipkart, Myntra, Ajio, Meesho, Nykaa, JioMart, plus a Shopify or custom storefront.
- Use EasyEcom as their warehouse and order management system because it aggregates listings, inventory, and orders across all channels uniformly.
- Revenue growth is healthy. Operations are functional. But financial reporting lags reality by two to four weeks every month.
- The founder cannot tell you their true gross margin per channel without a spreadsheet exercise.
- The accountant cannot confidently say whether a ten-lakh shortfall in expected revenue is a real loss or a deferred settlement.
- The CA, at audit time, finds gaps in tax credits because GST input on marketplace fees was never properly split out.

## 2.2 What is broken today

### 2.2.1 Net-of-fees settlements are an unsplit black box

Every marketplace pays a single net amount — gross sales minus dozens of fee categories. Amazon's settlement file alone splits into:

- Commission, fixed fee, FBA pick-and-pack, FBA storage
- Sponsored ads, refund administration
- Weight discrepancies, A-to-Z claims, FBA inventory reimbursements
- TCS and TDS deductions
Each is a separate accounting line. Each carries distinct GST treatment. Most sellers post the net amount as a single bank credit and call it revenue, forfeiting GST input tax credit and losing the ability to detect when a fee was wrongly charged.

### 2.2.2 Reconciliation is manual and inevitably skipped

Manual line-by-line reconciliation is hours of work per marketplace per week. Most teams give up. Industry benchmarks consistently put silent leakage at two to five percent of GMV — money the marketplace owed but never paid, that nobody noticed.

### 2.2.3 Claim windows close before anyone notices

- Amazon's reimbursement window: 60 days to 18 months depending on category.
- Flipkart's SPF window: often only 7-14 days from event.
- Without a system that surfaces eligible claims with a deadline countdown, sellers miss the window and the money is gone forever.

### 2.2.4 Returns are never properly closed in the books

- A return triggers a reverse-logistics chain of 3-30 days.
- The marketplace deducts the refund from the next settlement immediately.
- The physical inventory comes back days later, sometimes never.
- The bookkeeping rarely connects the two events.
- Result: shrinkage that looks like normal cost of goods sold and an overstated working-capital position.

### 2.2.5 TCS and TDS are unreconciled with GST returns

- Marketplaces deduct TCS at 0.5% under Section 52 CGST (reduced from 1% in July 2024).
- Marketplaces deduct TDS at 0.1% under Section 194-O of the Income Tax Act (reduced from 1% in October 2024).
- Both should be reconciled against the operator's GSTR-8 filings (visible in GSTR-2B) and Form 26AS Part F.
- Almost no SME seller does this reconciliation. Lakhs of rupees in legitimate tax credits go unclaimed every year.

## 2.3 Existing tools and where they leave gaps

### 2.3.1 What UniReco and EasyReco do well

Two products in the Indian market specifically address marketplace reconciliation: Unicommerce's UniReco and EasyEcom's EasyReco. Both are mature and deployed at scale. They handle the operational mechanics competently:

- Versioned channel rate cards with slabs by selling price, by category, by warehouse type
- Fee structures covering commission, shipping, fixed, collection, pick-and-pack, refund commission, technology fees, rebates
- Current TCS and TDS rates correctly encoded
- Forward, return, and rebate reconciliation as separate flows with their own dispute queues
- Failed-reconciliation diagnostics categorised by reason
- Delta-based tolerance matching, UTR-level transaction matching
- Coverage of Amazon FBA / Easyship / Flex, Flipkart / F-Assured, Myntra PPMP / SJIT, Meesho
Treating any of these as our differentiator would be a mistake. Both products do them, and both have years of edge cases caught and handled that we do not have.

### 2.3.2 The three gaps both products leave

- **Output is reports, not GL postings.** Both produce reconciliation dashboards and exportable files. Neither posts journal entries into the seller's accounting system. The seller manually re-keys results into Tally, BUSY, or ERPNext.
- **Reconciliation is backward-looking only.** Neither runs an order-time forecast. There is no Net Receivables view; CFO-side cash-flow planning happens in spreadsheets.
- **Both are configurable tools that assume the customer brings opinions.** The customer's opinion is usually "I don't have one — what should it be?" Neither product answers that question. Neither ships a methodology. SME sellers in our target band have neither a competent reconciliation operator nor a defined methodology, so the tools are bought, used briefly, and quietly abandoned.

### 2.3.3 Two structural gaps that benefit us

- **UniReco does not work on EasyEcom.** It requires Uniware as the OMS. Sellers running on EasyEcom cannot use UniReco without migrating WMS — a multi-month project most will not undertake.
- **Neither owns the rate card or the methodology.** Each customer maintains their own, and most don't keep them current. When Flipkart updates commission slabs, every UniReco customer must update their own data, and most don't immediately.

## 2.4 Why now

- Indian e-commerce has crossed an inflection where the typical seller operates on three or more channels. Single-channel sellers can manage with marketplace dashboards; multi-channel sellers cannot.
- ERPNext adoption in Indian SMEs has grown materially since version 14, particularly because of the maturity of the India Compliance app for GST.
- The cost of capable LLMs has dropped to a point where running an AI assistant against per-client data is economically viable.
- The reduction of TCS to 0.5% and TDS to 0.1% is recent enough that most existing systems still encode the old rates — a new product can launch correct from day one.

# 3. The Standard Methodology

## 3.1 Why methodology is the central pillar

This product exists because the customer does not know how to do marketplace reconciliation. They lack the standards, the time, and the in-house skill, and often even the awareness that there is a correct way to do it. Selling them a configurable tool is selling them a faster way to fail at something they don't know how to do. We must instead ship the answer.

The Standard Methodology is the answer. It is an opinionated, documented, software-embodied way of doing marketplace reconciliation, posting to the general ledger, and closing the books for an Indian e-commerce seller running on ERPNext. It is developed and maintained by the Methodology team (Section 4), refined through opt-out cross-client pattern observation, and signed off by a practising CA before release. The Forward-Deployed Engineer is the deployment specialist who carries it into a specific client environment; the FDE does not invent methodology.

Everything else in this document — the data model, the recon engine, the AI assistant, the FDE operating model — is in service of delivering and evolving this methodology. The methodology is the product; the software is the vehicle.

## 3.2 The eight components of the Methodology

The Standard Methodology covers eight components, each documented and embodied in a specific part of the system.

| Component | What it is | Where it lives |
| --- | --- | --- |
| Recommended Chart of Accounts | Standard set of GL accounts for marketplace operations: control accounts per marketplace, fee accounts per category, GST ITC handling, TCS/TDS receivables, claims and reserves | Parent-app fixture; client's CA finalises during onboarding; role-mapping layer translates methodology account names to client's chosen names |
| Tolerance and Decision Defaults | Standard thresholds for variance tolerance, write-off limits, claim filing thresholds, AI auto-approval thresholds, materiality cutoffs | Methodology Defaults DocType in parent app; client can override per Marketplace Account |
| Reconciliation Rules | How to treat each canonical scenario: short-payments, weight discrepancies, lost-in-transit, returns-without-goods-receipt, ad spend allocation, promotional cost-shares, partial settlements | Discrepancy Rule library shipped with parent app, written in DSL |
| GL Posting Rules | How each canonical event becomes journal entries with proper GST treatment | GL Posting Rule library; methodology team owns; FDE applies during onboarding |
| Month-End Close Playbook | Sequenced day-by-day operator tasks for the close | Scheduled tasks in the system that prompt the operator on the right days |
| Rate Card Library | Current marketplace and courier rate cards, refreshed centrally | Parent-app DocTypes with publish-and-subscribe to client sites |
| Standard KPI Set | The metrics every client should track: leakage % of GMV, claim recovery rate, settlement timing, variance attribution, fee growth rate | Built-in dashboards; benchmark numbers from cross-client data once benchmarking is mature |
| Diagnostic Onboarding Protocol | The standard 2-week diagnostic methodology — what data to pull, what report to produce | FDE playbook; sandbox deployment runbook |

## 3.3 Opinionated, not configurable

The methodology makes choices for the client that the client doesn't have the expertise or the time to make themselves. Examples of choices the methodology makes:

- Tolerance for shipping fee variance is ₹5 per shipment.
- Auto-write-off threshold for short-payments is ₹50.
- Claim filing threshold is ₹100 — below this, the operator's time costs more than the recovery.
- AI auto-approval is enabled for weight discrepancy claims under ₹500 on Apparel after 30 days of operation.
- Month-end close cut-off is the 7th of the following month.
- Returns where physical goods have not been received within 30 days of settlement deduction are reclassified as Lost Inventory and posted to Shrinkage.
- Ad spend is allocated to SKUs in proportion to sales of each SKU in the same period, on a weekly cadence.
Each is a small thing. Together they constitute the methodology. A client without these standards is making (or failing to make) each decision inconsistently every time it arises. With the methodology, each is decided once, by experts, and applied consistently.

### 3.3.1 The override discipline

Every methodology choice can be overridden. The client's CA has final authority. But overrides should be the exception, not the default. They require:

- A documented business reason recorded in the Methodology Override DocType
- FDE acknowledgement that the override has been considered and accepted
- Quarterly review during the FDE's business review with the client
This discipline matters because overrides accumulate. If every client overrides ten things, the methodology becomes meaningless. The override DocType is a forcing function that makes the override visible and reviewable.

## 3.4 The Recommended Chart of Accounts

The methodology ships a recommended chart of accounts covering approximately 40 specific accounts for marketplace operations. The client's CA reviews, adapts, and finalises during onboarding. The recon engine works against role names; an Account Role Map DocType translates roles to the client's actual account names. This preserves both methodology integrity and client-CA authority.

### 3.4.1 The account categories shipped

- Marketplace Receivable Control accounts — one per marketplace (Amazon, Flipkart, Myntra, Meesho, Ajio). Holds the running balance of expected-but-unreceived settlements.
- Marketplace Payouts In Transit — one per marketplace. Holds settlement amounts declared but not yet credited to bank.
- Commission Expense per marketplace — tracked separately so margin analysis attributes commission burden by channel.
- Shipping Expense — per marketplace and per courier (for D2C orders). Forward, reverse, RTO as sub-accounts.
- Fixed Fee Expense, Collection Fee Expense, Storage Fee Expense, Cancellation Penalty Expense — per marketplace.
- Marketplace Ad Spend per marketplace — not lumped with commission, deliberately distinct.
- Promotional Cost-Share per marketplace — for category-wide promotion charges.
- Rebate Income per marketplace.
- GST Input Tax Credit on Marketplace Fees — separate ledger so ITC claims are reconcilable to GSTR-2B.
- TCS Receivable per marketplace — holds TCS deducted by the operator pending GSTR-2B credit match.
- TDS Receivable - Section 194-O per marketplace — holds TDS pending Form 26AS Part F credit match.
- Marketplace Claims Receivable, Claims Recovered, Claims Lost.
- Reconciliation Variance — tolerance-absorbed variances too small to investigate per discrepancy.
- Marketplace Reserves & Holdback — for marketplaces that hold a percentage as reserve.
- Inventory Shrinkage - Marketplace Returns — for inventory deducted but not physically received back.

### 3.4.2 The role-mapping layer

The recon engine never references a literal account name. It posts to a role: 'commission_expense' for marketplace X. An Account Role Map DocType (one per Company) translates each role to the actual GL account name in that client's chart of accounts. The FDE configures the role map during onboarding, after the client's CA has reviewed and adapted the recommended structure. Cross-client benchmarking works on roles, not on account names — comparing 'commission_expense as % of GMV across clients' is straightforward even when one client calls it 'Amazon Commission' and another calls it 'AMZ Commission Expense'.

> **NOTE:** Risk acknowledged

## 3.5 Tolerance and Decision Defaults

The methodology ships a Methodology Defaults DocType (Single, parent-app) with standard thresholds:

```
# Tolerance defaults
variance_tolerance_per_line_inr      = 5     # absorb into Recon Variance below this
variance_tolerance_per_batch_pct     = 0.1
shipping_fee_tolerance_inr           = 5
commission_tolerance_pct             = 0.5

# Write-off defaults
auto_write_off_threshold_inr         = 50
claim_filing_threshold_inr           = 100
unmatched_settlement_age_days        = 60

# AI auto-approval defaults
ai_autoapprove_after_days            = 30
ai_autoapprove_max_amount_inr        = 500
ai_autoapprove_min_confidence        = 0.90

# Close cycle defaults
close_cutoff_day_of_next_month       = 7
return_inventory_max_age_days        = 30
ad_spend_allocation_cadence_days     = 7
recon_run_frequency_days             = 7
```

Every default is overridable per Marketplace Account or per Company via the Methodology Override DocType. Overrides are visible in the FDE quarterly review report so they are revisited regularly.

## 3.6 The Standard Reconciliation Rules

The methodology ships a library of Discrepancy Rules covering canonical reconciliation scenarios. Rules are written in the Discrepancy Rule DSL (a sandboxed Python expression syntax) and shipped as parent-app fixtures. Three illustrative rules:

```
# Rule MO-001: Standard short-payment
condition: discrepancy_type == "Settlement Variance" \
           and variance_amount < 0 \
           and abs(variance_amount) > settings.claim_filing_threshold_inr
classification: "Short Payment"
suggested_action: "File reimbursement claim with marketplace"
sla_days: 60

# Rule MO-002: Standard weight discrepancy
condition: discrepancy_type == "Fee Variance" \
           and fee_subtype == "Shipping" \
           and weight_charged > weight_declared * 1.10
classification: "Weight Discrepancy"
suggested_action: "File SPF/weight-discrepancy claim"
sla_days: 14

# Rule MO-005: Standard auto-write-off
condition: discrepancy_type == "Settlement Variance" \
           and abs(variance_amount) <= settings.auto_write_off_threshold_inr
classification: "Below Materiality - Auto Write-off"
auto_resolve: True
```

The methodology team maintains the rule library, evolves it based on cross-client patterns, and ships updates with the parent app. Each rule has a methodology version stamp.

## 3.7 The Standard Month-End Close Playbook

The methodology defines a sequenced 10-day close cycle, embodied as scheduled tasks that prompt the operator on the right days.

| Day | Task | Owner |
| --- | --- | --- |
| Day 1 | Pull all settlement files for the period from each marketplace | Operator |
| Day 2 | Run reconciliation; review AI-classified discrepancies | Operator |
| Day 3 | File pending claims through marketplace dispute portals | Operator |
| Day 4 | Bank statement import and Payout-to-Bank reconciliation | Operator |
| Day 5 | Returns reconciliation: match return events to credit notes and physical receipts | Operator |
| Day 6 | Fee-to-Expense recon: post Purchase Invoices for fees with GST ITC split | Operator |
| Day 7 | GSTR-2B import; TCS recon against GSTR-2B and Form 26AS. Settlement files received after this roll to next period. | Operator + CA |
| Day 8 | Run management close report: leakage %, claim recovery, fee growth, channel margin trends | FDE / Operator |
| Day 9 | FDE review: open Discrepancies past SLA, overrides for review, methodology compliance | FDE |
| Day 10 | Methodology team feedback loop: anonymised pattern observations sent to Methodology team | System (auto) |

The operator does not invent the close cycle. They follow the playbook. The system tells them what to do today. If they fall behind, the system surfaces the lag and the FDE escalates.

## 3.8 Methodology versioning and evolution

The methodology has its own version number, distinct from the product version.

- Methodology v1.0 ships with parent app v0.1.
- Minor versions (v1.1, v1.2) add new rules and refine defaults.
- Major versions (v2.0) involve structural changes to the chart of accounts or close cycle that require client-CA re-review.
- Minor updates push to subscribed clients with a 7-day review window. The FDE notifies the client; if no objection, the update activates.
- Major updates require explicit FDE-led client review and sign-off before activation.
The methodology evolves based on data. Cross-client benchmarking provides the Methodology team with the data they need to know whether the methodology is working — whether clients consistently override defaults in one direction (default is wrong, recalibrate), whether claim recovery rates are lower than the methodology assumes (classification rule is wrong, revise). Without this data flywheel, the methodology calcifies.

## 3.9 What this means for the buyer

From the prospect's perspective, the difference between us and a configurable tool is something like this:

> Configurable tool: 'Here is a powerful reconciliation engine. Tell us what your tolerances should be, what your account structure should be, what your discrepancy classifications should be, what your close cycle looks like. We will configure it for you.' — Customer thinks: 'I don't know any of those things. I came to you because I don't know.'

> Methodology + system: 'Here is the right way to do marketplace reconciliation in ERPNext. We have studied this across hundreds of clients. We have a recommended chart of accounts your CA can adapt, recommended tolerances based on cross-client data, a sequenced 10-day close cycle your operator follows, AI that does the discrepancy classification for them. You can override anything but most clients should not. Here is your diagnostic showing you have ₹47 lakh of unrecovered claims in the last 6 months. Sign here.' — Customer thinks: 'Yes, I want that.'

# 4. The Methodology Team

## 4.1 Why this is its own team

If the FDEs maintain methodology in their spare time, methodology decays. Client work always wins over IP work because client work has deadlines. The Methodology team exists to solve this structurally:

- Their entire job is methodology.
- They have no client delivery responsibilities.
- They report to the founder, not to engineering.
- Their performance is measured on methodology quality, not on client billable hours.

## 4.2 Composition

Three people minimum at v0.1, scaling as the methodology grows:

- **A practising Chartered Accountant** with at least three years of marketplace seller clients in the SME segment. Owns books-side correctness — chart of accounts, GST treatment, TCS/TDS handling, close cycle, audit trail. Their signature on methodology v0 is a hard gate before v0.1 code ships.
- **A marketplace operations specialist** with prior experience at UniCommerce, EasyEcom, Browntape, or as a Category Manager at a major Indian marketplace. Owns marketplace-side correctness — rate cards, reconciliation rules, discrepancy classifications, claim filing protocols, marketplace-by-marketplace quirks.
- **A senior Frappe/ERPNext consultant** with at least five years of ERPNext implementation experience including India Compliance, GST e-invoicing, and complex multi-company setups. Translates the methodology into Frappe constructs. Bridge between the CA and the engineering team.
The CA hire is the most critical and the hardest. Expect a 2-3 month search. Do not compromise on the qualification — must be practising, must have marketplace clients, must be willing to put their name on the methodology. Without the right CA, we don't have a credible product.

## 4.3 Responsibilities

### 4.3.1 Develop methodology v0

Before any v0.1 code is written, the team produces methodology v0. Estimated 8-12 weeks of focused team work. Deliverables:

- Methodology v0 document — 30-50 pages, written, signed by the CA
- Recommended chart of accounts as an importable Frappe fixture
- Tolerance defaults document with reasoning for each number
- Initial Discrepancy Rule library — at least 20 canonical rules
- GL Posting Rule library covering every canonical event
- Month-end close playbook documented for operator consumption
- Diagnostic Onboarding protocol — what FDE does in the 2 weeks, what report comes out
- Standard KPI definitions — exact formulas, exact denominators, target ranges

### 4.3.2 Maintain the Rate Card Library

Half of the team's recurring work. They:

- Monitor marketplace announcements, courier rate updates, and GST notifications
- Draft, test, and release Library updates
- Handle escalations from FDEs when client data suggests a Library entry is wrong

### 4.3.3 Evolve methodology from cross-client data

As benchmarking data accumulates (post v0.5), the team analyses it:

- Override patterns become methodology revisions
- Recurring AI classifications become Discrepancy Rules
- Outlier client behaviours become opt-in template variants
- Methodology evolves at quarterly cadence with patch-level changes between

### 4.3.4 Certify FDEs

Every FDE, before being assigned a client, completes a methodology certification administered by the team:

- One-week course plus written exam
- FDEs are recertified annually as the methodology evolves
- This is how we ensure the methodology is deployed consistently across clients

### 4.3.5 Publishable IP

As the methodology matures, the team publishes:

- Articles in CA journals
- Sessions at the ICAI Institute
- Presentations at marketplace seller conferences
Goal: the methodology becomes the de facto standard for how Indian SME marketplace sellers reconcile and post to the ledger. UniReco can copy our software in eighteen months; nobody can copy 'the standard' in five years.

## 4.4 Governance

The team reports to the founder, not to engineering or to client services. Methodology must not be subordinated to either engineering deadlines or client revenue. Their KPIs:

- Methodology adoption rate: 90% of clients on the latest minor version within 90 days of release
- Override rate per client: under 10% — if higher, the default is probably wrong and needs revision
- Rate Card Library freshness: 95% of entries refreshed within 30 days of marketplace announcement
- FDE certification pass rate (target: not too high — if every FDE passes, the certification is too easy)
- Recovery uplift attributable to methodology evolution — measured cohort by cohort

## 4.5 What happens if the team gets it wrong

Methodology error is the central risk of the entire product. A wrong recommended chart of accounts, a miscalibrated tolerance default, a flawed reconciliation rule — these affect every client. The blast radius is total. Mitigations:

- CA sign-off before any release
- Pilot with one client before broad rollout
- Override-rate monitoring as a canary signal
- Quarterly external review by an outside CA partner who is not on the team
- Rollback discipline with documented procedures for every release
The Methodology team's professional reputation is on the line with each release. This is a feature, not a bug — it ensures their incentive is alignment with correctness, not speed.

# 5. Product Vision and Non-Goals

## 5.1 Vision

> To become the default financial system of record for every multi-channel e-commerce seller in India running on ERPNext — the place where every rupee of revenue, every rupee of fees, every rupee of tax, and every rupee of recovered claim ends up correctly posted, reconciled, and reportable, automatically, against an opinionated methodology that defines what 'correct' means.

## 5.2 In scope

The product comprises:

- A bidirectional EasyEcom connector — pulls orders, returns, inventory, and master data; pushes Sales Orders, Purchase Orders, Items, Customers, and ERPNext-led inventory adjustments.
- A configurable file-ingestion pipeline for settlement files, payout statements, and tax statements, parsed by per-marketplace per-client templates into structured Settlement Batches and Lines.
- A central Rate Card Library — maintained by us, refreshed when marketplaces change rates, push-and-subscribe to client sites with a review window before activation. Per-client custom rate cards override the Library cleanly.
- A Forecasting Engine — at Sales Order on_submit, computes expected deductions and net settlement using current rate cards. Output is a Net Receivables view that is forward-looking, not just a list of unsettled invoices.
- First-class modelling of advertisement spend, rebates, and promotional cost-shares as distinct fee categories with dedicated GL accounts and proper GST treatment.
- A deterministic reconciliation engine performing five reconciliations: order-to-settlement, payout-to-bank, return-to-credit-note, fee-to-expense (with GST ITC splitting), and TCS-TDS-to-government-statement. Per-line variance computed against the forecast.
- An AI assistant performing autonomous first-pass discrepancy classification — every Discrepancy is auto-classified with claim narrative drafted before the operator sees it. Per-marketplace auto-approval thresholds for high-confidence patterns.
- Descriptive pricing diagnostics — per-SKU and per-channel margin breakdowns, variance attribution, 'why margin moved' explanations. No prescriptive recommendations in v1.
- A general-ledger posting layer converting reconciled events into ERPNext Journal Entries, Payment Entries, Sales Invoices, Credit Notes, and Purchase Invoices, following the methodology's account structure.
- A Diagnostic Onboarding capability — a 2-week pre-contract engagement run by the Forward-Deployed Engineer on the prospect's six-month historical data, producing a leakage report. Sandbox deployment, with a clean-deletion schedule for prospects who don't convert.
- Opt-out cross-client benchmarking (v0.5 onwards) — anonymised aggregated patterns improve AI classification and provide health-score benchmarks. DPDP-compliant by design.
- A parent-app + child-app extension framework — ninety percent of per-client variation expressed as configuration, ten percent in a thin Frappe child app maintained by the assigned FDE.
- Aggregator topology — multiple Frappe Companies under a single EasyEcom multi-tenant account with strict data isolation.

## 5.3 Out of scope

Discipline on non-goals is what keeps the product from drifting into a generic platform that does many things badly.

- **Order Management System.** EasyEcom remains the OMS.
- **Warehouse Management System.** EasyEcom and marketplace-owned warehouses (FBA, F-Assured, MSA) handle physical fulfilment.
- **Marketplace listing or catalog management.** Sellers continue to use marketplace portals or third-party listing managers.
- **Business intelligence.** ERPNext and Frappe Insights cover reporting; our job is to make the underlying data correct.
- **D2C storefronts.** Shopify and Magento remain the storefront.
- **Direct marketplace API connectors in v1.** All settlement data ingestion is via EasyEcom plus file upload.
- **Prescriptive pricing recommendations in v1.** Forecasts and variance analysis are descriptive. A wrong recommendation acted on by a seller can lose them volume, BuyBox position, or margin in non-recoverable ways. Deferred to v2.
- **AI that posts to the GL or files claims autonomously.** The AI does autonomous first-pass classification, drafting, and explanation; the output is always a proposed action awaiting human approval.
- **A managed reconciliation service.** The FDE configures and supports; the FDE does not perform the client's daily operations.

## 5.4 Design principles

### 5.4.1 Methodology over configuration over code

The product ships an opinion. Configuration handles per-client variation. Code is the last resort. When the FDE encounters a new client requirement, the question is not 'how do I write this in the child app' but 'what change to the methodology or parent-app configuration would let me deliver this without writing code.' If the answer is 'no clean configuration path exists,' the action is a parent-app feature proposal, not child-app code.

### 5.4.2 Deterministic core, advisory AI

Every number that ends up in the general ledger comes from deterministic Python with a clear, version-controlled rule. The AI exists to help humans with ambiguous discrepancies, drafting text, and answering questions — never to generate numbers. This separation is what makes the product audit-defensible.

### 5.4.3 Reconciliation as a queue, not a report

Discrepancies are records in a queue with assigned owners, SLA timers, and a defined workflow. The product is not done until somebody works the discrepancy or explicitly defers it.

### 5.4.4 Source-of-truth maps, not hardcoded ownership

Inventory, customer master, item master, and pricing each have a configurable source-of-truth per warehouse and per channel. The Warehouse Source-of-Truth Map lets the implementation team configure who wins per warehouse per data domain.

### 5.4.5 Upgrade safety is non-negotiable

The parent app must be upgradeable without breaking child apps. Parent-app DocType field changes follow semver, breaking changes go through patches, and child apps interact with the parent only through documented extension points.

# 6. Solution Architecture Overview

## 6.1 The three-layer model

The system has three architectural layers, separated so each can evolve independently.

### 6.1.1 Data plane

All persistent data lives in Frappe DocTypes. Contains:

- EasyEcom connector
- File ingestion pipeline
- Canonical reconciliation data model
- Rate Card Library subscription mechanism
- GL posting layer
Pure Python plus DocType JSON. This is what runs the business and what the auditor sees.

### 6.1.2 Forecasting and reconciliation engine

Pure deterministic Python that reads from the data plane and runs in two halves:

- **Forecasting half.** At the moment of order, computes expected deductions and net settlement using active rate cards. Output is stored as Settlement Forecast records.
- **Reconciliation half.** Reads settlement files when they arrive, performs the five reconciliations, computes per-line variance against the corresponding forecast, classifies discrepancies, and writes results back as Reconciliation Run, Discrepancy, and journal-entry records.
No network calls, no LLM calls, fully testable.

### 6.1.3 AI assistant

A separate service, not a Frappe-internal module:

- Talks to the Frappe site via a scoped API user
- Provides operator chat, autonomous first-pass discrepancy classification, claim narrative drafting, and rate-card PDF extraction
- Calls into the data plane through read-only endpoints
- Never writes to the GL
- Replaceable — we can swap the underlying LLM provider without touching the data plane

## 6.2 The two-app extension model

Within the data plane and reconciliation engine, code is split across two Frappe apps:

- **ecommerce_super (parent app).** Ships every DocType, every adapter, every methodology rule, and every default. Versioned strictly using semantic versioning.
- **ecommerce_super_<client> (child app).** One per client deployment, owned by the assigned FDE. Contains only client-specific configuration that cannot be expressed as parent-app DocType data.
The child app declares ecommerce_super in required_apps and uses parent-defined extension points to plug in its variations. The child app must never modify parent-app DocTypes or call parent-app private methods.

This pattern is borrowed from the established Frappe ecosystem — erpnext is the parent and apps such as india_compliance, hrms, and erpnext_easyecom layer on top using the same hooks system. We extend the pattern by formalising a registry pattern (Section 12) so a child app can override individual reconciliation rules, parsers, and AI tools without monkey-patching.

## 6.3 Data flow

Following one order from creation to fully reconciled illustrates how the layers interact:

```
  [Marketplace]                                                  [Bank]
       │ order placed                                                  │
       ▼                                                               │
  [EasyEcom]──── orders, returns, inventory ──────┐                   │
                                                   ▼                   │
                              ┌──── [Sales Order created in ERPNext]  │
                              │            │                           │
                              │            ▼                           │
                              │   [Forecasting Engine]                │
                              │   reads active Rate Cards             │
                              │            │                           │
                              │            ▼                           │
                              │   [Settlement Forecast]               │
                              │            │                           │
                              │            ▼                           │
                              │   [Net Receivables view]              │
                              │                                        │
  [Settlement file/upload] ──┼─── parsed by Settlement Template ───▶  │
                              │                       │                │
                              │                       ▼                │
                              │              [Recon engine]            │
                              │              ─ matches forecast        │
                              │              ─ computes variance       │
                              │              ─ posts to GL             │
                              │           ┌───────────┼──────────┐    │
                              │           ▼           ▼          ▼    │
                              │     GL postings   Discrepancy  Claim  │
                              │     (Journal       queue       queue  │
                              │     Entries)         │           │    │
                              │                       ▼           ▼    │
                              │              [AI: classify, draft] ◀──┘
                              │                       │
                              │                       ▼
                              │              [Operator approves]
                              │                       │
                              │                       ▼
                              └──────────[Pricing Diagnostics] ───▶ [reports]
```

## 6.4 Component summary

| Component | Purpose |
| --- | --- |
| EasyEcom Connector | Bidirectional sync — pulls orders/returns/inventory; pushes Sales Orders, POs, Items, Customers, inventory adjustments |
| Settlement Ingestion Pipeline | Parses settlement files via per-marketplace Settlement Templates |
| Rate Card Library | Centrally maintained marketplace and courier rate cards with subscription model |
| Forecasting Engine | Computes expected net settlement at Sales Order submit; powers Net Receivables view |
| Reconciliation Engine | Five reconciliations with variance vs forecast; classifies discrepancies; posts to GL |
| Pricing Diagnostics | Per-SKU margin breakdown and 'why margin moved' attribution |
| AI Assistant | Autonomous first-pass classification, claim drafting, chat, rate-card PDF extraction |
| GL Posting Layer | Converts reconciled events to ERPNext financial documents |
| Diagnostic Onboarding sandbox | Pre-contract environment for prospect leakage reports |
| Child-App Extension Registry | Documented hooks for client-specific parsers, rules, AI tools |

# 7. Data Ingestion

## 7.1 Two paths

All data entering the system comes through one of two paths. There is no third path in v1.

- **Path A — EasyEcom API.** Operational data (orders, returns, inventory, GRN, master data) pulled from EasyEcom on a defined schedule. EasyEcom is the source of truth for everything operational. We do not bypass EasyEcom by talking to marketplaces directly for operational data in v1.
- **Path B — file upload.** Settlement, payout, tax-statement, and bank-statement data uploaded as files (CSV, XLSX, MT940, OFX, or PDF where extractable). Covers the gap that EasyEcom does not address — settlement-level financial data — and avoids the cost of building five marketplace API integrations in v1.

## 7.2 The EasyEcom Connector

The integration with EasyEcom is bidirectional, deterministic, and operator-observable. The full technical specification — every DocType, every endpoint, every error class, every state transition — lives in SPEC.md (the Integration Specification, currently v1.2). What follows here is the executive summary of the integration's shape, sufficient for prospects and leadership to understand what the integration does without reading 150 pages of engineering detail.

### 7.2.1 What the integration does

- **Bidirectional**: pulls operational data (orders, GRNs, returns, manifests, inventory) and pushes ERPNext-side documents (Sales Orders, Purchase Orders, Items, Customers, B2B invoices, inventory adjustments).
- **Polling-first with webhooks as optimization**: every flow is reconcilable via deterministic polling on a cursor-tracked schedule. Webhooks reduce latency where they exist; we never depend on them for correctness.
- **Per-Company configuration**: every Frappe Company that uses EasyEcom has its own EasyEcom Settings record, scoped credentials, and policy controls. This is critical for our aggregator-topology customers.
- **ERPNext is never blocked by EasyEcom**: all outbound EE traffic is asynchronous through a Queue Job system. A Sales Order can be saved and submitted regardless of EE availability.
- **Idempotent and replayable**: every operation carries a deterministic idempotency key. Failed operations can be retried as-is, with override values, with mandatory dry-run before commit, or marked manually-resolved.

### 7.2.2 Resources covered

Across pull and push, the integration covers ten operational flows (full detail in SPEC.md Sections 4-9):

- Master sync (Items, Customers, Suppliers, Warehouses, Tax Categories, Channels) — bidirectional with field-level ownership matrix
- Buying flow (PO push, GRN pull, Purchase Receipt creation)
- Stock transfer flows (all four warehouse-pair combinations including in-transit)
- B2C / Marketplace sales flow (manifest detection, Sales Invoice creation, e-invoice integration)
- B2B sales flow (SO push with sync/async modes, Stock Reservation Entry mirroring, B2B invoice push)
- Returns and cancellations (six distinct flows including Return-Marked-Goods-Not-Received)

### 7.2.3 The operational surface

Beyond the integration mechanics, the parent app ships an operational surface designed for FDEs and operators (SPEC.md Sections 18-29):

- **Path-based Field Mapping engine** — declarative rulesets, FDE-editable, version-controlled. Adapt to client-specific payload variants without code changes.
- **Three log DocTypes** — Sync Record (entity-centric), API Call (call-centric), Webhook Event (inbound-centric). Different operator questions, different records.
- **Recon-aware integration alerts** — every alert carries a financial impact estimate. "This thing failed and here is what it cost you."
- **Morning Brief** — a single screen at 09:00 IST showing today's top 3 actionable items, anomalies, chronic neglect, health vs last week.
- **Replay tooling** — Replay Plan DocType with mandatory dry-run before commit; bulk filter; conditional retry; payload override.
- **Schema drift detection** — every payload hashed by shape; new shapes raise alerts before silent mis-mapping accumulates.
- **SLA budgets and tracking** — per-flow per-Company commitments with breach detection and in-context document indicators.
- **Cross-Company aggregator operations** — workspace and reports designed for FDEs managing multiple Companies.
- **Error translation library** — raw EE errors mapped to plain-English explanations with suggested actions. 50+ entries at v0.1.
- **Time travel** — Configuration Audit (append-only) plus Field Mapping Versions (snapshots on save) enable point-in-time queries.

### 7.2.4 Why this is in the PRD at all

Most product requirements documents leave integration detail to engineers. We don't, for two reasons. First, the integration is the foundation on which the recon engine sits — its correctness and observability directly affect whether reconciliation produces trustworthy outputs. Second, the operational surface (Morning Brief, recon-aware alerts, error translation) IS part of the product positioning — it's why the FDE can profitably operate the system at sustainable price points. A simpler integration would not support the methodology.

For technical detail at any level, refer to SPEC.md. For "why we built it this way" debates, this section plus Section 6 (Solution Architecture Overview) above is sufficient.

## 7.3 The Settlement Ingestion Pipeline

Every marketplace produces settlement files in a different format, and even within one marketplace the format varies by category, region, and seller account type. Rather than write a parser per marketplace, we model the parser as data — a Settlement Template DocType that defines:

- Marketplace and channel identifiers
- File format (CSV, XLSX with sheet name, fixed-width)
- Per-column mapping from source columns to canonical Settlement Line fields
- Per-column transforms (date format, sign convention, currency)
- Filtering rules and validation rules
- Mapping of marketplace-specific event types to canonical event types: Sale, Refund, Fee-Commission, Fee-Shipping, Fee-Fixed, Fee-Collection, Fee-Storage, Fee-RTO, Fee-Cancellation, Fee-Ad, Rebate, Promotion-Cost-Share, Reimbursement, Tax-TCS, Tax-TDS, Adjustment, Other
A Settlement Template is created by the FDE during onboarding for each marketplace the client uses, and updated when the marketplace changes its file format. The template is data, not code, so updates do not require a deployment.

Upload flow:

- User uploads a file through the ERPNext UI
- File stored as an attachment with content hash for deduplication
- Parsed in a background job, validated against template rules
- On success, materialised into a Settlement Batch with N Settlement Line children
- Reuploading the same file is a no-op
- Reuploading a corrected file overwrites the prior batch only after explicit operator confirmation, and only if the prior batch has not yet been posted to the GL
- Once GL postings exist, corrections require a reversing journal entry, never a silent overwrite

## 7.4 Bank statements and tax statements

Bank statements are ingested using ERPNext's native Bank Statement Import (CSV/MT940/OFX). We add no parser of our own; we extend the existing flow with:

- A custom field bank_transaction.marketplace_payout_link populated by the Payout-to-Bank reconciliation
- A Frappe report listing payouts that have not yet been matched to a bank credit
For TCS and TDS reconciliation, the operator uploads:

- GSTR-2B JSON downloaded from the GST portal — already supported by the india_compliance app, which we declare as a hard dependency
- Form 26AS Part F (typically a PDF, parsed with a small extractor we ship for the standard format)
The TCS-TDS-to-Government-Statement reconciliation compares marketplace-deducted amounts in Settlement Lines against amounts reported by the operator in these statements.

# 8. Canonical Data Model

## 8.1 Naming conventions

All DocTypes introduced by the parent app are prefixed with no namespace (Frappe convention) but live in the ecommerce_super module. Custom Fields added to ERPNext-core DocTypes are prefixed with ecs_ (e.g., sales_invoice.ecs_marketplace_order_id). This prefix ensures fixtures filter cleanly during export and prevents collision with other apps.

## 8.2 Core configuration DocTypes

| DocType | Purpose |
| --- | --- |
| EasyEcom Settings (Single) | Top-level connection config |
| EasyEcom Location | Per-location_key credentials and JWT cache |
| Marketplace | Master list of marketplaces |
| Marketplace Account | One per (Company, Marketplace) — seller_id, GSTIN, default warehouse |
| Marketplace Channel | Sales channel within a marketplace (FBA, FBM, Easy Ship, Self-Ship) |
| Settlement Template | Per-marketplace per-client file parser config |
| Marketplace Rate Card | Versioned commission/shipping/fixed/collection/storage/RTO/ad fee schedules |
| Courier Rate Card | Versioned weight slabs, fuel surcharge, COD handling, RTO charges per courier |
| Discrepancy Rule | DSL for classifying detected discrepancies |
| Account Role Map | Translates methodology role names to client's actual GL account names |
| Methodology Defaults (Single) | Tolerance and decision thresholds (overridable per Marketplace Account) |
| Methodology Override | Records each per-client deviation from methodology defaults with reason |
| Warehouse Source-of-Truth Map | Per-warehouse configuration of which system owns inventory and other masters |

## 8.3 Operational DocTypes

| DocType | Purpose |
| --- | --- |
| EasyEcom Sync Log | Audit trail of every API call (credentials redacted) |
| EasyEcom Sync Cursor | Persistent cursor per resource per location |
| EasyEcom Queue Job | Tracker for async EasyEcom operations and outbound push retries |
| EasyEcom Webhook Event | Raw webhook payloads with idempotency dedup |
| Settlement Batch | One per uploaded settlement file |
| Settlement Line | One per row in a settlement file; carries variance against forecast |
| Marketplace Payout | One per disbursement event; links to Bank Transaction |
| Marketplace Order Map | Bridge between marketplace order ID and ERPNext Sales Invoice |

## 8.4 Rate Card and Forecasting DocTypes

| DocType | Purpose |
| --- | --- |
| Marketplace Rate Card Slab (child) | One slab line within a rate card with condition_dsl, formula_dsl, gst_rate |
| Courier Rate Card Slab (child) | Weight slabs and surcharges within a courier rate card |
| Settlement Forecast | One per Sales Order; computed at submit, refreshed on rate-card change. Carries expected gross, fees, taxes, net, settlement date, and source rate-card snapshot |
| Settlement Forecast Line (child) | Per-fee-type breakdown in the forecast |

The condition_dsl and formula_dsl fields use the same sandboxed Python expression mechanism as Discrepancy Rules. Allowed names: gross, price, weight_kg, weight_dim, category, marketplace_tier, payment_mode, ship_zone, season_tag, plus standard arithmetic and helpers (slab_lookup, ceil, floor, max, min). The source_rate_cards JSON snapshot on Settlement Forecast means the forecast is reproducibly traceable even after rate cards change — important when answering 'why did this order have ₹X expected commission?' months later.

## 8.5 Reconciliation and claims DocTypes

| DocType | Purpose |
| --- | --- |
| Reconciliation Run | One per scheduled or manual recon execution |
| Discrepancy | One per detected mismatch awaiting action |
| Marketplace Claim | One per filed or fileable claim |
| Discrepancy Comment | Conversation thread on a Discrepancy |
| AI Reconciliation Suggestion | AI-proposed classification or action awaiting human review |

## 8.6 Custom fields on ERPNext-core DocTypes

To preserve the upgrade path, we add custom fields rather than overriding controllers. All custom fields are shipped as fixtures in the parent app with the ecs_ prefix. Notable additions: Item gets ecs_easyecom_company_product_id, ecs_marketplace_skus (Table), ecs_push_status. Warehouse gets ecs_easyecom_location_id and ecs_inventory_master. Sales Order gets ecs_settlement_forecast (link), ecs_expected_net, ecs_expected_settlement_date, plus push-status fields. Sales Invoice gets ecs_marketplace_order_id, ecs_marketplace, ecs_actual_net, ecs_variance_amount, ecs_variance_pct, ecs_settlement_status. Bank Transaction gets ecs_marketplace_payout (link). Journal Entry gets ecs_recon_run (link) and ecs_settlement_batch (link) so every posting traces back to its source.

## 8.7 Relationships at a glance

```
Marketplace 1───n Marketplace Account 1───n Marketplace Channel
                                  │
                                  ├─── n Marketplace Rate Card 1───n Rate Card Slab
                                  │
                                  ├─── n Settlement Batch  1───n Settlement Line
                                  │                                   │
                                  ├─── n Marketplace Payout           │
                                  │           │                       │
                                  │           └── 1 Bank Transaction  │
                                  │                                   │
                                  └─── n Marketplace Order Map  ◀────┤
                                              │                       │
                                              └── 1 Sales Invoice ─── n Sales Invoice Item
                                                          │
                                                          └─ n Sales Return / Credit Note

Sales Order 1───1 Settlement Forecast 1───n Settlement Forecast Line
Courier 1───n Courier Rate Card 1───n Courier Rate Card Slab
Reconciliation Run 1───n Discrepancy 0/1 Marketplace Claim
                                 │
                                 └─── 0/n AI Reconciliation Suggestion

Settlement Line ───→ links to Settlement Forecast Line for variance computation
```

# 9. Reconciliation Engine

## 9.1 Architectural shape

The reconciliation engine is pure deterministic Python organised as five recon implementations behind a common interface. Properties:

- Each recon is invoked by a Reconciliation Run record
- Runs to completion, writes Discrepancy records for unmatched items
- Optionally posts journal entries
- The engine has no knowledge of any specific marketplace
- All marketplace-specific logic lives in Settlement Templates, Discrepancy Rules, Rate Cards, and Marketplace Account Maps — which are configuration
```
class Reconciliation(Protocol):
    name: str
    def scope(self, filters: dict) -> Iterable[ReconRecord]: ...
    def match(self, record: ReconRecord) -> MatchResult: ...
    def classify(self, mismatch: Mismatch) -> Discrepancy: ...
    def post(self, match: MatchResult) -> JournalEntry | None: ...
    def run(self, recon_run: "Reconciliation Run") -> ReconSummary: ...
```

## 9.2 The five reconciliations

### 9.2.1 Order-to-Settlement

For every Sales Invoice with a marketplace_order_id:

- Find corresponding Settlement Lines (event_type Sale, with Refund offsets)
- Pull the Settlement Forecast computed at Sales Order submission
- Compute expected net (from Forecast), actual net (from Settlement Lines), variance
- If absolute variance exceeds tolerance: create Discrepancy of type Settlement Variance with sub-classification driven by which fee_type contributed most
- On within-tolerance match: mark sales_invoice.ecs_settlement_status = Settled and create a Payment Entry from the marketplace control account
- On out-of-tolerance variance: Payment Entry is still posted (the actual money was received) but a Discrepancy is also created

### 9.2.2 Payout-to-Bank

- For every Marketplace Payout, find matching Bank Transaction by UTR (preferred), then by amount and date within tolerance
- On match: link them and reduce the marketplace control account balance
- On no match: create Discrepancy of type Payout Missing Bank Match with sla_deadline = payout_date + 7 days

### 9.2.3 Return-to-Credit-Note

- For every Sales Return marketplace event in Settlement Lines, find matching Credit Note in ERPNext (matched on marketplace_return_id)
- On no match: attempt to construct one from EasyEcom return data; if that fails, create Discrepancy of type Return Without Credit Note
- On physical-return-not-received-but-deduction-taken (return event in settlement but no GRN in EasyEcom after N days): create Discrepancy of type Return Marked, Goods Not Received

### 9.2.4 Fee-to-Expense

For every fee Settlement Line:

- Aggregate by marketplace, by fee_type, by GST rate, by period
- Post a Purchase Invoice against the marketplace as Supplier with line items per fee_type
- Posting accounts come from the Account Role Map
- GST on fees is split out as ITC-eligible input tax — not lumped into expense
- Ad spend, rebate income, and promotional cost-shares each post to dedicated GL accounts
- Expected fees from the active Marketplace Rate Card are compared against actual
- Material variances per fee_type create a Discrepancy of type Fee Variance with the suspected sub-type inferred from which slab the variance maps to

### 9.2.5 TCS-TDS-to-Government-Statement

- For every Tax-TCS and Tax-TDS Settlement Line, aggregate by marketplace, by GSTIN (TCS), by PAN (TDS), by period
- Compare against GSTR-2B TCS section (auto-populated from operator's GSTR-8) and Form 26AS Part F
- Discrepancies are typically operator-side errors recoverable via grievance with the operator
- TCS/TDS deductions create entries in dedicated TCS Receivable and TDS Receivable accounts that close out when the credit is matched in the government statement

## 9.3 The Discrepancy Rule DSL

Classification of discrepancies is rule-driven. A Discrepancy Rule has:

- A condition expressed as a sandboxed Python expression (with a fixed allowed-names set)
- A classification label
- A suggested action template
- A claim_window_days value used to compute sla_deadline
- A priority for ordering
The rule library is shipped by the methodology team (Section 3.6). The engine evaluates rules in priority order against each detected discrepancy and assigns the first match. Unmatched discrepancies receive classification = Unclassified and are routed to the AI assistant for suggested classification.

## 9.4 The Claim Queue

Every Discrepancy with classification != None and recoverable_amount > 0 becomes a Marketplace Claim record. The claim queue is the primary operator interface.

- Shows open claims sorted by sla_deadline ascending
- Each row: marketplace, discrepancy type, amount, deadline countdown, suggested action, AI-drafted narrative
- Bulk actions: file claim (where the marketplace exposes a programmatic claim API; otherwise mark as Action Required), defer with reason, write off with approval workflow
SLA timers are non-cosmetic. A claim approaching its deadline raises a notification. A claim past its deadline is marked Lost and posts to a Claims Lost account, providing visibility on missed-claim leakage as a measurable KPI.

## 9.5 Tolerance, idempotency, replay

- Every reconciliation has configurable tolerance thresholds (per-line absolute, per-batch absolute, per-batch percentage)
- Below tolerance: mismatches are absorbed into a Reconciliation Variance account rather than producing Discrepancy records (avoids noise)
- Tolerances are set per Marketplace Account by the FDE during onboarding, with methodology defaults from Section 3.5
- Reconciliation Runs are idempotent: running the same recon over the same scope produces the same results; already-posted journal entries are not duplicated
- Reverse-and-Replay: marking a Reconciliation Run as Reverse-and-Replay reverses all journal entries posted by that run, deletes the Discrepancy records, and queues the recon for re-execution. Requires Accounts Manager role.

# 10. Forecasting and Pricing Intelligence

## 10.1 Why this is its own engine

Most reconciliation tools operate after the fact. The seller's hardest financial questions are forward-looking: how much will I actually receive for orders I have already shipped? When will the money arrive? Which SKU is silently bleeding margin because of a fee structure I underestimated? A backwards-looking system cannot answer any of these in time to act.

So the architecture introduces a forecasting engine that runs at the moment of order, not at the moment of settlement. It produces an expected outcome that the reconciliation engine compares actuals against. This single design choice converts the product from a backward-looking accounting tool into a forward-looking financial planning tool.

## 10.2 Rate Cards as data

Two distinct rate-card families exist and both must be modelled:

- **Marketplace Rate Cards.** Published by each marketplace, differ by category and price band, updated periodically (typically quarterly, but ad-hoc updates happen). Cover commission, shipping fee, fixed fee, collection fee, storage fee, RTO charges, cancellation penalties, ad spend, and promotional cost-shares.
- **Courier Rate Cards.** Used only for D2C and B2B orders where the seller has a direct courier relationship (Delhivery, Shiprocket, BlueDart, Ekart, DTDC, etc.). For marketplace-fulfilled orders (FBA, F-Assured, MSA), shipping is part of the marketplace rate card and the courier is invisible to the seller.

### 10.2.1 The slab DSL

Real rate cards do not fit cleanly into row-based slab tables. A real Flipkart fashion rate card might say: 'Commission is 5% for products under ₹500, 8% for ₹500-₹1000, 10% for ₹1000-₹2000, except in the apparel category where it is 7% flat, except during Big Billion Days where it drops to 4%, except for sellers in the Plus tier who get 0.5% rebate.' A row-based table either explodes into hundreds of rows or fails to express the logic.

So both condition_dsl and formula_dsl on a Rate Card Slab are small sandboxed Python expressions. Two example slabs:

```
# Slab 1 — Flipkart commission for Apparel under ₹500
condition: marketplace == "Flipkart" and category == "Apparel" and gross < 500
formula:   0.05 * gross

# Slab 2 — Delhivery surface, weight up to 500g, metro zone
condition: courier == "Delhivery" and service_level == "Surface" \
           and ship_zone == "Metro" and weight_kg <= 0.5
formula:   38 + 0.18 * 38     # base + fuel surcharge
```

### 10.2.2 Versioning and the Rate Card Library

- Every rate card has effective_from and effective_to
- Multiple rate cards for the same marketplace, channel, and category may exist, but at most one is Active at any moment
- When a marketplace announces a rate change, the Methodology team creates a new rate card with new slabs and an effective_from date; the old card is automatically Superseded as of that date
- The central Rate Card Library is maintained by the Methodology team
- Client sites subscribe to relevant Library entries rather than maintaining their own copies
- Library updates push to subscribed clients with a 7-day review window
- Per-client custom or negotiated rate cards override Library entries cleanly
Three import paths are supported:

- Manual entry — slowest but most reliable for complex cards
- Excel import via a parent-app template — fast for simple slab structures
- AI-assisted PDF extraction — accepts a PDF rate card upload, extracts proposed slabs as draft records, the Methodology team reviews and approves before activation. The AI never activates a rate card autonomously.

## 10.3 The Forecasting Engine

Forecasts are computed at three trigger points:

- **Sales Order on_submit.** Primary trigger — the forecast is the basis for the Net Receivables view, so it must exist as soon as the order does.
- **Sales Invoice creation.** Refresh trigger — if the invoice differs from the order in price, quantity, or shipping address, the forecast is recomputed.
- **Rate Card update.** Bulk refresh trigger — when a Rate Card is activated, a background job walks all unsettled forecasts whose source rate card is now superseded and recomputes them.
A Settlement Forecast record captures, for one Sales Order:

- expected_gross (customer-paid price)
- expected_commission, expected_shipping, expected_fixed_fee, expected_collection_fee, expected_storage_fee
- expected_rto_provision, expected_ad_apportionment, expected_promotion_cost_share
- expected_taxes_tcs, expected_taxes_tds
- expected_net (gross minus all deductions)
- expected_settlement_date (gross date plus the marketplace's published settlement cycle)
- source_rate_cards JSON snapshot of every Rate Card Slab used in the computation — makes the forecast reproducibly traceable even after rate cards change

### 10.3.1 The Net Receivables view

This is the single feature most likely to be the daily reason a CFO logs in. It replaces (for marketplace-channel customers) the standard Accounts Receivable view, which is essentially useless for marketplace business because the customer is the marketplace and the receivable is always 'whatever they decide'.

The view shows, for each open marketplace order:

- Gross order value
- Expected total deductions
- Expected net to be received
- Expected settlement date
- Status (Forecast / Partial Settlement Received / Settled / Settled with Variance / Disputed)
Aggregations: by Marketplace, by week, by category, by SKU. Forward-looking cash forecast: 'between today and Friday, ₹47.3 lakh expected from Amazon, ₹22.1 lakh from Flipkart' — directly usable for working-capital planning.

## 10.4 Variance Analysis

When a Settlement Batch is reconciled:

- Every Settlement Line is matched to a Sales Order's Settlement Forecast (joined on marketplace_order_id)
- The Recon Engine computes variance_amount (actual minus expected, signed), variance_pct (variance over expected, signed), and variance_attribution (which Forecast Line is the dominant contributor)
- These are stored on the Settlement Line itself
Aggregate views are then computable: variance by SKU, by category, by marketplace, by period, by fee_type. Patterns surface — 'Pick-and-Pack fee is consistently ₹12 higher than the rate card predicts on Apparel orders' — that drive both Discrepancy creation and Pricing Diagnostic insights.

A Discrepancy is a single binary event: this order was short-paid by ₹X. A variance is a continuous signal across thousands of orders. Discrepancies catch the egregious cases; variance catches the systemic ones. A 0.5% commission underbilling spread across 50,000 orders is invisible to discrepancy-by-discrepancy analysis but hugely material in aggregate. Both views are needed; the rate-card-driven forecast is what makes the systemic view possible at all.

## 10.5 Pricing Diagnostics

Pricing Diagnostics is a set of reports and dashboards that surface, per SKU and per channel, where margin is going. It is descriptive — what is happening and why — but does not prescribe a new selling price. Prescriptive recommendations are explicitly out of scope for v1; a wrong recommendation acted on by a seller can lose them volume, BuyBox position, or margin in non-recoverable ways.

The per-SKU margin breakdown computes, for every SKU sold in a period:

```
Gross sales            ₹ A
  − Commission           ₹ b1     (% of A, expected vs actual)
  − Shipping             ₹ b2     (expected vs actual)
  − Other marketplace fees ₹ b3
  − Ad spend apportioned   ₹ b4
  − Rebates received       (₹ b5)  (negative = income)
  − TCS / TDS              ₹ b6
  − Returns + RTO impact   ₹ b7
  ───────────────────────────────────
  Net realised             ₹ N
  − COGS (from Item)       ₹ C
  ───────────────────────────────────
  Contribution margin     ₹ M     (with %)
```

This breakdown alone — which no Indian marketplace seller has today as a continuous report — is a non-trivial commercial pitch. Most sellers know their gross sales and their bank deposits; the journey from one to the other is opaque.

Period-over-period margin movement on each SKU is decomposed into causes:

- Rate card change
- Average selling price change (marketplace algorithmic repricing)
- Mix shift
- Fee variance
- Return rate change
- Ad spend change
Each cause's contribution is quantified. The output reads: 'Margin on SKU XYZ-Red-M dropped from 28% to 21% in February. Contributing factors: rate card commission changed from 5% to 8% (-3.0pp), ASP dropped ₹47 likely due to Flipkart algorithmic repricing (-2.1pp), return rate increased from 14% to 19% (-1.4pp), ad spend increased (-0.3pp). Net: -6.8pp explained, -0.2pp residual.'

## 10.6 Marketplace algorithmic repricing

- Flipkart, Amazon, and Meesho all algorithmically reprice listings based on platform competitive dynamics — Mean Selling Price adjustment (Flipkart), BuyBox dynamics (Amazon), category-level normalisation (Meesho)
- The seller often does not realise their effective price has been reduced
- We cannot prevent this, but we can detect it
- From Settlement Lines we recover the actual transacted price per order; from the Sales Order we have the seller's intended price; the delta is the marketplace's repricing
- Surfaced as a per-SKU 'effective price drift' metric
- Sellers historically discover algorithmic repricing weeks late; even passive month-end detection is a meaningful improvement

## 10.7 Trade-offs and limits

- **Forecast accuracy depends on rate card freshness.** If the Rate Card Library has not been updated for a marketplace that changed slabs, forecasts drift. Mitigation: a monitoring report flags Library entries older than 90 days with no review.
- **Per-order ad spend apportionment is approximate.** Marketplaces report ad spend at campaign or account level, not per-order; we allocate proportionally to sales in the period (industry-standard but an approximation).
- **Promotional cost-shares are sometimes opaque.** Myntra in particular charges cost-shares for category-wide promotions in ways the seller did not consent to in advance; these show up in settlement files but cannot always be predicted from rate cards.
- **Returns are forecast-friendly only in aggregate.** The forecast includes an expected_rto_provision based on SKU-level historical return rate × expected RTO charge — a probabilistic provision, not a hard prediction.

# 11. The AI Assistant Layer

## 11.1 Bounded scope

The AI assistant performs first-pass discrepancy classification, claim narrative drafting, and rate-card extraction without waiting for a human to ask. This is the central UX premise that differentiates us from reconciliation tools designed for skilled analysts — the operator does not work the discrepancy queue from scratch; the operator approves or corrects what the AI has already done.

Autonomy stops sharply at the financial line. The AI never:

- Produces a number that ends up in the GL — every figure in a journal entry, payment entry, sales invoice, credit note, or purchase invoice comes from deterministic Python in the recon engine
- Autonomously files a claim or sends a message to a marketplace — drafts are produced for human review; the human submits
- Modifies or deletes records other than its own advisory writes (AI Reconciliation Suggestion and Discrepancy Comment with ai_generated=True)
- Accesses raw bank data, customer PII beyond marketplace-anonymised identifiers, or user credentials
- Bypasses the ERPNext role-based access control — it runs as a scoped user with System Manager rights explicitly denied

## 11.2 What it does

### 11.2.1 Autonomous first-pass discrepancy classification

The AI's primary job. When the recon engine produces a Discrepancy, the AI is invoked automatically. Inputs:

- The discrepancy record
- Related order context
- Related settlement lines
- The active rate card
- Sample of past similar discrepancies classified by humans
Outputs (stored on the Discrepancy record before the operator first opens it):

- Suggested classification
- Confidence score
- Reasoning trace
- Draft claim narrative (for classifications that imply a marketplace claim)
The operator's job changes from 'investigate this' to 'review and approve, or correct'. For high-confidence classifications matching well-established patterns, the operator can configure auto-approval thresholds (e.g., auto-approve weight discrepancy claims under ₹500 on Flipkart Apparel) so the human reviews only the exceptions. Auto-approval is configured per Marketplace Account, defaults to off, and can be revoked at any time.

Over time, recurring patterns the AI classifies confidently are promoted into Discrepancy Rule records by the Methodology team, after which the deterministic engine handles them — moving load from the AI to the cheaper rule engine.

### 11.2.2 Claim narrative drafting

For each Marketplace Claim awaiting filing, the AI drafts the narrative the operator will submit to the marketplace's grievance system. Sources:

- Discrepancy details
- Order history
- Supporting attachments
- Per-marketplace narrative template
Output is editable text; the operator reviews, edits, and submits.

### 11.2.3 Operator question-answering

A chat interface scoped to the client's site, callable from anywhere in the ERPNext desk via a side panel. Example questions:

- 'Why was this Amazon order short-paid by ₹47?'
- 'Which open claims have less than three days to file?'
- 'How much did Flipkart commission cost us last month versus the schedule?'
- 'What's our current TCS receivable, and how much of it is unmatched in GSTR-2B?'
Implementation: an LLM with tool-calling, where the tools are Frappe API endpoints exposed by the parent app — get_discrepancy(id), list_open_claims(marketplace, days_to_deadline), get_settlement_summary(marketplace, period), and so on. The LLM has no direct database access; it can only invoke the documented tools.

### 11.2.4 Pattern detection and rule suggestion

On a weekly cadence, the AI reviews recently classified discrepancies and identifies patterns that recur. It proposes new Discrepancy Rule entries to the Methodology team — for example: 'I notice that 23 short-payments on Flipkart in the last 30 days share the same shape: Pick-and-Pack fee charged at ₹40 instead of expected ₹25 for category Apparel. Suggested rule.' The team reviews, validates, and accepts; the rule moves into the deterministic engine.

### 11.2.5 Rate-card extraction and pricing diagnostics chat

- When the FDE uploads a marketplace or courier rate card as a PDF, the AI extracts the slab structure as draft Rate Card and Slab records — saving hours of manual entry
- The Methodology team reviews and approves before activation
- The chat interface answers pricing diagnostic questions using the variance and margin data ('why did margin on SKU XYZ-Red-M drop in February?'), returning the attribution analysis sourced from the deterministic engine

## 11.3 Architecture

The AI assistant is a separate service, not a Frappe module. Reasons:

- Different dependency footprint (LLM SDKs, vector stores, embeddings) that should not pollute the Frappe app
- Different release cadence — model upgrades and prompt changes happen far more often than ERPNext app releases
- Cleaner audit boundary — every AI action is an HTTP call from a known service principal that can be logged and rate-limited
- Replaceable LLM provider — swapping providers is a service-level change, not a Frappe deployment
Deployment shape:

- A small FastAPI service per environment, multi-tenant by Frappe site
- Each Frappe site has a dedicated API key for the AI service to authenticate as a scoped user
- Read access to recon DocTypes; write access only to AI Reconciliation Suggestion and Discrepancy Comment
- Per-site conversation history maintained in the AI service's own datastore (not in Frappe)
- No cross-tenant prompt context, no cross-tenant retrieval, no cross-tenant model fine-tuning in v1

## 11.4 Cost model and audit

LLM token consumption is the variable cost. We constrain it through:

- Aggressive context shaping — only the relevant discrepancy plus ten exemplar past classifications per call
- Caching of repeated retrievals
- Promoting confident AI patterns into Discrepancy Rules so the deterministic engine handles them next time at zero AI cost
- Per-site monthly token budgets with a hard cut-off
Target: one to two rupees of LLM cost per discrepancy classified, comfortably within the value being recovered.

Every AI action is recorded:

- Every classification suggestion stores the model name, prompt template version, input context size, output, and the user who accepted or overrode
- Every chat interaction stores the question, tool calls made, tool responses, final answer, and the user who asked
- Every claim narrative draft stores the prompt version
This audit trail is what lets a finance team or auditor trust the AI as a tool rather than a black box. If the auditor asks 'why was this discrepancy classified as a weight discrepancy?', the answer is reproducible.

# 12. Parent-App + Child-App Extension Model

## 12.1 The premise

Seventy percent of what every client needs is the same. Thirty percent is genuinely client-specific. We codify the seventy in the parent app (ecommerce_super) and the thirty in a per-client child app (ecommerce_super_<client>) owned by the assigned FDE. The parent must be upgradeable without the FDE having to migrate the child app each time.

## 12.2 The discipline

> If a piece of per-client logic can plausibly be expressed as configuration data in a parent-app DocType, it must be. Code in the child app is the last resort, not the default.

This single discipline produces the test that governs every design decision in the parent app. When the FDE encounters a new client requirement, the question is not 'how do I implement this in the child app' but 'what change to the parent app would let me deliver this without writing code at all'. If the answer is 'no clean configuration path exists', the action is to propose a parent-app change, not to write child-app code immediately.

## 12.3 The extension surface

The parent app exposes eight documented extension points. A child app uses these and only these.

Configuration DocTypes — Settlement Template, Marketplace Rate Card, Discrepancy Rule, Marketplace Account Map, Account Role Map, Warehouse Source-of-Truth Map, Methodology Override. The FDE creates and edits these through the Frappe desk; no code involved. Most client variations live here.

Registry hooks — the parent app exposes registries for pluggable behaviours. A child app registers entries via its own hooks.py:

```
# In ecommerce_super_clientx/hooks.py
ecs_settlement_parsers = {
    "flipkart_v3_clientx_custom": "ecommerce_super_clientx.parsers.flipkart_custom.parse",
}
ecs_discrepancy_classifiers = {
    "clientx_special_category": "ecommerce_super_clientx.classifiers.special.classify",
}
ecs_gl_posting_overrides = {
    ("Flipkart", "Fee-Storage"): "ecommerce_super_clientx.posting.flipkart_storage.post",
}
```

If a child registration is present for a given key, it wins; otherwise the parent default is used. This avoids the override_doctype_class footgun where two apps competing to override the same controller produce undefined behaviour.

Frappe doc_events — for event-driven hooks (validate, on_submit, on_update_after_submit), the child app uses Frappe's standard doc_events. Multiple apps stack additively. Custom Fields and Property Setters via fixtures, prefixed with ecsc_<client>_ to avoid namespace collision. Scheduler events for client-specific scheduled jobs. Whitelisted method overrides for cases where the registry pattern is too narrow. Form scripts for client-side UI customisation. And, only when none of the above suffices, patches.txt for one-shot data fixes and monkey-patching from the child app's __init__.py — every such case is documented as a parent-app design bug.

## 12.4 Versioning and upgrade compatibility

The parent app follows strict semantic versioning. Major version bumps signal breaking changes to the extension surface. Minor versions are additive. Patch versions are bug fixes only. Child apps declare a minimum and maximum compatible parent version. Schema and data changes ship as Frappe patches in patches.txt; ordering follows apps.txt order, so the parent's patches run first. Before a parent-app upgrade is released, it is tested against a synthetic child app that exercises every documented extension point.

## 12.5 Apps.txt order

Expected order on a client site:

```
frappe
erpnext
india_compliance        # hard dependency for GST, e-invoice, GSTR-2B
ecommerce_super         # parent — this product
ecommerce_super_<client> # child — per-client customisation
```

This order ensures erpnext and india_compliance load before our parent (we extend them); our parent loads before the child (the child overrides ours, not vice versa).

## 12.6 What is forbidden in child apps

To preserve upgrade safety, child apps must not:

- Modify parent-app DocType JSON files in place
- Edit parent-app Python files (other than monkey-patching from the child's own code)
- Add fields or property setters to parent-app DocTypes via the child app's fixtures
- Call parent-app private functions (those prefixed with _ or not in the documented extension surface)
- Fork the parent app rather than extending it
The FDE's code review checklist enforces these. A child-app pull request that breaks any of them is rejected and the requirement is taken back to the parent-app backlog.

# 13. Forward-Deployed Engineer Operating Model

## 13.1 Why an FDE

Every serious client of this product will have unique requirements: a custom GL chart of accounts, a quirky settlement file format, a unique fee structure, integration with a legacy system. A pure self-serve product cannot serve these clients. A pure consulting engagement cannot scale beyond a handful. The Forward-Deployed Engineer model is the synthesis: a dedicated technical owner per client, but operating on top of a productised platform with strict discipline that prevents the product from devolving into bespoke code.

## 13.2 What an FDE owns

Each FDE is the technical owner for two to four client deployments. For each, they own:

- The 2-week diagnostic onboarding before contract
- Initial setup: provisioning the Frappe Cloud site, installing the parent app, configuring EasyEcom Settings, EasyEcom Locations, Marketplace Accounts, subscribing to Rate Card Library entries
- Per-client custom Rate Cards for negotiated rates
- Initial population of Discrepancy Rules and Marketplace Account Maps
- The client's child app code (ecommerce_super_<client>)
- The client's production Frappe site upgrades
- Technical support escalations from the client team
- The quarterly business review with the client

## 13.3 What an FDE does not own

- They do not perform daily reconciliation operations — the client's own team operates the system; the FDE's role is to make the team successful, not to do the team's work
- They do not maintain the parent app — that goes through the platform team
- They do not maintain the central Rate Card Library — that's the Methodology team
- They do not invent methodology — that's the Methodology team's job
- They do not customise per-client what should be a parent-app feature
- They do not bypass the extension model under time pressure

## 13.4 The week-by-week shape of an engagement

| Phase | Duration | FDE activities |
| --- | --- | --- |
| Diagnostic (pre-contract) | Week -2 to 0 | Run prospect's 6-month historical data through sandbox. Produce leakage report quantifying unrecovered claims, missed windows, unbilled fees. Present to prospect's CFO with rupee value of opportunity. Convert to signed contract. |
| Discovery | Week 1-2 | Deeper map of client's channels, marketplaces, EasyEcom setup, chart of accounts. Plan production rollout. |
| Foundation setup | Week 2-4 | Provision production Frappe site, install parent app and child app skeleton. Configure EasyEcom and Marketplace Accounts. Subscribe to Rate Card Library entries. First test pulls. |
| Settlement template build | Week 4-6 | Build Settlement Templates (most are pre-existing in the parent app; only client-specific overrides are new). Validate on three months of historical settlement files. |
| Operator training | Week 5-7 (parallel) | Train the client's reconciliation operator on the queue UX, the AI suggestions, the approval workflow. Pair-work for the first 50-100 discrepancies. |
| Recon engine activation | Week 6-8 | Run first end-to-end Order-to-Settlement reconciliation. Triage discrepancies. Build initial Discrepancy Rules for recurring patterns. |
| GL posting go-live | Week 8-10 | Switch on GL posting for matched lines. Reconcile against client's prior month-end. Sign-off. |
| Steady state | Ongoing | Client team operates daily. FDE handles escalations, monthly close support, new marketplace onboarding, quarterly business reviews. |

## 13.5 Sustainability rules

- Every child-app commit goes through review by a platform-team engineer (someone outside the FDE team) before deployment. The reviewer enforces discipline: no unauthorised parent-app modifications, no monkey-patches without documented justification, no duplication of patterns the parent should provide.
- Once a quarter, the platform team and all FDEs review every child app for patterns appearing in two or more clients — anything found in three clients must be promoted to the parent.
- If a child app exceeds 1,000 lines of Python, it triggers a refactoring engagement.
- After eighteen months on a client, the FDE rotates to a new client and is replaced — this prevents single-person bus-factor risk and ensures fresh eyes review historical decisions.

## 13.6 Pricing

The FDE model has a structural cost — one full-time engineer per two to four clients — that must be reflected in pricing. Plausible commercial structure (to be validated):

- One-time onboarding fee covering the first ten weeks
- Annual platform license per Frappe Company under management, scaling with GMV
- Annual FDE retainer separately stated
Detailed pricing is documented in a separate commercial document.

# 14. Phased Rollout

## 14.1 Phasing principles

Each phase ships a recoverable amount of money for the client. Engineering milestones that do not produce client value are not milestones. Each phase proves a piece of the architecture before the next phase loads more on top. Each phase is releasable — we can stop at the end of any phase and still have a working, valuable product.

## 14.2 v0.1 — the tracer bullet

Goal: prove end-to-end that the full forecast → settlement → reconciliation → claim → diagnostics loop can be delivered for a pilot client on EasyEcom, with bidirectional EasyEcom data flow, deterministic forecasting against versioned rate cards, deterministic reconciliation with variance reporting, and minimal AI assistant. v0.1 is intentionally ambitious — full bidirectional EasyEcom plus the rate-card-driven forecasting engine plus pricing diagnostics — because the commercial pitch only fully lands when the seller sees the forward-looking net-receivables view alongside the backward-looking reconciliation.

### 14.2.1 Hard prerequisite — Methodology v0 sign-off

Before any v0.1 code is written, the Methodology team produces and signs off on Methodology v0. The CA's signature is mandatory. Without this sign-off, v0.1 build does not start. The reasoning: code that embodies methodology cannot be built before the methodology is decided, or we will rebuild large parts of it when the methodology arrives. Estimated 8-12 weeks of dedicated work by the Methodology team, in parallel with FDEs running diagnostic engagements with prospective pilot clients (which informs the methodology).

### 14.2.2 v0.1 product scope

v0.1 ships:

- Diagnostic Onboarding sandbox
- Methodology Defaults DocType populated from methodology v0
- Recommended Chart of Accounts shipped as a parent-app fixture, with Account Role Map for client-CA-finalised account names
- Standard Reconciliation Rules library and Standard GL Posting Rules library
- Standard Month-End Close Playbook embodied as scheduled tasks with operator prompts
- Central Rate Card Library with seed entries for Amazon, Flipkart, Myntra
- EasyEcom Connector pull side — orders, returns, inventory, GRN, PO status, master products, locations
- EasyEcom Connector push side — all five flows live, with configurable Sync/Async for Sales Order
- EasyEcom Queue Job DocType and poller; EasyEcom Webhook receiver with idempotent processing
- Settlement Ingestion with Settlement Templates for the client's primary marketplace
- Forecasting Engine and Net Receivables view
- Order-to-Settlement reconciliation live with variance against Forecast (other four reconciliations stubbed)
- Variance Analysis and Pricing Diagnostics
- AI Assistant with autonomous first-pass classification
- Claim Queue with SLA countdown
- GL Posting
- Parent + Child app split
- Push Failures dashboard
One pilot client. One marketplace primary.

### 14.2.3 Success criteria

- Pilot client's monthly close reduced by at least three working days
- Pilot client recovers at least ₹1 lakh in claims in the first three months that they would not have recovered otherwise
- AI first-pass classification accepted (without correction) by the operator at least 70% of the time
- Net Receivables view shows expected settlement amounts within ±2% of actuals over a one-month measurement window
- Junior analyst UX validated: a person with under three months of marketplace reconciliation experience successfully operates the queue without escalation for a full week
- Zero unexplained postings to the GL
- FDE confirms the configuration-over-code discipline held — child app under 200 lines of Python
Estimated effort: 38-46 weeks elapsed with five engineers plus one FDE plus part-time product. The increase from the original 18-24 week estimate reflects the expanded operational surface in v1.2 of the integration spec — Morning Brief, recon-aware alerts, Field Mapping engine, replay tooling, schema drift, SLA tracking, cross-Company ops, time travel. Two release tiers within v0.1: alpha at week 32 (integration mechanics + four must-have operational pieces; pilot-ready), final at week 46 (full ten operational directions; FDE-team-ready).

## 14.3 v0.5 — production for early adopters

Scope:

- All five reconciliations live
- Settlement Templates for at least four marketplaces shipped as parent-app seed data
- Marketplace Fee Schedules pre-populated with current commission slabs, refreshed quarterly
- Discrepancy Rule library covering the top 20 patterns identified across pilot clients
- AI claim narrative drafting and pattern-detection-and-rule-suggestion
- Bank Statement Ingestion
- GSTR-2B and Form 26AS upload paths
- Opt-out cross-client benchmarking with DPDP-compliant contracts
- Documentation: FDE playbook, parent-app extension surface reference, child-app cookbook
Success criteria:

- Three to five clients in production
- Zero parent-app upgrades have broken any child app
- Median monthly close time reduced by 5-7 working days across clients
- Median claim recovery > 2% of GMV
Estimated effort: 3-4 months from v0.1; team scaled to four engineers plus three FDEs.

## 14.4 v1.0 — productised platform

Scope:

- Aggregator topology hardened — multiple Frappe Companies under one EasyEcom account with strict data isolation tested
- Self-service onboarding wizard for non-FDE-led deployments
- Commercial pricing structure finalised and published
- FDE training programme for partner FDEs (system integrators want to do this for their own clients)
- Performance hardening: tested at 1 lakh settlement lines per month per Frappe site
- Frappe Cloud Marketplace listing
- Public roadmap and changelog
Success criteria:

- 8-15 clients
- At least one client onboarded by a partner FDE without our team's direct involvement
Estimated effort: 6-9 months from v0.1.

# 15. Multi-Tenant and Aggregator Model

## 15.1 Two layers

Two distinct multi-tenant scenarios must be supported and they are different in nature.

### 15.1.1 Frappe-site-per-client

The standard Frappe multi-tenancy model:

- Each client has their own Frappe site, their own database, their own ERPNext install
- Each site has its own EasyEcom credentials, its own AI service tenant, its own bank statements
- Clients are entirely isolated
- This is the default deployment for v1

### 15.1.2 Multi-Company within one site

The EasyEcom aggregator case. EasyEcom supports an aggregator topology where one EasyEcom account contains multiple sub-companies. Some of our clients are aggregators — for example, a brand house running five distinct legal entities, each a separate Frappe Company under one ERPNext site, each mapped to a separate EasyEcom company. Within one Frappe site this means:

- Multiple EasyEcom Location records (each tied to a different Frappe Company)
- Multiple Marketplace Account records scoped per Company
- Strict Company-scoping enforced everywhere — Settlement Batches, Marketplace Payouts, Discrepancies, Claims all carry company
- Bank Transactions belong to a specific Company so Payout-to-Bank reconciliation never crosses Company boundaries
- AI assistant queries are Company-scoped

## 15.2 Data isolation enforcement

Frappe's standard Permission Levels and User Permissions handle the bulk of Company isolation. We add:

- All parent-app DocTypes that touch financial data have a mandatory company field
- All recon engine queries filter by company explicitly
- All AI assistant tool calls accept a company parameter and validate the calling user has access
- Audit log entries record the company alongside the user

## 15.3 FDE assignment

In the aggregator case, one FDE may be responsible for one Frappe site that hosts five client Companies. This is heavier than five separate sites because the FDE must context-switch between Companies' different chart-of-accounts, fee structures, and reconciliation policies. We size FDE capacity accordingly: an aggregator site of five Companies counts as roughly two-and-a-half client deployments.

# 16. Security, Compliance, and Audit

## 16.1 Credentials and audit trail

Credential storage:

- EasyEcom API key, email, and password stored as Password fields in EasyEcom Settings, encrypted at rest using Frappe's encryption_key
- LLM provider API keys stored in the AI service's environment, never in the Frappe site
- Marketplace seller-portal credentials never stored by us — the client downloads settlement files manually
- Database credentials, site configs, and infrastructure secrets follow Frappe Cloud practice
- Secrets never logged — the EasyEcom Sync Log explicitly redacts x-api-key, JWT, password, and any field whose name matches token, secret, password, or key
Audit trail:

- Every journal entry posted by the recon engine carries a back-reference to its source
- ecs_recon_run links to the Reconciliation Run; ecs_settlement_batch links to the Settlement Batch
- An auditor can trace any GL entry to the source row in a settlement file, and from there to the original file attachment

## 16.2 Role-based access control

Standard Frappe roles plus four custom roles:

- **Marketplace Operator.** Read all recon DocTypes; write Discrepancy Comments; file Marketplace Claims; upload Settlement Batches; approve AI suggestions.
- **Marketplace Reviewer.** Operator + approve write-offs and tolerance adjustments; run Reconciliation Runs.
- **Accounts Manager.** Reviewer + Reverse-and-Replay; edit Account Role Map and Marketplace Fee Schedules; configure Settlement Templates.
- **AI Assistant.** Service-only role; read recon DocTypes; write only AI Reconciliation Suggestion and Discrepancy Comment with ai_generated=True; explicitly denied System Manager and any role that can post to GL.

## 16.3 PII and data handling

- Customer PII from EasyEcom (buyer name, shipping address, phone) stored in ERPNext's Customer DocType under standard handling
- We do not duplicate it elsewhere
- Marketplace orders that arrive with anonymised buyer identifiers are mapped to a Marketplace Anonymous Customer per (marketplace, marketplace_customer_id) — never to a real Customer record without explicit identification
- AI assistant prompts and responses do not include real customer PII unless the operator has explicitly invoked a tool that returns customer data
- The AI service does not persist PII beyond the chat session
- We use LLM providers that contractually commit to no training on customer data
- We configure no-retention modes where supported

## 16.4 Backups and disaster recovery

- Hosted on Frappe Cloud
- Backups follow Frappe Cloud's default policy: daily database snapshots, weekly site-file snapshots
- Settlement file attachments are part of the site-file backup
- Original uploaded files retained on object storage with versioning
- AI service has its own backup
- AI Reconciliation Suggestion records (the audit trail of AI actions) are persisted in the Frappe site itself and covered by the Frappe Cloud backup
- Disaster recovery target: RTO 8 hours, RPO 24 hours for v1

## 16.5 Compliance posture

- Full conformance to GST and Income Tax requirements via the india_compliance app dependency
- Data residency: all data stored in India by default (Frappe Cloud Mumbai region)
- Cross-border data flow only for LLM provider API calls, where we use providers with India-routed endpoints where available
- SOC 2 and ISO 27001 not in scope for v1; targeted for v1.5

## 16.6 Opt-out cross-client benchmarking and DPDP compliance

The methodology evolves through cross-client pattern observation. Methodology decisions, override patterns, classification accuracy, and recovery rates are aggregated anonymously across clients to identify what works.

- Benchmarking is opt-out by default in v0.5 onwards (v0.1 has no benchmarking)
- Opt-out by default rather than opt-in is a deliberate trade-off: opt-in produces too-small samples; opt-out gets us to a meaningful corpus faster but creates real privacy and contractual obligations
What is shared (anonymised, aggregated):

- Methodology default override values
- Discrepancy classification accuracy rates
- Recovery rates by claim type
- Reconciliation variance distributions
- Rate card error rates
What is never shared:

- Customer names, marketplace seller IDs, specific order details, specific SKU details
- Specific bank UTRs, contact information, GSTIN values
- Financial totals identifiable to a specific client
Aggregation rules:

- No benchmark statistic is published unless it covers at least 5 distinct opted-in clients
- No per-marketplace statistic unless 3+ clients on that marketplace are in the cohort
DPDP Act compliance:

- Written privacy notice is part of the client contract, explicitly disclosing the benchmarking program, what is shared, what is not, and the opt-out mechanism
- Opt-out mechanism is a single setting in the client's EasyEcom Settings — toggling it off stops new contributions immediately and triggers a delete of historical anonymised contributions within 30 days
- Privacy lawyer reviews the contract terms and the technical implementation before the first paid client signs
- The Methodology team designates one person as the DPO function
- Annual external audit of the benchmarking pipeline confirms anonymisation rules are enforced as documented

# 17. Risks and Open Questions

## 17.1 Top risks

Methodology correctness is the single most consequential risk. The other risks are presented in no particular priority order; methodology correctness is the one to spend most defensive effort on, because a flawed methodology contaminates every client deployment simultaneously and is the kind of mistake from which commercial recovery is hardest.

### 17.1.1 Methodology correctness

The entire product premise rests on the Standard Methodology being correct. A flawed recommended chart of accounts, a miscalibrated tolerance default, an incorrect GL posting structure, a wrong interpretation of GST treatment, a flawed reconciliation rule — any of these affects every client simultaneously. The blast radius is total. Unlike a software bug that can be patched in a release, a methodology error damages credibility in a way that is hard to recover from.

Mitigations:

- Methodology v0 must be signed off by the practising CA on the Methodology team before any v0.1 code ships — a hard gate, not a guideline
- Quarterly external review by an independent CA partner who is not on the Methodology team
- Pilot rollout discipline — any methodology version goes to one pilot client first, observed for one full close cycle, before broader rollout
- Override-rate monitoring as the canary: if clients consistently override a default in one direction, the default is wrong
- Rollback discipline: every methodology release has a documented rollback procedure; methodology version pinning is supported per client
- Cross-client benchmarking provides early-warning signals
- Insurance: as the customer base grows, professional indemnity insurance covering methodology error becomes a real consideration from year 2 onwards

### 17.1.2 The AI assistant may scope-creep into doing what the deterministic engine should do

The architecture draws a hard line between the deterministic engine (does all the math, produces every GL number) and the AI assistant (does language and judgement work, never produces a number). The risk is that this line moves under client and schedule pressure — a client says 'our marketplace has a new fee category your rate card doesn't have, can the AI just figure out the amount and post it?', and under deadline the answer becomes yes. Now the books contain numbers no human can explain or reproduce. This risk destroys the product's core promise.

Mitigations:

- Permission-level enforcement — the AI Assistant role explicitly cannot write to Journal Entry, Payment Entry, Sales Invoice, Credit Note, or Purchase Invoice; the database refuses regardless of intent
- Architectural separation — the AI is a separate service; crossing the boundary requires deliberate code changes by the platform team, not a configuration toggle
- Written client agreement at onboarding stating the AI is advisory only
- FDE escalation, not workaround — when a client request implies AI-produced numbers, the FDE escalates it as a parent-app feature gap

### 17.1.3 EasyEcom API instability under load

We have read every endpoint in the v2.1 Postman collection but have not stress-tested any. EasyEcom's rate limits are not publicly documented.

Mitigations:

- Instrument every API call with latency and error metrics from day one
- Build the EasyEcomClient with adaptive back-off
- Engage with EasyEcom support during the pilot to characterise tolerance
- Have a fallback plan for high-volume clients

### 17.1.4 Child-app proliferation outpaces governance

By the time we have ten clients, we have ten child apps, each potentially diverging.

Mitigations:

- The kill-switch (1,000 lines of Python in any child app triggers a refactoring engagement) keeps growth bounded
- Quarterly cross-FDE pattern reviews promote anything found in three clients to the parent
- Platform team has explicit budget allocation for parent-app feature work driven by FDE feedback (target: 30% of platform team's capacity)

### 17.1.5 Competitive response from UniReco or EasyReco extending into ERPNext

Both Unicommerce and EasyEcom are larger companies with mature reconciliation engines and existing seller relationships. Either could ship an ERPNext integration that posts journal entries directly. EasyEcom is a particular concern because they own EasyReco and the EasyEcom WMS.

Mitigations:

- Our window is the time it takes them to recognise and act on the gap
- We win by speed, by methodology depth in a segment they are slowest to address (sellers on EasyEcom + ERPNext + without skilled reconciliation operators)
- Accumulating cross-client patterns improves AI quality faster than they can replicate
- We do not try to out-reconcile UniReco; we win by being the only product that ships a methodology and integrates into ERPNext

### 17.1.6 AI first-pass classification quality

The entire UX premise rests on the AI getting first-pass classification right at least 70% of the time. If accuracy is closer to 40-50%, operators investigate from scratch anyway and our differentiator collapses. We cannot validate this in advance — only with real client data.

Mitigations:

- Instrument AI accuracy as a primary KPI from day one (70% acceptance without correction by week 8 of pilot, 80% by week 12)
- Discrepancy Rule promotion path means recurring patterns move from AI to deterministic rules over time, so accuracy compounds
- AI fallback to plain rule-based classification if confidence is below threshold (operator gets 'unclassified, please investigate' rather than a wrong classification)
- Architectural separation means we can swap LLM provider if quality is insufficient

### 17.1.7 Settlement file format changes by marketplaces

Marketplaces change settlement file formats without notice. A column rename or new sheet breaks every client's Settlement Template overnight.

Mitigations:

- Settlement Template versioning
- Upload validation surfaces format mismatches before they hit the recon engine
- Parent-app monitoring job alerts on Settlement Templates that have not successfully parsed any file in N days
- Public watchlist of marketplace format changes maintained by the platform team

### 17.1.8 Rate Card Library staleness and liability

If the central Library does not refresh promptly, every subscribed client's forecasts and reconciliations silently drift. By maintaining the Library centrally and pushing updates, we also take on quasi-regulatory responsibility for accuracy.

Mitigations:

- A small dedicated team owns the Library
- Entries past last_verified_at + 90 days flag automatically
- AI cross-checks settlement variance patterns and flags potential rate-card-change indicators back to the Library team
- Liability for Library accuracy is best-effort and contractually bounded — clients are responsible for verifying their own subscribed rates against marketplace communications

### 17.1.9 Role-mapping integrity

The methodology's recommended chart of accounts can be fully overridden by the client's CA. The recon engine relies on the role-mapping layer to translate methodology roles to client account names. Inconsistent or sloppy role mapping by FDEs across clients creates client-specific posting bugs and contaminates cross-client benchmarking.

Mitigations:

- Role-map configuration is part of FDE certification
- Methodology team reviews role-map completeness during onboarding sign-off
- Automated check flags role-map entries that look anomalous
- Benchmarking aggregations exclude clients whose role-map quality score falls below a threshold

### 17.1.10 FDE economics

The cost of one FDE per two to four clients implies a per-client annualised cost in the high-single-digit lakhs of rupees. Multi-channel sellers in our target band can afford this if leakage recovery is real, but if recovery is closer to half a percent of GMV than two percent, the ROI math is thin for clients at the smaller end.

Mitigations:

- Instrument leakage-recovery as a tracked KPI from v0.1
- Publish anonymised recovery numbers from pilot clients before broader sales
- Size client targeting (minimum GMV thresholds) based on observed recovery rates

### 17.1.11 LLM cost per discrepancy

As discrepancy volume grows, AI costs scale linearly. At low recovery values per discrepancy, unit economics break.

Mitigations:

- Rule-promotion path moves load from AI to the deterministic engine
- Per-site monthly token budgets prevent runaway spend
- We measure AI cost per recovered rupee as a primary internal metric

## 17.2 Open questions

Several decisions are deferred for later resolution:

- LLM provider choice — settled with a head-to-head bake-off in the first two weeks of v0.1 build (candidates: Anthropic Claude, OpenAI GPT-4 family, Google Gemini, self-hosted Llama)
- Source visibility model — commercial confirmed; choice between fully closed-source and source-available (BSL or equivalent) deferred for separate discussion
- Multi-currency support — deferred to v2.0

# 18. Glossary, References, and Document Metadata

## 18.1 Glossary

- **Aggregator** — multi-tenant setup where one EasyEcom account contains multiple sub-companies, each a separate legal entity.
- **Account Role Map** — per-Company DocType that translates methodology role names to the client's actual GL account names.
- **BuyBox** — on Amazon, the featured offer that wins the default 'Buy' button when multiple sellers list the same product.
- **Child app** — a Frappe app installed on top of the parent app containing per-client customisations.
- **Claim window** — the time period during which a marketplace will accept a claim.
- **Diagnostic Onboarding** — a 2-week pre-contract engagement in which the FDE runs the prospect's last 6 months of historical data through a sandbox and produces a leakage report.
- **Discrepancy** — a detected mismatch between expected and actual financial values.
- **DocType** — Frappe Framework's data model unit.
- **DPDP Act** — India's Digital Personal Data Protection Act 2023.
- **DSL** — Domain-specific language.
- **EasyReco** — EasyEcom's reconciliation product.
- **FDE** — Forward-Deployed Engineer.
- **First-pass classification** — the AI's autonomous initial classification of a Discrepancy.
- **Forecast** — expected deductions and net settlement computed at Sales Order submission.
- **Frappe** — the web framework underlying ERPNext.
- **GMV** — Gross Merchandise Value.
- **GRN** — Goods Receipt Note.
- **GSTR-2B** — Auto-drafted GST input tax credit statement.
- **ITC** — Input Tax Credit under GST.
- **JWT** — JSON Web Token.
- **Mean Selling Price** — Flipkart's algorithmic repricing metric.
- **Methodology** — the opinionated, software-embodied way of doing marketplace reconciliation that this product ships.
- **Methodology Override** — any client-specific deviation from the Standard Methodology defaults.
- **Methodology Team** — three-person team (CA + marketplace ops specialist + Frappe consultant) that develops, maintains, and evolves the Methodology.
- **Net Receivables** — forward-looking view of expected net settlement amounts.
- **Parent app** — ecommerce_super, the core platform.
- **Prescriptive pricing** — recommendations of the form 'set this SKU's price to ₹X' — out of scope for v1.
- **Rate Card** — versioned schedule of fees a marketplace or courier charges.
- **Rate Card Library** — centrally-maintained collection of current marketplace and courier rate cards.
- **RTO** — Return to Origin.
- **SLA** — Service Level Agreement.
- **SoT** — Source of Truth.
- **SPF** — Seller Protection Fund.
- **TCS** — Tax Collected at Source under Section 52 CGST, deducted by e-commerce operators at 0.5%.
- **TDS** — Tax Deducted at Source under Section 194-O Income Tax, deducted by e-commerce operators at 0.1%.
- **UniReco** — Unicommerce's reconciliation product.
- **UTR** — Unique Transaction Reference.
- **Variance** — signed delta between actual settlement amount and forecast amount.

## 18.2 Referenced documents

- EasyEcom V2.1 Postman Collection (parsed in full and inventoried during research)
- Frappe Framework documentation — frappe.io/docs
- ERPNext India Compliance app — github.com/resilient-tech/india-compliance
- Frappe Ecommerce Integrations — github.com/frappe/ecommerce_integrations (reference for connector patterns)
- EasyEcom official API documentation — api-docs.easyecom.io

## 18.3 Document metadata

Version 2.0. Last updated April 2026. Status: for internal review and prospect-facing use.

