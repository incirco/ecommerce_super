# EasyEcom Integration Specification

**Version:** 1.2 — Working Document
**Date:** May 2026
**Companion to:** ERPNext E-commerce Super-App PRD
**Audience:** FDEs, Platform Engineers, Methodology Team, Claude Code

---

# Part I — Orientation

*Why this integration exists and the principles that govern it.*

# 1. Introduction

## 1.1 What this document is

This is the EasyEcom Integration Specification, a companion to the ERPNext E-commerce Super-App PRD. It specifies, in engineering-grade detail, how the parent app (ecommerce_super) integrates bidirectionally with EasyEcom across all ten operational flows that the product needs to function. It is the authoritative reference for FDEs, platform engineers, and the methodology team when implementing or evolving the integration.

It is not a marketing document. It is not a product overview. It assumes the reader has read the PRD and is now asking the question: precisely how does this integration work, end-to-end, in every flow, including failure cases?

## 1.2 Why we are building this rather than depending on erpnext_easyecom

The official Incirco-managed app erpnext_easyecom exists and covers the same operational surface. We have evaluated depending on it and chosen to build our own integration. The reasons:

- The integration is the foundation of the recon engine. Every flow must produce data shaped exactly the way the recon engine needs to consume it — correlation keys, custom fields, event hooks, idempotency tokens. Living downstream of someone else's release cadence and design decisions is a structural risk on the data the books-of-record depends on.
- Multi-company topology is a first-class concern for our target market. A single EasyEcom account routinely spans multiple locations, each carrying a company that resolves to a distinct Frappe Company. erpnext_easyecom treats EasyEcom Settings as Single, which fundamentally constrains the multi-Company case our customers operate in.
- Conflict resolution rules for bidirectional master sync, and the field-level ownership matrix that powers them, are part of our methodology — they need to be ours, versioned with the methodology, and changeable when the methodology evolves.
- Custom fields needed for reconciliation back-references (ecs_recon_run, ecs_settlement_batch, ecs_marketplace_order_id) need to be wired into every operational document the integration produces. Adding these via a child app on top of someone else's app is fragile.
Building our own carries cost. We honestly acknowledge:

- Engineering scope expands materially. Realistic v0.1 with all ten flows plus the recon engine plus the AI assistant plus methodology v0 is 28-32 weeks with five engineers, not the 18-24 weeks originally quoted in the PRD. The PRD's Section 16 will be updated to reflect this. v16's better defaults (Caffeine perf, expanded Stock Reservation, native Item-level stock accounting) trim 1-2 weeks off the integration build but do not materially change the outer envelope.
- We bear ongoing maintenance burden as EasyEcom evolves their API surface.
- Where erpnext_easyecom has already caught and handled an edge case across hundreds of deployments, we will rediscover it the hard way.
The conclusion: we accept these costs because the integration is too central to the product's correctness to be downstream of an external dependency. We will, however, study erpnext_easyecom carefully as a reference implementation and acknowledge it as such where its patterns are sound.

## 1.3 Audience

- Forward-Deployed Engineers configuring the integration for a specific client deployment
- Platform engineers building, testing, and maintaining the parent-app integration code
- Methodology team members validating that integration outputs feed the recon engine correctly
- Frappe consultants on the wider ecosystem who want to understand the integration model

## 1.4 Inherited assumptions from the PRD

- Hosting on Frappe Cloud (Mumbai region for data residency)
- ERPNext v16 only — this is a hard floor, not a recommendation. The integration is built for v16 primitives and does not support backwards compatibility with v15 or earlier.
- v16-specific features the integration relies on: expanded Stock Reservation across Sales Order / Pick List / Work Order / Subcontracting flows; Frappe Caffeine cache for sync-job performance; Item-level stock accounting (separate GL accounts per Item or Item Group); refactored TDS for procurement flows; Consolidated Trial Balance for multi-company reporting; Master Production Schedule and MRP views (forward compatibility for clients with manufacturing); Subcontracted Sales Order and Subcontracting Inward Order documents; Landed Cost Voucher applied to Stock Entries and subcontracting receipts; Automatic Closing Stock Posting for periodic-accounting clients.
- india_compliance app installed as a hard dependency (GST, e-invoicing, GSTR-2B parsing)
- Methodology v0 signed off by the Methodology team's CA before integration code ships to a paying client
- Target client profile: SME multi-channel sellers, ₹5-200 crore GMV, 3+ marketplaces, EasyEcom as OMS
- FDE-led deployment model — every integration goes live with an FDE configuring it, not via self-serve

## 1.5 Document conventions

- DocType names appear capitalised mid-sentence: Sales Order, Purchase Receipt, EasyEcom Queue Job
- Field names appear in monospace: marketplace_order_id, ecs_inventory_master
- EasyEcom API endpoints appear with the leading slash: /orders/V2/getAllOrders
- HTTP verbs in CAPS: GET, POST
- Custom field prefix is ecs_ (parent app) and ecsc_<client>_ (child app)
- All times are IST unless otherwise stated
- All amounts are in INR unless otherwise stated

# 2. Architectural Principles

The non-negotiables that govern every flow in this specification. When a design decision is unclear in a specific flow, refer to these principles for adjudication.

## 2.1 Books-of-record vs operations-of-record

- **ERPNext is the books-of-record.** Every transaction with financial impact (Purchase Receipt, Sales Invoice, Credit Note, Stock Entry, Journal Entry, Payment Entry) lives in ERPNext as the authoritative copy. The auditor reads ERPNext.
- **EasyEcom is the operations-of-record.** Operational state — order status through fulfilment, GRN process, manifest creation, marketplace channel state — lives in EasyEcom as the authoritative copy.
- When the two diverge, ERPNext wins for financial questions, EasyEcom wins for operational questions. The integration's job is to keep them aligned within tolerable lag, and to surface a Discrepancy when they cannot be aligned.

## 2.2 Idempotency is mandatory

- Every API call from ERPNext to EasyEcom carries a deterministic idempotency key (typically the ERPNext document name, e.g., SO-2026-00123)
- Every webhook from EasyEcom to ERPNext is deduplicated against an EasyEcom Webhook Event record using a composite key of event_type + payload hash
- Every poll-based ingest checks for existing records before insert, never duplicates
- A flow that has been run partially must be safely re-runnable to completion without producing duplicate financial documents

## 2.3 Replay is mandatory

- Every flow has a documented FDE-runnable replay procedure
- Replay produces the same outcome as the original run, or surfaces a clear reason it cannot
- Reverse-and-replay (the recon-engine pattern from the PRD) extends to integration outcomes — a Purchase Receipt created from a faulty GRN webhook can be reversed and the GRN re-pulled

## 2.4 ERPNext submission is never blocked by EasyEcom availability

The default consistency posture is asynchronous-eventually-consistent, not synchronous-immediate-consistent. Specifically:

- Sales Order, Purchase Order, Item, Customer, Supplier, and Stock Entry submissions in ERPNext complete regardless of EasyEcom availability
- Push to EasyEcom is enqueued via EasyEcom Queue Job and retried with back-off
- Persistent push failures alert the FDE via the Push Failures dashboard
- The ERPNext UI never hangs waiting on EasyEcom
One configurable exception: Sales Order push for B2B orders can be set to synchronous mode per Marketplace Account, in which case on_submit waits for EasyEcom acknowledgement and rejects the submission on EasyEcom error. This is a deliberate per-client choice with a documented trade-off.

## 2.5 Multi-company is first-class

- The credential and sync boundary is the **EasyEcom Account** (one per client deployment), not the Company. One account holds one credential set; JWTs are minted per location_key under it.
- Company identity is **resolved through the location**: each EasyEcom Location carries the Frappe Company it maps to. The relationship is many-to-one — several locations may resolve to the same Company. The integration never assumes location equals company.
- Account-level configuration (credentials, endpoint, sync window, sync tuning, webhook auth) lives on the EasyEcom Account. Company-level configuration (alert recipients, assigned FDE) lives on a per-Company settings record.
- Every operational DocType carries a company field — EasyEcom Sync Record, EasyEcom API Call, EasyEcom Sync Cursor, EasyEcom Queue Job, EasyEcom Webhook Event — populated by resolving the originating location to its Company. It is mandatory for all entity-sync work; the sole exception is foundational calls (token, location discovery, connection test — Section 7.7), whose API Call rows are account-scoped and leave company blank.
- EasyEcom Account and per-Company settings are normal DocTypes keyed on their natural identifiers (the account, and the Company respectively).
- Every API call carries Account context and (where operational) Company context; every webhook is routed to the correct Company on receipt by resolving its location.
- Permission rules ensure a user with access to Company A cannot see Company B's sync logs or sync state.
- Item, Customer, Supplier masters are account-global: synced once via the primary location and shared across all Frappe Companies following ERPNext's standard multi-company semantics (Item Defaults table, Customer/Supplier Default per Company), not the integration's invention.

## 2.6 Source-of-truth is configurable, not hardcoded

Different clients will have different operational realities. We do not assume EasyEcom always owns inventory, or ERPNext always owns customers. The Warehouse Source-of-Truth Map (per Company) determines, for each Frappe Warehouse:

- Which system is the inventory master (ERPNext or EasyEcom)
- Which system originates Purchase Receipts (ERPNext direct, or EasyEcom GRN flow)
- Which system originates Stock Adjustments
- Whether Stock Reservation Entries from B2B orders are mirrored, originated, or ignored
Master-level direction-of-truth (Item, Customer, Supplier, Tax Category) is similarly configurable, with field-level ownership rules documented in Section 8.

## 2.7 No silent data divergence

If the integration cannot reconcile a state difference between systems, it does not silently pick a winner. Instead it produces a visible artefact:

- An Integration Discrepancy record (distinct from the recon-engine Discrepancy, but in the same UX queue)
- With a documented severity (Info, Warning, Error, Critical)
- Routed to the FDE for resolution, with suggested actions
This is what makes the integration suitable for financial workloads. A silent overwrite of one side's data by the other is a class of bug we refuse to allow.

## 2.8 Audit trail is mandatory

- Every API call (in either direction) is logged in EasyEcom API Call with timestamp, endpoint, request_payload (credentials redacted), response_status, response_body, latency_ms, calling user (where applicable)
- Every webhook received is recorded in EasyEcom Webhook Event before processing
- Every document created or modified by the integration carries back-references: ecs_easyecom_source (the EasyEcom record ID) and ecs_api_call (the API Call that produced it)
- API Call and Webhook Event records are retained for 90 days minimum (configurable up to 7 years for high-compliance clients)

## 2.9 Failure is expected and recoverable

The integration is built on the assumption that any individual API call, webhook, or background job will eventually fail. Recovery is the default behaviour, not an afterthought:

- Exponential back-off with jitter on 429, 5xx, and connection errors
- Maximum retry counts per flow with documented escalation thresholds
- Persistent failures escalate to FDE via dashboard, not silently dropped
- Every flow has a documented manual-recovery procedure for the case where automatic retry exhausts

# Part II — The Foundation

*The connection, the records, the translation engine, and the contract that every integration obeys. Built first, in this order, before any business flow.*

# 3. Authentication and Connection Model

## 3.1 The topology: Account, Locations, Companies

The integration's connection model has three entities and one resolution rule.

**EasyEcom Account** — the credential and sync boundary. One per client deployment. It holds the single set of EasyEcom credentials (api_key, email, password) and the account-wide operational configuration (endpoint, sync window, sync tuning, webhook auth). JWTs are minted per location_key under this one account.

**EasyEcom Location** — one record per location_key the account exposes. Each location is flagged as primary and/or operational, and each operational location resolves to a Frappe Company. EasyEcom issues JWTs scoped to a location_key, so the per-location JWT cache lives here.

**Frappe Company** — the legal entity. The integration never talks to EasyEcom "as a Company"; it talks as the Account, against a location, and resolves that location to a Company for the purpose of posting documents.

The resolution rule is always `location → frappe_company`, and it is **many-to-one**: several locations may resolve to the same Company. The number of distinct Companies a deployment spans is simply a count — one for a single-entity client, several for a multi-entity client — with no structural difference in how the integration is wired. The integration must never assume a location corresponds one-to-one with a Company, and must never derive Company identity from anything other than the location's resolved `frappe_company`.

### 3.1.1 The primary location and masters

Every EasyEcom account has exactly one **primary** location. By EasyEcom convention the primary location is the main/head location where account-level masters (products, users) and access are managed. The primary location may itself be a working warehouse, or it may be purely a head-office location with no physical fulfilment; child (non-primary) locations are generally the working warehouses and store locations. This is exactly why `is_primary` and `is_operational` are independent flags (Section 3.1.2) — being the master location says nothing about whether the location also fulfils orders.

Master sync (Items, Customers, Suppliers, Tax Categories) runs against the primary location and the resulting masters are account-global — shared across every Frappe Company via ERPNext's native cross-company master sharing (Section 8). There is no per-(Company, location) master mapping.

The primary location's own company value (the company EasyEcom records against it) is **recorded for reference but not used operationally** unless the primary location is also flagged operational (see below). When the primary location is master-only, the integration creates no operational Company binding from it.

### 3.1.2 Operational locations

A location is **operational** when the integration runs order, GRN, return, settlement, and stock flows against it and posts the resulting documents into a Frappe Company. `is_primary` and `is_operational` are independent flags; all four combinations are valid:

- primary, not operational — masters only; company value ignored operationally
- primary, operational — holds masters and is also transacted on, resolving to a Company like any other operational location
- not primary, operational — an ordinary company-bearing location
- not primary, not operational — a location that exists in EasyEcom but is not relevant to ERPNext; the integration ignores it (see 3.1.3)

### 3.1.3 Partial mapping is the normal steady state

The location ↔ warehouse relationship is not total in either direction, and this is expected, not an error condition:

- ERPNext may hold warehouses that map to no EasyEcom location (internal-only warehouses — scrap, WIP, quarantine). The integration never pushes or pulls against them.
- EasyEcom may expose locations that map to no Frappe warehouse and resolve to no Company (locations the client uses for non-ERPNext purposes). The integration ignores them.

The integration acts only on the explicitly-mapped intersection — the set of (EasyEcom location ↔ Frappe warehouse) pairs the FDE has configured. Validation must not error on the existence of unmapped warehouses or unmapped locations; both are a normal part of every deployment.

## 3.2 The EasyEcom Control Panel

The FDE configures and operates the integration from a single landing surface — the **EasyEcom Control Panel** (a Frappe Workspace). The Control Panel is a navigation and status front door, not a separate data store: the underlying DocTypes (EasyEcom Account, EasyEcom Location, per-Company settings, Field Mapping, and so on) hold the data; the Control Panel aggregates access to them so the FDE has one place to start rather than a set of pages to remember.

The Control Panel surfaces, in one place:

- **Account configuration** — the EasyEcom Account record (credentials, endpoint, connection health, sync window, sync tuning, webhook auth). One per deployment.
- **Locations & mapping** — the list of EasyEcom Locations with their primary/operational flags and resolved Company, plus the Source-of-Truth Map (location ↔ warehouse). The unmapped-on-both-sides reality is shown plainly so the FDE can see what is and isn't in scope.
- **Per-Company configuration** — the per-Company settings records (alert recipients, assigned FDE, and any company-specific routing).
- **Marketplace Accounts** — the per-Company marketplace seller configurations.
- **Field Mappings, Error Translation, SLA Budgets** — links into those libraries (Sections 5, 27, 23).
- **Operational launchpad** — Sync Now, connection status, recent failures, and the health surface, linking into the operational surface in Section 17.

A header strip is present on both the Account record and each per-Company settings record. On the Account it shows the account-wide kill-switch, the live connection status, the most recent successful sync timestamp, and the environment badge. On a per-Company settings record it shows the per-Company enable toggle and that Company's connection/health rollup. This gives the FDE both an account-wide master switch and per-Company control.

## 3.3 EasyEcom Account (account-level configuration)

One record per client deployment. Holds the credentials and the operational configuration that is genuinely one-per-account. Organised into collapsible sections so the FDE navigates to the area that needs attention without scrolling through unrelated fields.

### 3.3.1 Header strip (always visible)

| Field | Type | Notes |
| --- | --- | --- |
| account_name | Data | Human-readable label for this deployment's EasyEcom account |
| enabled | Check | Account-wide kill-switch. When unchecked, no syncs of any kind run for any location |
| connection_status | Select (read-only) | Live indicator: Connected / Degraded / Down / Disabled. Computed every 60 s from connection health |
| last_successful_sync_at | Datetime (read-only) | The most recent successful API call against any endpoint, any location |
| environment_badge | Select | Sandbox / Production. Drives a coloured banner across every related screen |

The Sync Now button opens a dropdown with per-master triggers: Sync Items, Sync Customers, Sync Suppliers, Sync Tax Categories, Sync All Masters, Pull Orders Now, Pull GRNs Now, Pull Returns Now. Each opens a scope-selection modal (described in Section 17.10).

### 3.3.2 Setup section

The credentials required to authenticate with EasyEcom. One credential set for the whole account. This is the first thing the FDE configures during onboarding.

| Field | Type | Notes |
| --- | --- | --- |
| api_endpoint | Data | EasyEcom base URL. Production: https://api.easyecom.io. Sandbox URL differs |
| x_api_key | Password | Account-level API key, encrypted at rest using Frappe encryption_key. Generated only from the client's Primary Seller Account (Account Settings → Change credentials → Generate X-API-KEY). Does not auto-expire; regenerating it invalidates the previous key instantly, so a rotation must be applied here at the same time it is regenerated on EasyEcom |
| email | Password | EasyEcom account email (must be a user with multi-location access in the primary account). Treated as a credential: encrypted, write-only, never readable back (Section 3.7) |
| password | Password | EasyEcom account password, encrypted |
| rate_limit_tier | Select | The client's current EasyEcom rate-limit tier: Default / Bronze / Silver / Gold / Diamond. Mandatory, no preset default — the FDE records the tier EasyEcom has actually assigned to this api_key. The integration derives its throttle and daily-quota ceiling from this tier (Section 3.10). A newly generated key sits in Default tier until EasyEcom upgrades it after their UAT review |
| default_location_key | Link → EasyEcom Location | Used when an operation is location-agnostic (typically the primary location for master operations) |
| test_connection_action | Button | Triggers a /access/token call and reports result inline. Available before save |

Every authenticated EasyEcom call carries two mandatory headers: `x-api-key: {api_key}` (identifies the client; account-level) and `Authorization: Bearer {jwt}` (validates the session; per-location JWT). Missing either header yields HTTP 401. The x-api-key is required even on the token-acquisition call.

### 3.3.3 Sync Window section

Allowed time windows for background sync activity. Account-wide. Lets clients restrict heavy sync to off-peak hours. All times in the deployment's default timezone.

| Field | Type | Notes |
| --- | --- | --- |
| sync_window_enabled | Check | If unchecked, syncs run 24x7 |
| sync_window_start | Time | Daily start (e.g., 22:00) |
| sync_window_end | Time | Daily end (e.g., 06:00). May cross midnight |
| sync_window_weekends_only | Check | Restrict heavy syncs to Sat/Sun only |
| pause_until | Datetime | Manual pause — overrides the window. Useful for planned EE maintenance |
| window_exemptions | Table of (job_type, always_run) | Critical jobs that bypass the window — typically webhook-triggered |

### 3.3.4 Sync Tuning section

Per-resource cadences and concurrency, account-wide. Defaults suit a 5,000-orders/month client; FDEs adjust per deployment to reflect actual volume and EasyEcom rate-limit headroom.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| poll_interval_orders_min | Int (minutes) | 5 | B2C order pull cadence |
| poll_interval_returns_min | Int (minutes) | 15 | Return-receipt pull cadence |
| poll_interval_grn_min | Int (minutes) | 30 | GRN pull cadence |
| poll_interval_inventory_min | Int (minutes) | 60 | Stock-level pull cadence |
| poll_interval_po_status_min | Int (minutes) | 60 | PO status pull cadence |
| poll_interval_master_products_hours | Int (hours) | 24 | Master product pull cadence |
| poll_interval_locations_hours | Int (hours) | 24 | Location pull cadence |
| max_throughput_per_sec | Int | tier | Outbound rate cap per location_key. Defaults to the request-rate of the account's rate_limit_tier (Section 3.10); an FDE may set it lower but not above the tier ceiling. The integration clamps to the tier regardless |
| max_concurrent_workers | Int | 4 | Max parallel sync workers (account-wide; per-Company sub-limits in Section 6.3.7) |
| batch_size_items_push | Int | 50 | Items per push API call |
| batch_size_orders_pull | Int | 100 | Orders per pull API call |
| sync_enabled_orders | Check | True | Per-resource toggle for incremental rollout |
| sync_enabled_inventory | Check | True | ... |
| sync_enabled_returns | Check | True | ... |
| sync_enabled_grn | Check | True | ... |
| sync_enabled_master_products | Check | True | ... |
| push_so_mode | Select: Async / Sync | Async | Sales Order push mode |
| push_so_block_on_error | Check | False | If sync mode: reject on_submit on EE error |

### 3.3.5 Inbound Webhook Auth section

Configuration for inbound webhook handling. Account-wide. Auth is mandatory; we do not accept unauthenticated webhooks under any circumstance.

| Field | Type | Notes |
| --- | --- | --- |
| webhook_enabled | Check | If unchecked, all inbound webhooks return 503; we operate in poll-only mode |
| webhook_token | Password | Shared bearer token issued to EasyEcom; the receiver accepts it in either an `Access-token` or `Authorization: Bearer` header and compares in constant time |
| webhook_allowed_ips | Long Text | Optional CIDR allowlist. Requests from outside the list are rejected at the framework level |
| webhook_max_age_seconds | Int | If EasyEcom includes a timestamp header on the webhook, reject deliveries older than this (default 300) as stale. Best-effort replay mitigation; skipped if no timestamp header is present |
| webhook_dedup_window_minutes | Int | How long to remember (event_type + payload_hash) for dedup (default 60) |
| webhook_endpoint_url_display | Read-only | Shows the URL to register in EasyEcom. The receiver resolves the originating location to its Company on receipt (Section 3.6) |

### 3.3.6 GRN / Inward Policy section

Business rules for GRN-to-Purchase-Receipt translation. These default values feed Section 9 (Buying flow) preconditions. Defaults are account-wide; warehouse links resolve per location/Company.

| Field | Type | Notes |
| --- | --- | --- |
| default_rejected_warehouse | Link → Warehouse | Where rejected_qty from GRN lines posts. Resolved per receiving Company |
| default_in_transit_warehouse | Link → Warehouse | Used in transfer flows; see Section 10 |
| allow_over_receipt_pct | Percent | Tolerance for cumulative received_qty exceeding PO qty (default 0%) |
| allow_under_receipt_pct | Percent | Tolerance for short-receipt before alerting (default 0%) |
| mandatory_batch_for_groups | Table of Item Group | Item Groups that always require batch_no; integration enforces independently of Item.has_batch_no |
| mandatory_serial_for_groups | Table of Item Group | Same for serials |
| mandatory_expiry_for_groups | Table of Item Group | Same for expiry |
| tax_variance_tolerance_pct | Percent | EE-side tax vs ERPNext-derived tax tolerance before raising Discrepancy (default 1%) |
| lost_in_transit_threshold_days | Int | Days after dispatch with no GRN before raising Discrepancy (default 30) |

Where a default warehouse must differ by Company (because each Company has its own warehouse tree), the account-level value is the fallback and the per-Company settings record (Section 3.5) may override it.

## 3.4 EasyEcom Location (per-location record and JWT cache)

One record per location_key the account exposes. Carries the primary/operational flags, the Company resolution, the warehouse mapping, the per-location JWT cache, and the per-location pull cursors.

| Field | Type | Notes |
| --- | --- | --- |
| location_key | Data | EasyEcom-issued identifier; unique within the account; the primary key for this record. It is the warehouse/store-level unique identifier found on EasyEcom's Location Master page (Account Settings → Company Information → Seller ID) in the primary account. EasyEcom's location_key and warehouse ID are the same-level identifier; the integration uses location_key everywhere for its standard usage across all APIs |
| location_name | Data | Human-readable EasyEcom location name |
| is_primary | Check | Exactly one location per account has this set. Marks the master/user-management location |
| is_operational | Check | Whether the integration runs operational flows against this location and posts into a Company. Independent of is_primary |
| is_wms_location | Check | Whether this location runs EasyEcom's full WMS plan (PO, GRN, cycle counting, shelving, putaway) versus the OMS-only Non-WMS plan (order management with manually-maintained inventory). The inbound buying/GRN flow (Section 9) applies only to WMS locations; a Non-WMS location has no GRN/PO flow and its stock is not driven by EE GRN events |
| serialization_enabled | Check | Whether EasyEcom serialization is enabled for this seller-location. When set, GRN quantities are pushed per individual serial rather than as an aggregate quantity (Section 9) |
| frappe_company | Link → Company | The Frappe Company this location resolves to. Set if and only if is_operational. Nullable. NOT unique — several locations may resolve to the same Company |
| ee_company_value | Data | The company value EasyEcom records against this location. Recorded for reference; never used operationally |
| mapped_warehouse | Link → Warehouse | The Frappe Warehouse this location represents (within frappe_company). Blank for non-operational locations |
| jwt_token | Long Text | Cached JWT for this location_key, encrypted at rest |
| jwt_acquired_at | Datetime | When the JWT was acquired |
| jwt_expires_at | Datetime | Token expiry. EasyEcom JWTs are valid for 90 days; a daily scheduled job renews the token at 85 days of age (Section 3.6), and the client also re-authenticates on any 401 |
| enabled | Check | Per-location kill-switch |
| last_pull_orders | Datetime | Cursor — last successful order pull for this location |
| last_pull_returns | Datetime | Cursor — last successful return pull for this location |
| last_pull_grn | Datetime | Cursor — last successful GRN pull for this location |

Validation rules:

- Exactly one location per account has `is_primary` set.
- `frappe_company` presence is governed by the location's workflow state (§8.4.1): it must be set in the mapped states (Mapped but not Live, Live) and empty in the unmapped states (To Map, Skipped). Because `is_operational` is itself workflow-derived (true only in Live), this subsumes the older "mandatory iff is_operational" phrasing.
- `frappe_company` is deliberately non-unique; the integration relies on many-to-one location→Company resolution.
- A location with neither `is_primary` nor `is_operational` set is inert — the integration neither syncs masters against it nor runs operational flows on it. It exists only to record that EasyEcom exposes the location.
- A single EasyEcom user credential can mint JWTs across multiple locations only if that user was created in the primary account with multi-location access. The account's credentials must have this access for the integration to span locations.

## 3.5 Per-Company settings record

The configuration that genuinely varies by Frappe Company lives in a per-Company settings record (one per operational Company). This is deliberately thin — most configuration is account-level (Section 3.3).

### 3.5.1 Header strip

| Field | Type | Notes |
| --- | --- | --- |
| company | Link → Company | Primary key, mandatory, immutable post-create |
| enabled | Check | Per-Company kill-switch. When unchecked, operational flows for this Company pause; the account and other Companies are unaffected |
| connection_status | Select (read-only) | Rollup of connection health across this Company's operational locations |
| last_successful_sync_at | Datetime (read-only) | Most recent successful operation posting into this Company |

### 3.5.2 Alerts section

Severity thresholds and notification routing for this Company. The full notification framework is in Section 18.

| Field | Type | Notes |
| --- | --- | --- |
| alert_recipients_critical | Table of (User, channel) | Who gets paged on Critical alerts. Channels: Notification, Email, Slack |
| alert_recipients_error | Table of (User, channel) | Who gets Error alerts |
| alert_recipients_warning | Table of (User, channel) | Who gets Warning alerts (typically only the FDE) |
| queue_depth_warning_threshold | Int | Queue Job depth that triggers Warning (default 500) |
| queue_depth_critical_threshold | Int | Threshold that triggers Critical (default 1000) |
| api_error_rate_warning_pct | Percent | API error rate over a 15-min window that triggers Warning (default 5%) |
| api_error_rate_critical_pct | Percent | Same for Critical (default 20%) |
| webhook_gap_warning_minutes | Int | Minutes since last webhook that triggers Warning if expected (default 60) |
| sync_lag_warning_minutes | Int | Cursor lag that triggers Warning (default 30) |
| daily_digest_enabled | Check | Send daily integration health digest |
| daily_digest_time | Time | When to send the digest (default 08:00) |
| weekly_summary_enabled | Check | Send weekly integration summary report |

### 3.5.3 Notifications section

Channel-specific configuration for outbound notifications for this Company. The Alerts section decides who and when; this section configures how.

| Field | Type | Notes |
| --- | --- | --- |
| email_template_critical | Link → Email Template | Override the default email template for Critical alerts |
| email_template_error | Link → Email Template |  |
| email_template_warning | Link → Email Template |  |
| slack_webhook_url | Password | Optional. If populated, alerts post to this channel |
| slack_channel_critical | Data | e.g., #ee-prod-critical |
| slack_channel_error | Data |  |
| banner_show_to_all_users | Check | If checked, Critical alerts surface as a desk-wide banner, not just to alert recipients |
| digest_recipients | Table of User | Who receives daily/weekly digests (independent of alert recipients) |
| assigned_fde | Link → User | The FDE responsible for this Company; drives Discrepancy auto-routing (Section 14) |
| default_rejected_warehouse_override | Link → Warehouse | Optional per-Company override of the account-level GRN policy default (Section 3.3.6) |
| default_in_transit_warehouse_override | Link → Warehouse | Optional per-Company override |

### 3.5.4 Permissions and roles — and how they are created

This section specifies not just the permission *policy* but the *mechanism* by which each permission exists, because the difference determines whether a permission appears automatically in every deployment or has to be set up. The governing rule: **everything that can be expressed as a role, a DocType permission, or a permission level ships as code/fixtures and is created automatically on install/migrate — never by hand. Only the things that are inherently instance-specific (a real user account, and that user's per-Company scoping) are an onboarding step, and those are checklisted so they are never ad-hoc.**

**The roles (canonical set — see Section 14.3)**

The integration ships **five custom roles**, defined once in Section 14.3 and used consistently across the whole spec. Section 3 does not invent its own role vocabulary; it uses these. The two that matter for the Account and connection model in this section:

- **EasyEcom System Manager** — full access to the EasyEcom Account including all credential sections; the only role that can **set or overwrite** credentials. Note: per the write-only guarantee (§3.7.3), even this role can set/rotate a credential but can never read its plaintext back — "access to credential sections" means the ability to write them, not to reveal them.
- **EasyEcom FDE** — read-only on the EasyEcom Account, plus read/write on the per-Company settings record for the Companies they are assigned to.

The remaining three (EasyEcom Operator, EasyEcom Replay Approver, EasyEcom Auditor) govern operational records and are specified in Section 14.3; they are not needed to build the §3 connection model but ship in the same fixture. Note these are *custom* roles shipped by the app — not Frappe's built-in System Manager, and not ERPNext's Accounts Manager.

**The permission policy**

- The EasyEcom Account is readable by **EasyEcom FDE** (read-only) and readable/writable by **EasyEcom System Manager**.
- The credential-bearing fields of the Account — the Setup section (api_key, email, password), the Inbound Webhook Auth section (webhook_token), and the Slack credential fields of the Notifications section — are restricted to **EasyEcom System Manager only**, even for an EasyEcom FDE who can otherwise read the Account.
- Per-Company settings records are readable and editable by the **EasyEcom FDE** assigned to that Company (and by EasyEcom System Manager).
- Background sync work runs as a dedicated **sync user** — a real User account holding the EasyEcom System Manager role, scoped to the relevant Companies via User Permissions, so a sync worker acting for Company A cannot read or write Company B's data.
- A user with access to one Company cannot see another Company's sync logs, sync state, or settings (enforced by the Company link on every operational record plus User Permissions — Section 14.2).

**How each of these is created**

| What | Mechanism | Created how | Manual per instance? |
| --- | --- | --- | --- |
| The five custom roles (Operator, FDE, Replay Approver, System Manager, Auditor) | Shipped as a **fixture** (Role records) in the parent app — the same fixture listed in Section 31.6 | On install / migrate (fixture load) | No — automatic |
| Account read for EasyEcom FDE; read/write for EasyEcom System Manager | DocPerm rows in the EasyEcom Account DocType definition | On `bench migrate` (part of the DocType) | No — automatic |
| Credential-field restriction to EasyEcom System Manager | The credential fields are assigned a higher **permlevel** (e.g. permlevel 1); a DocPerm grants permlevel-1 read/write to EasyEcom System Manager only (Section 31.7.3) | On `bench migrate` (DocType definition) | No — automatic |
| Per-Company settings editable by the assigned EasyEcom FDE | DocPerm on the per-Company settings DocType keyed to the role; the assigned_fde link plus a User Permission drive row-level visibility | DocPerm on migrate; the User Permission is applied when the FDE is assigned (onboarding) | Role/perm automatic; the assignment is onboarding |
| The dedicated **sync user account** | A real User record (with its own credentials) holding the EasyEcom System Manager role | **Onboarding step** — see below | Yes — onboarding |
| The sync user's and FDEs' **per-Company User Permissions** | User Permission records binding a user to specific Companies | **Onboarding step** | Yes — onboarding |

The split is deliberate and unavoidable: roles and permission *rules* are not instance-specific, so they ship in code and require zero manual setup in any deployment. But a *user account* references a real person or service credential, and a *User Permission* references the actual Companies a deployment has — neither exists until the deployment exists, so neither can be a fixture. Shipping a literal user as a fixture would be wrong (it would hard-code a credential into the app). These two are therefore explicit, documented onboarding actions, not ad-hoc manual fiddling: the onboarding playbook creates the sync user (granting it the EasyEcom System Manager role) and assigns the per-Company User Permissions for that user and for each FDE, as a required, checklisted step before go-live.

This is why the build must define the roles and permissions **as part of the DocType definitions and as fixtures** — so that a freshly installed instance already has every role, every DocPerm, and every permlevel restriction in place, and the only permission-related onboarding work is creating the sync user and binding users to their Companies.

## 3.6 The EasyEcomClient class

A single Python class encapsulates every interaction with EasyEcom. No code outside this class talks to EasyEcom directly. The client is constructed against the EasyEcom Account (for credentials) and a location_key (for JWT scope). Responsibilities:

- Token acquisition via POST /access/token with the account's email, password, and the target location_key
- Token caching, scoped per location_key (one account credential set; one JWT per location). EasyEcom JWTs are valid for 90 days. A scheduled daily job renews each enabled location's JWT once it reaches 85 days of age (a 5-day safety margin before the 90-day expiry), writing the new token to the location's jwt_token cache. On any 401 the client also re-authenticates immediately as a fallback, so an unexpected early invalidation never blocks a flow
- Two mandatory headers on every authenticated call: `x-api-key: {account api_key}` and `Authorization: Bearer {jwt}`. Missing either is a 401. The x-api-key is sent even on the token-acquisition call
- Automatic re-authentication on HTTP 401 — no caller has to handle auth retry
- Exponential back-off with jitter on 429 (rate limit) and 5xx — initial 1s, doubling, max 60s, max 6 retries; on exhaustion it raises the corresponding transient subclass from the §31.5 hierarchy (EasyEcomRateLimitError for 429, EasyEcomServerError for 5xx, EasyEcomTimeoutError for network timeouts) — all subclasses of EasyEcomAPIError
- Connection-error retries (TCP-level failures) with the same back-off
- Request and response logging to EasyEcom API Call with credentials redacted
- Mandatory request_id header on every outbound call (UUID4) — used for cross-system trace correlation
- Tier-aware rate limiting: the client throttles to the account's rate_limit_tier request-rate and tracks consumption against the tier's daily quota (Section 3.10), so it slows or pauses before EasyEcom returns 429 rather than only reacting to it
- Mandatory location_key on every operational call; the resolved Company is derived from the location, never inferred from globals
```
# Illustrative shape, not full implementation
class EasyEcomClient:
    def __init__(self, company: str | None = None, location_key: str | None = None): ...
    def get(self, endpoint: str, params: dict) -> dict: ...
    def post(self, endpoint: str, body: dict, idempotency_key: str | None = None) -> dict: ...
    def authenticate(self) -> str: ...  # returns JWT for this location_key
    def _refresh_token_if_needed(self): ...
    def _log(self, request, response, latency_ms): ...

# Exception classes are defined authoritatively in §31.5 (EasyEcomError →
# EasyEcomAPIError → {EasyEcomAuthError, EasyEcomRateLimitError,
# EasyEcomTimeoutError, EasyEcomServerError, EasyEcomValidationError,
# EasyEcomDuplicateError}). Build against §31.5, not this sketch.
```

## 3.7 Credential handling — encrypted, write-only, never readable back

Credentials are the highest-sensitivity data in the integration. The standard is not merely "encrypted" — it is **set-only**: an authorised user can write a credential, but no user, role, API path, report, or export can ever read its plaintext back. Encryption-at-rest alone does not achieve this (an authorised session can still decrypt an encrypted field); the write-only guarantee is a deliberate build requirement on top of encryption.

### 3.7.1 Which fields are credentials

The following are credential fields and are governed by every rule in this section: `x_api_key`, `email`, `password`, `webhook_token`, `slack_webhook_url`, and the cached `jwt_token`. The EE login `email` is included deliberately — it is one half of the email+password pair that authenticates to EasyEcom, so it is protected to the same standard as the password, not left as plain Data.

### 3.7.2 Encrypted at rest

- Every credential field is a Frappe **Password** fieldtype (the cached `jwt_token`, being long, is stored encrypted at rest by the controller). Frappe encrypts Password fields in the database with the site `encryption_key`; the raw database row holds ciphertext, never plaintext.
- The site `encryption_key` lives in site config, outside the database, so a database dump alone cannot decrypt the credentials.

### 3.7.3 Write-only in the UI and the backend

- In the desk form, credential fields render masked and show only a **set / not-set** indicator — never the stored value. The standard Frappe "reveal" affordance is disabled for these fields.
- There is **no read-back path**. The integration exposes no endpoint, whitelisted method, report column, list view, or export that returns a credential's plaintext. The Frappe `get_password` / `get_decrypted_password` route is not exposed for these fields to any role.
- The decrypted value is materialised **only** transiently inside the EasyEcomClient at the moment of building an outbound request (to set the `x-api-key` / `Authorization` header or the token-acquisition body), and is never written to a response, a log, a return value, or a document field.
- **This applies to every role, including EasyEcom System Manager and Frappe's built-in System Manager.** A System Manager can *overwrite* a credential (rotation) but cannot *retrieve* the existing one. There is no privilege level that can read a credential back in plaintext.
- Consequence, stated honestly: credential debugging is **rotate-and-re-enter**, never **reveal-and-compare**. To verify or change a credential you set a new value; you never read the current one out. This is the intended trade-off — the value of "no plaintext is ever retrievable" outweighs the convenience of reading a key back to compare it.

### 3.7.4 Redacted in logs

EasyEcom API Call entries store request payloads, response bodies, and headers for audit. Before any value is persisted, a centralised redaction pass removes:

- Any field whose name matches: x_api_key, x-api-key, authorization, password, token, secret, jwt, email, slack_webhook_url
- Any field whose value matches a Bearer-token pattern
- Any field marked sensitive in a redaction config

Redaction is applied to both request and response, and to webhook payloads on receipt. The redaction function is centralised — every log write goes through it, so no flow can forget it. A redaction *failure* is itself an audit event (and the offending record is not written until redaction succeeds), so a bug in redaction can never silently leak a credential into a log.

## 3.8 Webhook authentication

Inbound webhooks from EasyEcom are authenticated by a shared bearer token. EasyEcom does not sign webhook bodies; it attaches a pre-shared token to each delivery, in one of two header forms (the seller chooses the form when configuring the webhook on the EasyEcom side):

- `Access-token: {token}`
- `Authorization: Bearer {token}`

Our receiver accepts either form: it reads the token from `Access-token` if present, otherwise from `Authorization` (stripping the `Bearer ` prefix), and compares the supplied token against the configured webhook_token in constant time.

- The token is stored account-level as webhook_token (encrypted at rest)
- Token mismatches, and requests carrying neither header, are logged and rejected with 401 — never processed
- Comparison is constant-time to avoid timing side channels
- EasyEcom retries a failed delivery up to 5 times with exponential back-off on any response other than 200 (Section 13 covers the processing contract). The receiver must therefore authenticate, deduplicate, and return 200 quickly and idempotently: a slow or non-200 response triggers EE's retries, and duplicate deliveries are expected and absorbed by the dedup key (Section 3.3.5)
- Company routing: the webhook payload carries a location_key (or a location identifier we map to one); the receiver resolves that location to its Frappe Company via EasyEcom Location.frappe_company before processing. Webhooks for the primary or non-operational locations that resolve to no Company are handled as master/account events, not posted into a Company.

## 3.9 Connection health monitoring

A Connection Health dashboard surfaces, at the account level and rolled up per Company:

- Last successful authentication timestamp per location_key
- API call success rate (last 1h, last 24h, last 7d)
- Average latency per endpoint
- Outstanding queue depth (EasyEcom Queue Job rows in queued or retrying state)
- Webhook receipt rate vs expected rate (rough heuristic — sustained anomaly triggers an alert)
- Sync cursor lag (how far behind real-time each per-location pull cursor is)
- Daily API quota consumption against the rate_limit_tier ceiling (calls used / quota, with a Warning as it approaches the cap)
Health alerts fire to the FDE via Frappe's standard notification system when any metric crosses configured thresholds.

## 3.10 Operational rate-limit handling

EasyEcom rate-limits **per X-API-Key** (not per user, not per JWT). Limits are tiered; a newly generated key starts in the Default tier and is upgraded by EasyEcom only after their UAT review. The account's tier is recorded in rate_limit_tier (Section 3.3.2) and the integration throttles to it.

| Tier | Request rate | Burst | Daily quota |
| --- | --- | --- | --- |
| Default | 5 / sec | 10 | 500 / day |
| Bronze | 5 / sec | 10 | 50,000 / day |
| Silver | 20 / sec | 40 | 200,000 / day |
| Gold | 30 / sec | 60 | 300,000 / day |
| Diamond | 30 / sec | 60 | 500,000 / day |

Handling rules:

- The client throttles outbound calls to the configured tier's request rate, allowing short bursts up to the tier's burst ceiling.
- The integration tracks the running daily call count against the tier's quota and surfaces it on the Connection Health dashboard. As consumption approaches the quota it slows non-urgent background work (master re-syncs, inventory pulls) ahead of urgent work (webhook-triggered processing), and raises a Warning before the quota is exhausted.
- The Default tier's 500/day quota is too low for production traffic — it exists for onboarding and testing only. An FDE must not put a Default-tier key into production cutover; the spec treats "tier still Default at go-live" as a blocking onboarding condition.
- On 429 (quota consumed or rate exceeded): back off with jitter, requeue via the Queue Job retry path, and alert the FDE if 429s persist for > 5 minutes.
- On 5xx: back off, requeue, alert FDE if sustained for > 15 minutes.
- EasyEcom outage detection: if all calls for the account fail for > 10 minutes, surface a Connection Down banner in the desk; if calls for a single operational location fail while others succeed, scope the banner to the affected Company.
- When the FDE updates rate_limit_tier (after an EasyEcom upgrade), the new ceilings take effect without code change.

## 3.11 Acceptance criteria (Section 3 is done when…)

This is the build-and-test contract for Section 3. Claude Code builds to it; the FDE team test script (`process/test_scripts/foundation_section_3_and_4.md`) verifies it on staging. Section 3 is done when all of the following hold:

- **Account config exists and is editable.** An EasyEcom Account record can be created with api_endpoint, x_api_key, email, password, and a mandatory rate_limit_tier (no preset default).
- **Credentials are set-only and never readable back.** Every credential field (x_api_key, email, password, webhook_token, slack_webhook_url, jwt_token) is encrypted at rest and stored as a Password field. In the desk form they show a set/not-set indicator, never the value, with no reveal affordance. No role — including EasyEcom System Manager and Frappe's built-in System Manager — can retrieve a credential's plaintext through the form, any API or whitelisted method, a report, a list view, or an export; a credential can only be overwritten, never read out. The decrypted value appears only transiently inside the EasyEcomClient when building an outbound request. A test that attempts to read each credential back through every surface returns masked/empty, not plaintext (Section 3.7).
- **Token acquisition works.** With valid credentials, a Test Connection action acquires a JWT for the primary location via POST /access/token and reports success inline. With invalid credentials it reports a clear failure, not a stack trace.
- **Both headers are sent on every call.** Every outbound request carries `x-api-key` and `Authorization: Bearer {jwt}`. A call with either header removed (test harness) returns 401 and is handled as an auth failure.
- **JWT is cached per location and reused.** A second call against the same location does not re-acquire a token; the cached JWT (90-day validity) is reused. jwt_acquired_at / jwt_expires_at are populated.
- **Day-85 renewal is scheduled.** The renewal job (`renew_aging_jwts`) is registered in scheduler_events and, when a JWT's age crosses 85 days (simulated by back-dating jwt_acquired_at), renews it on the next run.
- **On-401 re-auth works.** If a call returns 401 (simulated by invalidating the cached JWT), the client re-authenticates once and retries transparently; the caller does not see the 401.
- **Locations are discovered and recorded.** A location pull (`/getAllLocation`) creates EasyEcom Location records in workflow state To Map. Exactly one can be flagged is_primary (FDE-set); is_operational is workflow-derived (true only in Live); frappe_company is set in the mapped states and empty in the unmapped states (§8.4.1). A location left in To Map / Skipped is inert.
- **Foundational calls are logged account-scoped.** The token and location-discovery calls each write an EasyEcom API Call row with easyecom_account set, company blank, is_foundational = 1, credentials redacted in the stored payload.
- **Rate-limit tier drives the throttle.** With tier = Default, outbound throughput is capped at 5 req/sec and the daily-quota counter increments; with tier = Diamond the cap is 30 req/sec. Changing the tier field changes the effective cap with no code change.
- **429 and 5xx back off and surface.** A simulated 429 triggers back-off and requeue; sustained failure raises the configured alert and the Connection Health status reflects Degraded / Down.
- **Webhook auth is bearer-token.** The webhook receiver accepts a valid token in either `Access-token` or `Authorization: Bearer` header and rejects a missing/invalid token with 401. (Full webhook processing is tested with the flows that use it; this criterion covers only auth.)
- **Permissions and roles exist on a fresh install with no manual step.** After install/migrate on a clean site: the five custom roles (EasyEcom Operator, FDE, Replay Approver, System Manager, Auditor) exist as fixtures; the EasyEcom Account DocType grants read to EasyEcom FDE and read/write to EasyEcom System Manager; the credential fields (api_key, email, password, webhook_token, Slack fields) are at a restricted permlevel readable/writable only by EasyEcom System Manager (a user holding EasyEcom FDE but not EasyEcom System Manager can open the Account but cannot see those field values); per-Company settings carry the assigned-FDE DocPerm. None of these required creating a role or permission by hand. The only permission-related onboarding actions are creating the dedicated sync user (granting it EasyEcom System Manager) and binding users to Companies via User Permissions (§3.5.4) — these are documented onboarding steps, not defaults.
- **Connection Health reflects reality.** The dashboard shows last successful auth per location, success rate, and daily-quota consumption, and updates after the above actions.

# 4. The EasyEcom Data Model in ERPNext

This section consolidates every DocType the integration introduces, plus the custom fields it adds to ERPNext-core DocTypes. It is the authoritative reference for engineering — what to build, what to migrate, what to ship as fixtures.

## 4.1 New DocTypes (parent app)

### 4.1.1 Configuration DocTypes

| DocType | Type | Purpose |
| --- | --- | --- |
| EasyEcom Account | Standard, one per deployment | Credentials + account-wide config; the connection/sync boundary |
| EasyEcom Company Settings | Standard, per Company | Per-Company alert recipients, assigned FDE, per-Company overrides |
| EasyEcom Location | Standard | Per-location_key flags (primary/operational), Company resolution, JWT cache, pull cursors |
| EasyEcom Tax Rule Map | Standard, per (tax_rule_name, Company) | Maps an EasyEcom tax rule name → that company's Item Tax Template rows (with native Min/Max Net Rate slab bands); FDE-configured; resolver stamps onto items at sync (Section 8.5) |
| Marketplace | Flat channel list | One row per EasyEcom channel, keyed by marketplace_id; spans B2C marketplaces, B2B, storefronts, POS (Section 8.6). No child Marketplace-Channel hierarchy — EE is flat |
| Marketplace Account | Standard, per (Company, Marketplace) | Seller account on a marketplace |
| Warehouse Source-of-Truth Map | Standard, per Company | Per-warehouse SoT configuration |
| EasyEcom Category Map | Standard, per Company | EE category ↔ Item Group mapping |
| Marketplace Anonymous Customer | Standard | Per-marketplace anonymised buyer pool tracking |

### 4.1.2 Operational DocTypes

The integration deliberately separates three distinct kinds of operational records, each answering a different question:

- EasyEcom Sync Record — entity-centric: 'has this Item / Customer / Order ever synced, and what's its current state?' One row per (ERPNext document, sync direction). Updated in place across retries
- EasyEcom API Call — call-centric: 'what happened on this specific outbound HTTP call to EasyEcom?' One row per request. Append-only, never updated
- EasyEcom Webhook Event — inbound-centric: 'what did EasyEcom send us, and how did we process it?' One row per inbound webhook. Append-only
This separation matters because the questions different operators ask map cleanly onto different records. An FDE asking 'why is this SKU not synced' lands on the Sync Record. An engineer asking 'what was the actual payload of that 500 error 8 minutes ago' lands on the API Call. An ops engineer asking 'have we received the manifest webhook for order 1234' lands on the Webhook Event. A unified log does none of these well.

**Relationship to Frappe's Error Log:** Frappe's built-in `Error Log` DocType captures Python exceptions raised anywhere in the system. The integration's three logs do NOT replace Error Log — Error Log continues to capture stack traces for unhandled exceptions, and engineering reads it for debugging. EasyEcom API Call is broader (it captures every HTTP call, including successful ones, for forensic analysis and schema drift detection); EasyEcom Sync Record is entity-centric (which Error Log isn't designed for); EasyEcom Webhook Event captures inbound traffic (which Error Log doesn't see at all). Each log answers a different question. An exception raised inside a worker creates an Error Log row AND updates the relevant Sync Record AND records an API Call — the three are complementary, not redundant.

| DocType | Purpose |
| --- | --- |
| EasyEcom Sync Record | Entity-centric. One per (ERPNext doc, direction). Tracks attempts, current status, payload hashes for change detection, last error, last success |
| EasyEcom API Call | Call-centric, append-only. One per outbound HTTP call. Endpoint, request payload, response, status, latency, retry attempt number, parent Sync Record (if any), correlation ID |
| EasyEcom Webhook Event | Inbound-centric, append-only. Raw payload, token auth result, idempotency dedup key, processing result, downstream Frappe documents created/updated |
| EasyEcom Sync Cursor | Persistent cursor per (resource, Company, location). Modified in place by polling workers |
| EasyEcom Queue Job | Tracks async work units (push, pull, retry). Distinct from API Call — one Queue Job may produce multiple API Calls across retries |
| EasyEcom Schema Snapshot | Hashed shape of every distinct payload variant seen. Powers schema drift detection (Section 20) |
| EasyEcom Payload Sample | One redacted sample per distinct schema, dated. Powers diff against historical shapes |
| EasyEcom Error Translation | FDE-editable library mapping raw EE error patterns to plain-English explanations and suggested actions (Section 25) |
| EasyEcom Mapping Coverage Snapshot | Periodic record of what fraction of fields in real payloads matched a mapping rule vs identity vs dropped |
| EasyEcom SLA Budget | Per-flow per-Company commitment (e.g., 'B2C SI within 5 min of manifest 99%'). Drives Section 21 |
| EasyEcom SLA Breach | One per breach incident. Tracks duration, root cause, financial impact |
| EasyEcom Configuration Audit | Append-only log of every Settings or Field Mapping change. Captures before/after, actor, reason (Section 26) |
| EasyEcom Field Mapping | Declarative ruleset for translating ERPNext shapes ↔ EasyEcom payloads (Sections 8.0 and 21) |
| EasyEcom Field Mapping Version | Snapshot of a Field Mapping ruleset at a point in time. Created on every save. Enables time travel |
| EasyEcom Replay Plan | FDE-created plan for replay operations. Holds the filter, override values, dry-run results before commit (Section 19) |
| Marketplace Order Map | Bridge between marketplace order ID and ERPNext Sales Invoice — the join key for recon |
| Integration Discrepancy | Distinct from recon Discrepancy; surfaces integration-level data divergence with attached financial impact (Section 23) |
| EasyEcom Morning Brief Snapshot | One per Company per day. Materialised view powering the daily-driver opening screen (Section 24) |

## 4.2 Custom fields on ERPNext-core DocTypes

All shipped as fixtures with the ecs_ prefix. Listed here as the canonical inventory; any future change goes through fixture versioning.

**This section is an inventory, not a "create all of these now" instruction.** It catalogues every custom field the integration will add across its whole lifetime so there is one place to see the complete set. It does **not** mean every field is created during the foundation build. Each field group is **owned by the flow that uses it**, and a field is created as a fixture **only when its owning flow is built** — not before. Creating a field before its flow exists would commit to the field's shape before the flow that uses it has validated that shape, and would leave the schema full of fields nothing reads or writes. The owning flow for each group:

| Custom-field group | Owning flow / section | Built when |
| --- | --- | --- |
| Item, Customer, Supplier correlation fields (ecs_easyecom_*_id, ecs_easyecom_mappings, ecs_push_status, ecs_last_sync_at, ecs_last_sync_error) | Master Sync (Section 8) | With master sync |
| Warehouse fields (ecs_easyecom_location_id, ecs_inventory_master, ecs_is_rejected_warehouse, ecs_is_in_transit_warehouse) | Master Sync — Warehouse (Section 8.4) | With warehouse master sync |
| Purchase Order fields (ecs_easyecom_po_id, ecs_easyecom_po_mappings, …) | Buying (Section 9) | With the buying flow |
| Purchase Receipt fields (ecs_easyecom_grn_id, ecs_supplier_invoice_date, ecs_easyecom_grn_line_id, …) | Buying (Section 9) | With the buying flow |
| Stock Entry fields (ecs_easyecom_source_event, ecs_easyecom_source_type, …) | Stock Transfers (Section 10) | With stock transfers |
| Sales Order fields (ecs_easyecom_so_id, ecs_easyecom_so_mappings, …) | B2B Sales (Section 11) | With B2B sales |
| Sales Invoice fields (ecs_easyecom_order_id, ecs_easyecom_invoice_id, ecs_marketplace_order_id, ecs_marketplace*, …) | B2C Sales (Section 12) | With B2C sales |
| Recon-feeding fields (ecs_settlement_forecast, ecs_actual_net, ecs_variance_*, ecs_recon_run, ecs_marketplace_payout) | Recon engine (PRD-owned) | With the recon engine |

The fixture file (`custom_field.json`, Section 31.6) is therefore assembled flow-by-flow as each flow is built, not shipped complete at foundation time. When a build packet for a flow is prepared, the custom fields in that flow's group are created as part of that flow's build.

**UI placement — a dedicated "EasyEcom" tab or section per DocType.** Integration custom fields are never scattered inline among native ERPNext fields. On each core DocType they are grouped together under a single **collapsible "EasyEcom" tab** where the DocType's form structure supports tabs (Frappe v16 Tab Break), or a **collapsible "EasyEcom" section** (Section Break) where a tab is not appropriate for that DocType's layout. The choice between tab and section is made per DocType based on how that DocType's form is organised — the rule is only that every integration field lives in one clearly-labelled EasyEcom group, never interleaved with native fields. This keeps the native form clean, puts all integration fields where an operator expects them, and lets the whole group be shown/hidden and permission-controlled together.

### 4.2.1 Item

- ecs_easyecom_company_product_id — Data, the single EE master product id for the account (masters are account-global). Stored in child table ecs_easyecom_mappings
- ecs_easyecom_mappings — Table with rows per (frappe_company, easyecom_location_key, easyecom_company_product_id)
- ecs_marketplace_skus — Table with rows per (marketplace, marketplace_sku, channel)
- ecs_push_status — Select: Not Synced / Pending / Synced / Failed
- ecs_last_sync_at — Datetime
- ecs_last_sync_error — Long Text (last failure reason)

### 4.2.2 Customer

- ecs_easyecom_customer_id — Data
- ecs_is_marketplace_pseudo — Check (True for the per-marketplace buyer-pool customers)
- ecs_marketplace — Link → Marketplace (only for pseudo-customers)
- ecs_push_status, ecs_last_sync_at — same pattern as Item

### 4.2.3 Supplier

- ecs_easyecom_vendor_id — Data
- ecs_easyecom_mappings — Table for multi-company
- ecs_push_status, ecs_last_sync_at

### 4.2.4 Warehouse

- ecs_easyecom_location_id — Data (set if linked, blank if not)
- ecs_inventory_master — Select: ERPNext / EasyEcom (mirrored from Source-of-Truth Map for convenience)
- ecs_is_rejected_warehouse — Check
- ecs_is_in_transit_warehouse — Check

### 4.2.5 Purchase Order

- ecs_easyecom_po_id — Data (single EE PO if all lines on one location)
- ecs_easyecom_po_mappings — Table for multi-warehouse POs
- ecs_push_status, ecs_last_sync_at, ecs_last_sync_error

### 4.2.6 Purchase Receipt

- ecs_easyecom_grn_id — Data (the dedup key)
- ecs_supplier_invoice_date — Date
- ecs_easyecom_grn_payload_ref — Link → EasyEcom Webhook Event
- Item-level: ecs_easyecom_grn_line_id

### 4.2.7 Sales Order

- ecs_easyecom_so_id — Data
- ecs_easyecom_so_mappings — Table for multi-warehouse SOs
- ecs_settlement_forecast — Link → Settlement Forecast
- ecs_expected_net, ecs_expected_settlement_date — already in PRD
- ecs_push_status, ecs_last_sync_at

### 4.2.8 Sales Invoice

- ecs_easyecom_order_id — Data (the EE internal Order_id; shared across a split order's shipments)
- ecs_easyecom_invoice_id — Data (the EE internal shipment/Invoice ID; unique per Sales Invoice; the B2C idempotency key)
- ecs_marketplace_order_id — Data (the marketplace identifier via reference_code; the join key for recon)
- ecs_marketplace — Link → Marketplace (the flat channel list keyed by EE marketplace_id; this single field is the channel — there is no separate channel field, because EE has no marketplace/channel hierarchy). Also the value stamped into the "Channel" accounting dimension (Section 4.4)
- ecs_marketplace_account — Link → Marketplace Account
- ecs_payment_mode — Select: Prepaid / COD / EMI / etc.
- ecs_awb_number, ecs_courier — Data
- ecs_actual_net, ecs_variance_amount, ecs_variance_pct, ecs_settlement_status — already in PRD
- ecs_marketplace_order_map — Link → Marketplace Order Map

### 4.2.9 Stock Entry

- ecs_easyecom_source_event — Data (event ID that originated this entry)
- ecs_easyecom_source_type — Select: transfer / GRN / dispatch / adjustment
- ecs_easyecom_location_from, ecs_easyecom_location_to — Data

### 4.2.10 Bank Transaction (already in PRD)

- ecs_marketplace_payout — Link → Marketplace Payout

### 4.2.11 Journal Entry (already in PRD)

- ecs_recon_run — Link → Reconciliation Run
- ecs_settlement_batch — Link → Settlement Batch

## 4.3 Fixtures shipped with parent app

- Marketplace (flat channel list) — the **authoritative** list is synced from EasyEcom `/current-channel-status` at onboarding (Section 8.6), keyed by EasyEcom marketplace_id. A small **starter seed** (`marketplace.json`) of common channels with sensible default channel_type classifications ships as a fixture to speed first-time FDE setup; the onboarding sync then reconciles it against the client's actual EE channel list (adding, activating, and reclassifying as needed). The seed is a convenience, not the source of truth — EE is.
- Tax category mapping skeleton (FDE finalises during onboarding)
- HSN-to-Item-Tax-Template fixture for common HSN codes
- Custom field definitions for all DocTypes above
- Standard Reconciliation Rules from methodology v0 (PRD Section 3.6)
- Standard Methodology Defaults values (PRD Section 3.5)

## 4.4 The Channel accounting dimension (marketplace-wise reporting)

To support marketplace-wise profitability reporting, the integration ships a custom ERPNext **Accounting Dimension** so that revenue, fees, and other P&L can be sliced by marketplace in native ERPNext financial reports — not only through the recon engine's own reports.

**Name and grain.** The dimension is named **"Channel"** and its **reference document type is the `Marketplace` doctype** — the single flat channel list keyed by EasyEcom `marketplace_id` (Section 8.6). Its values are EasyEcom channels (Amazon.in, Flipkart, meesho, Cloudtail B2B, Zepto, …) — the same flat list EasyEcom itself uses, which spans B2C marketplaces and B2B channels alike. There is no coarse/fine split, because EasyEcom has no hierarchy: each channel is one entry. Where several EE channels should report together (Amazon.in + Amazon_FBA + Amazon.co.uk), the optional `reporting_parent` on the Marketplace row (Section 8.6.1) provides rollup — the dimension can be reported at either the raw-channel level or rolled up to the reporting parent.

**Optional, never mandatory — and this is what keeps non-marketplace transactions working.** The dimension is configured as **optional**: it is present and reportable on transactions, but **not** set "Mandatory For Profit and Loss Account" and **not** "Mandatory For Balance Sheet." This is a deliberate design decision with a concrete reason:

- ERPNext's mandatory flags are **account-driven, not document-type-driven** — there is no per-voucher-type "mandatory" switch. If "Mandatory For P&L" were on, *every* GL line posting to a P&L account across the client's entire books — including transactions with no marketplace, and including the client's own manual journal entries and expense postings — would be forced to carry a Channel value.
- Many integration transactions legitimately have **no marketplace**. A Stock Reconciliation posts its inventory line to a Balance Sheet account (stock-in-hand) but posts its *difference* to a Stock Adjustment account, which is a **P&L** account — so a stock reco with any adjustment produces a P&L line that has no marketplace. The same is true of inter-warehouse transfer losses, general expenses, bank charges, and similar postings.
- Forcing a marketplace onto such a line would be **fiction** — a stock-adjustment loss is genuinely not attributable to a marketplace. Optional lets the system record the truth: these lines carry no Channel and report as Unallocated, rather than being blocked or stamped with an invented value.

Marketplace-wise P&L therefore works off the transactions that genuinely *do* carry a marketplace — every B2C Sales Invoice (which the integration stamps with `ecs_marketplace`) and the marketplace fee postings — which is exactly where the reporting value lies. Channel-less P&L lines (stock recos, general expenses) appear under Unallocated, which is correct.

**Upgrade path.** A client with a hard requirement that *100% of P&L be marketplace-attributed with zero Unallocated* may upgrade the dimension to "Mandatory For P&L" — but only by also defining an explicit "Unallocated" marketplace value and accepting that every non-marketplace P&L posting (theirs and the integration's) must then carry it. This is an **FDE/methodology onboarding choice, not the default**; the default is optional.

**How it is created.** The Channel Accounting Dimension itself ships as a **fixture** (created automatically on install/migrate). Its **per-Company Dimension Defaults rows** — which reference real Companies — are set at **onboarding**, like User Permissions: the FDE adds a row per operational Company (leaving both Mandatory flags off for the default optional configuration). The decision of *which* dimensions exist at all (this Channel/marketplace dimension, and any future ones) is owned by the recon/methodology design; this section specifies only the one the integration needs for marketplace-wise reporting and commits the integration to **populating** it (stamping the marketplace as the Channel dimension value) on the documents it creates that carry a marketplace — the detailed population per flow is specified with those flows (Section 12 for B2C Sales Invoices).

# 5. Path-based Field Mapping (deep specification)

Every master sync and every flow translates between an ERPNext document shape and an EasyEcom payload shape. This section is the formal specification of the engine that performs that translation: path syntax, transformer vocabulary, conditional rules, computed fields, ruleset composition, and execution semantics. It belongs to the foundation because every integration in Part III depends on it; Master Sync (Section 8) is the first consumer, and Section 8.0 recaps the essentials in context. Engineering builds against this section.

## 5.1 Why this engine exists

Stated again because it bears repeating: hardcoding ERPNext-to-EasyEcom translations in Python means every payload tweak is a code deploy, and the rules are invisible to non-engineers. We pay the cost of building a mapping engine because:

- EasyEcom changes its payload shapes; we want field-level adaptation without redeployment
- Different clients have different needs — one wants gross_weight included, another wants it dropped
- FDEs need to inspect and modify mappings during incident response without engineering involvement
- New marketplace channels add fields that should sync without engineering work
- Audit and compliance require visibility into 'what does the integration actually send'

## 5.2 The Field Mapping DocType

| Field | Type | Notes |
| --- | --- | --- |
| mapping_name | Data | Human-readable; e.g., 'EasyEcom-Item-Sync' |
| entity_type | Select | Item / Customer / Supplier / Warehouse / Tax Category / Channel / Order / GRN / etc. |
| direction | Select | Push / Pull / Bidirectional |
| active | Check | Inactive rulesets are not applied; useful for staged rollouts |
| company_scope | Table of Company (or 'all') | Allows per-Company overrides |
| missing_field_policy | Select | Strict (raise) / Permissive (identity-default) / Drop (silently omit) |
| rules | Table of Field Mapping Rule | The actual rules; see below |
| computed_fields | Table of Computed Field | Derived values; see Section 5.6 |
| preconditions | Long Text (Python expression) | Optional. If set, ruleset is applied only when the expression evaluates True for the record |
| version | Int (auto-increment on save) | Powers Section 26 time travel |
| last_modified_by, last_modified_at, change_reason | Audit fields | Required on save |

## 5.3 Field Mapping Rule (child table)

| Field | Type | Notes |
| --- | --- | --- |
| erpnext_path | Data | JSONPath-like selector into the ERPNext document shape |
| easyecom_path | Data | Same for the EasyEcom payload shape |
| transform_push | Select | Transformer applied when going ERPNext → EE; from a closed vocabulary (Section 5.5) |
| transform_pull | Select | Transformer applied EE → ERPNext |
| transform_args | JSON | Optional arguments for the transformer (e.g., date format string) |
| condition | Long Text (Python expression, sandboxed) | Optional. Rule applies only if expression evaluates True |
| default_value | Data | Used when source field is absent (per missing_field_policy) |
| validate_against | Data | Optional. Validates the translated value against a Frappe DocType (e.g., 'HSN Code Master') |
| required | Check | If checked, missing source value raises regardless of missing_field_policy |
| notes | Small Text | FDE-facing documentation of the rule |

## 5.4 Path syntax

A subset of JSONPath chosen for readability and predictable behaviour:

- Dot notation for object access: customer.gstin
- Brackets for array iteration: items[].item_code applies the rule per row
- Filter predicates: items[?type='CGST'].amount selects only matching rows
- Wildcards: items[*].item_code (synonym for items[].item_code)
- Double-dot for recursive descent: ..hsn_code (rare; for deeply-nested or variable shapes)
- Index access: items[0].item_code (specific element)
- Keys with internal spaces are tolerated on the EasyEcom side (e.g. `address type.billing_address.street`, `phone number`) — EasyEcom payloads sometimes use space-bearing keys; both the runtime parser and the compile-time validator accept them.
Not supported (deliberate scope limit): full JSONPath script expressions, regex matching in paths, recursive transformations. If needed, the rule's condition field is the escape hatch — it has the full sandboxed Python expression vocabulary.

## 5.5 Transformer vocabulary

Closed vocabulary of transformer types. Custom Python is the escape hatch but not the default — most translations fit one of these:

| Transformer | Effect | Args |
| --- | --- | --- |
| identity | No transform; pass through | (none) |
| bool_to_yn | True → 'Y', False → 'N' | (none) |
| yn_to_bool | 'Y' → True, 'N' → False | (none) |
| str_lower / str_upper / str_strip | String case/whitespace normalisation | (none) |
| date_format | Date string reformat | {from: 'YYYY-MM-DD', to: 'DD/MM/YYYY'} |
| datetime_to_iso / iso_to_datetime | Frappe datetime ↔ ISO 8601 | (none) |
| int_to_str / str_to_int / float_to_str / str_to_float | Type coercion | (none) |
| currency_to_paise / paise_to_currency | INR rupees ↔ paise (the EE convention) | (none) |
| lookup_id | Resolve a Frappe DocType name to its EE-side ID | {doctype: 'Item', target_field: 'ecs_easyecom_company_product_id'} |
| reverse_lookup_id | EE ID → Frappe document name | {doctype: 'Item', source_field: 'ecs_easyecom_company_product_id'} |
| enum_map | Translate one enum value set to another | {map: {'paid': 'PAID', 'cod': 'COD'}, default: 'OTHER'} |
| conditional_constant | Output a constant per condition | {conditions: [...], default: 'X'} |
| computed | Resolve via the ruleset's Computed Fields table | {name: 'total_with_tax'} |
| custom_python | Sandboxed Python expression; restricted name set | {expression: '...'} |

## 5.6 Computed fields

Some output values aren't single-source translations — they're derived from multiple inputs. The Computed Field child table on the ruleset declares these:

| Field | Type | Notes |
| --- | --- | --- |
| name | Data | Reference name; cited by transform_args.name in the rules table |
| expression | Long Text | Sandboxed Python; allowed names: source_doc, source_payload, get_path(), sum_path(), filter_path() |
| output_type | Select | Decimal / Int / String / Date / Datetime / Boolean / JSON |
| cache_per_record | Check | Computed once per record-level invocation rather than per access |

Example computed field:

```
name: total_with_tax
output_type: Decimal
cache_per_record: True
expression: |
  base_amount + sum_path("items[].tax_components[].amount")
```

## 5.7 Conditional rules

Each rule may carry a condition expression. The expression evaluates against the source record (the ERPNext document for push, the EE payload for pull):

```
# Example: only sync the 'wholesale_price' field for B2B customers
erpnext_path: wholesale_price
easyecom_path: wholesale_price
condition: source_doc.customer_type == 'B2B'

# Example: route the customer reference differently for marketplace orders
erpnext_path: customer
easyecom_path: marketplace_meta.buyer_id
condition: source_payload.channel != 'B2B'
```

## 5.8 Ruleset composition

Some entities have nested child structures that warrant their own rulesets, composed into the parent. Item has UoMs and Aliases as child tables; Order has Lines as a child table. Each gets a separate ruleset, referenced from the parent:

- Parent rule with transform_push='compose', transform_args={ruleset: 'EasyEcom-Item-UOM-Sync'} applies the named ruleset to each row of the source array
- Composed rulesets cannot recurse infinitely — engine enforces a max-depth of 5
- Composition is per-direction — Item-Push composes Item-UOM-Push; Item-Pull composes Item-UOM-Pull
- Field Mapping list shows composed-ruleset relationships visually (a tree icon next to composing rulesets)

## 5.9 Execution semantics

### 5.9.1 Compilation

- On Field Mapping save, the ruleset is compiled to an executable form and cached (Frappe v16 Caffeine)
- Compilation validates: path syntax, transformer name resolution, condition expression compiles, computed field expressions compile, no circular composition
- Validation failure prevents save with a clear error pointing to the offending rule
- Cache invalidation on save; new version takes effect on next API call

### 5.9.2 Application order

1. Preconditions checked; if false, ruleset skipped
1. Composed child rulesets resolved (recursive)
1. Computed fields evaluated (deps-aware order)
1. Each rule applied in declaration order; last write wins for same-target conflicts
1. Identity defaults applied (in Permissive mode) for unmapped fields
1. Required fields checked; raise on missing
1. Output validated against any validate_against constraints

### 5.9.3 Error handling

- A rule failure surfaces a specific exception (FieldMappingRuleError) with the failing rule's ID, condition state, source value, and reason
- In Push direction: rule failures abort the push and create a Sync Record in Failed state with the error captured
- In Pull direction: rule failures abort ingestion of the affected record and create a Sync Record / Integration Discrepancy
- Per-rule failures within a batch operation isolate to the affected record; other records in the batch continue

## 5.10 The FDE editing surface

Field Mapping is the configuration most often touched during incident response and client-specific tuning. The UI is correspondingly important:

### 5.10.1 List view

- Columns: Mapping Name, Entity Type, Direction, Active, Composition Tree (if any), Last Modified By, Coverage % (latest snapshot), Version
- Filter by Entity Type, Direction, Active
- Bulk actions: Activate, Deactivate, Export to JSON, Import from JSON

### 5.10.2 Detail view

- Top: metadata (entity type, direction, mode, version, change reason on last edit)
- Show Computed Mapping action: expands implicit identity matches inline so the FDE sees the full effective mapping
- Test Mapping action: opens a dialog with paste-able sample input; runs the ruleset and shows output side-by-side with annotations per rule
- Diff Against Version action: pick a prior version, see what changed (added rules in green, removed in red, modified in yellow)
- Coverage History tab: time series of mapping coverage % from the latest snapshots
- Change History tab: every save with author, change reason, version diff link

### 5.10.3 Rule editor

- Per-row editor with autocomplete on path syntax (suggests valid paths from a sample payload if available)
- Transformer dropdown with inline help
- transform_args JSON editor with schema validation per transformer type
- Live validation as the FDE types
- Reorder rows by drag-and-drop

## 5.11 Initial ruleset library

Shipped as fixtures in the parent app. FDEs adjust per client; the shipped versions are the methodology team's recommendation for the standard EasyEcom integration.

- EasyEcom-Item-Sync (bidirectional) + EasyEcom-Item-UOM-Sync + EasyEcom-Item-Alias-Sync
- EasyEcom-Customer-Sync (bidirectional) — B2B/D2C
- EasyEcom-Customer-Anon-Pull (pull-only) — marketplace anonymous customer pattern
- EasyEcom-Supplier-Sync (bidirectional)
- EasyEcom-Location-Pull (pull) — maps the `/getAllLocation` payload to EasyEcom Location; used by the §8a discovery flow (renamed from the earlier "Warehouse-Pull"; reconciled to the real payload)
- EasyEcom-Tax-Category-Sync (bidirectional)
- EasyEcom-Channel-Pull (pull)
- EasyEcom-PO-Push (push) + EasyEcom-PO-Line-Push
- EasyEcom-GRN-Pull (pull) + EasyEcom-GRN-Line-Pull
- EasyEcom-SO-Push (push) + EasyEcom-SO-Line-Push (B2B)
- EasyEcom-Order-Pull (pull) + EasyEcom-Order-Line-Pull (B2C marketplace orders)
- EasyEcom-Stock-Reservation-Pull (pull)
- EasyEcom-Cancellation-Pull, EasyEcom-Return-Pull
- EasyEcom-Manifest-Pull, EasyEcom-Dispatch-Pull
- EasyEcom-Inventory-Snapshot-Pull (pull)

## 5.12 Versioning and rollback

- Every Field Mapping save creates a Field Mapping Version snapshot (the entire ruleset at that point)
- Version snapshots retained indefinitely (small payloads; hundreds of versions per ruleset over years is fine)
- Rollback action on Field Mapping detail: pick a prior version, preview diff, click Rollback to restore (which itself creates a new version with author = current user, reason = 'Rollback to v<n>')
- Configuration Audit (Section 26) captures every save and rollback

# 6. Idempotency, Replay, and Correlation

Every operation in the integration is designed to be safely retryable. The integration cannot assume any single API call, webhook delivery, or queue job will succeed; it must assume any of them may be retried, duplicated, or processed out of order. This section specifies the mechanisms that make safe retry possible: idempotency keys, correlation IDs, deduplication windows, and the worker contract.

## 6.1 Idempotency key generation rules

Every outbound mutating call to EasyEcom carries an idempotency_key. The key is deterministic and reproducible — re-running the same logical operation produces the same key, allowing EE (where supported) and our retry logic to detect duplicates without coordination.

| Operation | Idempotency key formula | Stored in |
| --- | --- | --- |
| Item push | sha256(f'item:{company}:{item_code}:{ee_location_key}:{change_hash}') | EasyEcom Sync Record.idempotency_key |
| Customer push | sha256(f'customer:{company}:{customer_name}:{ee_location_key}:{change_hash}') | Sync Record |
| Supplier push | sha256(f'supplier:{company}:{supplier_name}:{ee_location_key}:{change_hash}') | Sync Record |
| PO push | sha256(f'po:{company}:{po_name}:{ee_location_key}') | Sync Record (PO names are immutable) |
| SO push (B2B) | sha256(f'so:{company}:{so_name}:{ee_location_key}') | Sync Record |
| B2B Invoice push | sha256(f'b2b_invoice:{company}:{si_name}:{ee_location_key}') | Sync Record |
| Sync Record retry | Same key as original attempt — never recomputed on retry | Inherited |

change_hash is the SHA-256 of the normalised JSON payload (sorted keys, no whitespace) before any transforms. Two pushes of an unchanged Item produce the same idempotency_key; two pushes that differ in any field produce different keys. This permits us to skip pushes when nothing has changed (common case) without missing pushes that should fire.

These formulae are the **contract**: each operation type has a named key builder (item / customer / supplier / po / so / b2b_invoice). Callers (the flows) must use the builder for their operation; the enqueue facade must not silently substitute a generic key when a per-operation key is expected. The builders live in a small dedicated module (e.g. `easyecom/utils/idempotency.py`) so every flow computes its key the same way.

## 6.2 Correlation IDs

Distinct from idempotency keys, every operational record carries a correlation_id — a UUIDv7 (time-ordered) generated at the entry point of a logical operation. The correlation_id propagates through every downstream record so the operation can be traced end-to-end across the three log DocTypes.

- Webhook receipt → correlation_id assigned, stored on Webhook Event
- Webhook processing → spawns Sync Record with same correlation_id
- Sync Record execution → spawns API Call(s), each with same correlation_id (and per-attempt sub_correlation_id)
- Push from Frappe document_event hook → correlation_id assigned, propagates to Sync Record + API Call
- Replay-induced retry → new correlation_id (so the replay is visible as a distinct operation), with parent_correlation_id pointing at the original
The Event Timeline view (Section 17.8) and the Inspector (Section 17.9) both pivot on correlation_id. Every error message in every log carries the correlation_id so the FDE can immediately jump to the full trace.

## 6.3 The EasyEcom Queue Job lifecycle

Every async operation in the integration flows through EasyEcom Queue Job, which is the FDE's primary debugging interface for in-flight work. **The actual queue mechanism uses Frappe's standard `frappe.enqueue` (RQ-backed) workers; the EasyEcom Queue Job DocType is a tracking and observability layer on top.** This is a deliberate hybrid: Frappe handles the worker pool, queue tiers, and crash recovery; the DocType captures correlation_id, idempotency_key, financial impact, and the FDE-debuggable state machine that Frappe's bare RQ does not provide.

### 6.3.1 The two-layer model

Concretely:

- **Layer 1 — Frappe RQ:** runs the worker processes (`bench start` spawns `short`, `default`, `long` queue workers per Frappe Cloud's standard configuration). Workers pick jobs by name; we pass `job_name=<EasyEcom Queue Job name>` so RQ's job ID matches our DocType.
- **Layer 2 — EasyEcom Queue Job DocType:** captures everything Frappe RQ doesn't: correlation_id, idempotency_key, parent_sync_record, parent_webhook_event, business-level state (Queued / Running / Retrying / Success / Partial / Failed / Cancelled), per-record succeeded_count and failed_count for batch jobs, financial impact when applicable, and per-Company concurrency tracking.
The two layers are kept consistent by writing the DocType row first, then enqueuing via `frappe.enqueue` referencing the row by name. The worker function reads the DocType, executes, and writes state transitions back.

### 6.3.2 Queue tier mapping

Different job types have different latency expectations. Frappe ships three default queues (`short`, `default`, `long`) with separate worker pools. We map our job_types to these:

| Frappe queue | Timeout | Job types | Reason |
| --- | --- | --- | --- |
| short | 5 min (default 300s) | Webhook Process, SLA Breach Compute, Configuration Audit Write | Low-latency: webhook responses must process quickly to keep recon-aware alerts fresh |
| default | 5 min (default 300s) | Item Push, Customer Push, Supplier Push, PO Push, SO Push, B2B Invoice Push, Order Pull, GRN Pull, Return Pull, Field Mapping Compile | Routine integration work |
| long | 1 hr (default 1500s+) | Master Sync Bulk, Replay Plan Step, Inventory Pull, Schema Snapshot Compute, Mapping Coverage Compute, Morning Brief Compute | Bulk operations or scheduled compute that doesn't block real-time flows |

Job-type-to-queue mapping lives in `ecommerce_super.queue.routing.QUEUE_FOR_JOB_TYPE` (a dict). Adding a new job_type requires registering it there.

### 6.3.3 States and transitions

| State | Meaning | Allowed next states |
| --- | --- | --- |
| Queued | DocType row created; frappe.enqueue called; awaiting RQ worker pickup | Running, Cancelled |
| Running | RQ worker is executing the job | Success, Partial, Failed, Retrying |
| Retrying | Failed transiently; re-enqueued via frappe.enqueue with enqueue_after for back-off | Running, Failed, Cancelled |
| Success | Completed; all records in the batch succeeded (terminal) | (none — terminal) |
| Partial | Batch transport succeeded but some per-record units failed; records succeeded_count / failed_count and links the Failed Sync Records (Section 7.3). Failed children are retried individually, not the whole job (terminal for the job) | (none — terminal; failed children retried separately) |
| Failed | Transport failed entirely, or a single-record job's record failed; exhausted retries; FDE intervention required | Queued (via Retry action) |
| Cancelled | Manually cancelled by FDE before pickup, or aborted in-flight | (none — terminal) |

### 6.3.4 Field schema (Frappe DocType definition)

```
# Field name, fieldtype, mandatory, options
job_id              Data        Y   (auto: format ECS-QJ-YYYY-MM-DD-######;
                                     also passed to frappe.enqueue as job_name)
company             Link        Y   Company
job_type            Select      Y   "Item Push" | "Customer Push" | "Supplier Push" |
                                    "PO Push" | "SO Push" | "B2B Invoice Push" |
                                    "Order Pull" | "GRN Pull" | "Return Pull" |
                                    "Inventory Pull" | "Webhook Process" |
                                    "Replay Plan Step" | "Master Sync Bulk" |
                                    "Field Mapping Compile" | "Schema Snapshot Compute" |
                                    "Mapping Coverage Compute" | "SLA Breach Compute" |
                                    "Morning Brief Compute" | "Configuration Audit Write"
target_doctype      Data        N   (the Frappe DocType this job operates on)
target_name         Dynamic     N   (link to target_doctype)
correlation_id      Data        Y   (UUIDv7 from operation entry point)
parent_correlation_id Data      N   (for replay-induced jobs)
idempotency_key     Data        Y   (per Section 6.1)
payload             Long Text   N   (request payload, JSON, redacted on save)
state               Select      Y   (per 11.3.3: Queued|Running|Retrying|Success|Partial|Failed|Cancelled)
succeeded_count     Int         N   (batch jobs: per-record units that reached Success)
failed_count        Int         N   (batch jobs: per-record units that reached Failed;
                                     a Partial job has failed_count > 0 and succeeded_count > 0)
priority            Int         Y   (1=highest, 10=lowest, default 5; affects ordering
                                     within a Frappe queue, not across queues)
queue_tier          Select      Y   "short" | "default" | "long"
                                    (set automatically from job_type via routing.py)
attempts            Int         Y   (default 0; incremented on each Running transition)
max_attempts        Int         Y   (default 5; configurable per job_type in routing.py)
last_error          Long Text   N   (most recent failure trace, if any)
last_error_translation_key Data N   (FK to EasyEcom Error Translation, if matched)
last_attempted_at   Datetime    N
next_attempt_at     Datetime    N   (when in Retrying state; informational —
                                     Frappe RQ owns the actual scheduling via
                                     enqueue_after parameter)
rq_job_id           Data        N   (Frappe's internal RQ job ID; for cross-reference
                                     to bench show-pending-jobs output)
created_at          Datetime    Y
completed_at        Datetime    N
parent_event        Link        N   EasyEcom Webhook Event (if webhook-spawned)
parent_sync_record  Link        N   EasyEcom Sync Record (if sync-spawned)
parent_replay_plan  Link        N   EasyEcom Replay Plan (if replay-spawned)
last_response       Long Text   N   (last successful response body, redacted)
last_api_call       Link        N   EasyEcom API Call (latest call from this job)
```

### 6.3.5 Enqueue API

All async work goes through one entry point that creates the DocType and enqueues via Frappe RQ atomically:

```
# ecommerce_super/easyecom/queue/__init__.py

def enqueue_easyecom_job(
    job_type: str,
    company: str,
    *,
    target_doctype: str | None = None,
    target_name: str | None = None,
    payload: dict | None = None,
    correlation_id: str | None = None,
    parent_correlation_id: str | None = None,
    parent_event: str | None = None,
    parent_sync_record: str | None = None,
    parent_replay_plan: str | None = None,
    priority: int = 5,
    max_attempts: int | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Single entry point for enqueuing async work.

    Returns the EasyEcom Queue Job name (== rq_job_id passed to frappe.enqueue).

    Behaviour:
      1. Look up queue_tier from QUEUE_FOR_JOB_TYPE[job_type]
      2. Compute idempotency_key if not provided (per Section 6.1)
      3. Insert EasyEcom Queue Job row with state=Queued
      4. Call frappe.enqueue(
           method="ecommerce_super.easyecom.queue.workers.execute_job",
           queue=queue_tier,
           job_name=qj.name,            # == EasyEcom Queue Job name
           timeout=TIMEOUT_FOR_JOB_TYPE[job_type],
           easyecom_queue_job=qj.name,  # passed as kwarg to execute_job
         )
      5. Return qj.name
    """
```

### 6.3.6 Worker contract

Every job_type registers a handler function with the worker:

```
# ecommerce_super/easyecom/queue/workers.py

JOB_TYPE_HANDLERS: dict[str, Callable[[Document], None]] = {
    "Item Push": "ecommerce_super.easyecom.flows.master_sync.push_item_handler",
    "Customer Push": "ecommerce_super.easyecom.flows.master_sync.push_customer_handler",
    "PO Push": "ecommerce_super.easyecom.flows.buying.push_po_handler",
    "Order Pull": "ecommerce_super.easyecom.flows.b2c_sales.pull_orders_handler",
    "GRN Pull": "ecommerce_super.easyecom.flows.buying.pull_grns_handler",
    "Webhook Process": "ecommerce_super.easyecom.flows.webhook_router.process_handler",
    "Replay Plan Step": "ecommerce_super.easyecom.replay.commit.step_handler",
    "Master Sync Bulk": "ecommerce_super.easyecom.flows.master_sync.bulk_handler",
    # ... full registry
}

def execute_job(easyecom_queue_job: str):
    """Worker entry point. Called by RQ. Updates DocType, dispatches to handler."""
    qj = frappe.get_doc("EasyEcom Queue Job", easyecom_queue_job)

    if qj.state == "Cancelled":
        return  # FDE cancelled before pickup; no-op

    qj.db_set({
        "state": "Running",
        "attempts": qj.attempts + 1,
        "last_attempted_at": frappe.utils.now_datetime(),
        "rq_job_id": frappe.get_running_job_id(),
    }, update_modified=False)
    frappe.db.commit()

    handler_path = JOB_TYPE_HANDLERS[qj.job_type]
    handler = frappe.get_attr(handler_path)

    try:
        # Per-Company concurrency check (see 11.3.7)
        with company_concurrency_semaphore(qj.company):
            handler(qj)
        qj.db_set({"state": "Success", "completed_at": frappe.utils.now_datetime()})

    except EasyEcomError as e:
        qj.db_set({"last_error": str(e), "last_error_translation_key": _translate(e)})

        if e.retry_policy == "transient" and qj.attempts < qj.max_attempts:
            backoff_s = compute_backoff(qj.attempts)
            qj.db_set({
                "state": "Retrying",
                "next_attempt_at": frappe.utils.add_seconds(frappe.utils.now_datetime(), backoff_s),
            })
            # Re-enqueue via frappe.enqueue with enqueue_after for back-off
            frappe.enqueue(
                method="ecommerce_super.easyecom.queue.workers.execute_job",
                queue=qj.queue_tier,
                job_name=f"{qj.name}-attempt-{qj.attempts + 1}",
                timeout=TIMEOUT_FOR_JOB_TYPE[qj.job_type],
                enqueue_after=backoff_s,
                easyecom_queue_job=qj.name,
            )
        else:
            qj.db_set({"state": "Failed", "completed_at": frappe.utils.now_datetime()})
            raise   # let RQ record the failure too

    except EasyEcomRateLimitError as e:
        # EE asked us to back off explicitly
        qj.db_set({
            "state": "Retrying",
            "next_attempt_at": frappe.utils.add_seconds(
                frappe.utils.now_datetime(), e.retry_after),  # EasyEcomRateLimitError.retry_after
        })
        frappe.enqueue(
            method="ecommerce_super.easyecom.queue.workers.execute_job",
            queue=qj.queue_tier,
            job_name=f"{qj.name}-attempt-{qj.attempts + 1}",
            enqueue_after=e.retry_after,
            easyecom_queue_job=qj.name,
        )

    finally:
        frappe.db.commit()
```

### 6.3.7 Per-Company concurrency

Frappe RQ's worker count is global (e.g., 4 default-queue workers per site). For per-Company concurrency limits — important for aggregator clients where Company A's bulk sync should not starve Company B's webhook processing — we use a Frappe-native semaphore.

```
# ecommerce_super/easyecom/queue/concurrency.py

import frappe
from contextlib import contextmanager

@contextmanager
def company_concurrency_semaphore(company: str):
    """Acquire a per-Company slot. Blocks (or fails fast) if at capacity.

    Implemented via frappe.cache() with atomic incr/decr — no custom Redis client.
    Frappe's cache is Redis-backed, so this is effectively a Redis-native semaphore
    that goes through Frappe's connection pool.
    """
    account = frappe.get_cached_doc("EasyEcom Account", get_account_for_company(company))
    cap = account.max_concurrent_workers or 4
    cache_key = f"easyecom:concurrency:{company}"

    cur = frappe.cache().incr(cache_key)
    if cur > cap:
        frappe.cache().decr(cache_key)
        # Back off and re-enqueue rather than block the worker
        raise CompanyConcurrencyExceeded(f"Company {company} at concurrency cap ({cap})")

    try:
        yield
    finally:
        frappe.cache().decr(cache_key)
```

`CompanyConcurrencyExceeded` is treated as a transient retry (re-enqueued with short back-off), not a failure. This means workers don't sit idle waiting for a slot; they release back to the pool and try again.

**Crash-drift caveat.** The semaphore decrement runs in a `finally` block, so a *clean* exit (success or handled error) always releases the slot. But a worker that is *killed* mid-execution (OOM, SIGKILL, container eviction) never runs `finally`, leaving the Company's counter one slot too high permanently. The crash-recovery path (§6.3.9) must therefore call `concurrency.reset(company)` — or decrement for the reclaimed job — when it reclaims an orphaned Running job, otherwise repeated crashes silently erode a Company's effective concurrency cap. The `reset()` method exists for this purpose; the reclaim hook must invoke it.

### 6.3.8 Retry policy

Exponential back-off with jitter: backoff_seconds = min(2^attempts * 30, 3600) ± random(0, 30). Capped at 1 hour between attempts. After max_attempts, state → Failed.

- Transient errors (HTTP 5xx, timeouts, rate limit): retry up to max_attempts
- Permanent errors (HTTP 400 with structured validation error, HTTP 401/403): no retry; state → Failed immediately
- Classification of error → transient/permanent uses Error Translation Library matchers (Section 25) plus the exception class's `retry_policy` attribute (Appendix C)
- Failed jobs surface in the Workspace's Queue Job depth Number Card and trigger alerts per Section 18
- Manual Retry from the desk: re-enqueues via the same `enqueue_easyecom_job` entry point with the same correlation_id (so all logs link to the original)

### 6.3.9 Crash recovery

Frappe RQ's built-in crash recovery handles the queue-mechanism layer: workers that die mid-job have their jobs re-queued automatically (configurable via `RQ_FAILURE_TTL`). Our DocType layer adds a complementary check: a separate scheduler hook runs hourly, scanning for EasyEcom Queue Job rows in state=Running where last_attempted_at is more than 10 minutes old AND no corresponding RQ job is found. These are reclaimed: state → Retrying with attempts unchanged, then re-enqueued via `enqueue_easyecom_job`.

### 6.3.10 Cron-driven jobs vs enqueued jobs

Polling cron events (Order Pull every 5 min, GRN Pull every 30 min) are NOT enqueued — they're invoked directly by Frappe's `scheduler_events` (per `hooks.py`). The cron handler decides whether to enqueue per-Company work based on `Settings.enabled` and resource flags. This keeps the cron lightweight and the queue clean of recurring work.

In practice the cron handlers iterate Companies and enqueue one job per (Company, resource); those enqueued jobs are the ones that perform the actual EE pull. So the cron creates jobs; jobs do the work. This separation matters for FDE debugging — the cron's job is small and reliable; the per-Company pull jobs are the ones that may fail and warrant Queue Job tracking.

## 6.4 Idempotency at persistence

Idempotency keys protect outbound calls. Inbound idempotency is enforced at the persistence layer:

- EasyEcom Webhook Event has a unique constraint on (event_type, ee_event_id, company); duplicate webhooks are rejected at insert with HTTP 200 (so EE doesn't retry)
- Marketplace Order Map has a unique constraint on (marketplace, marketplace_order_id, company); duplicate ingestion is a no-op update with conflict resolution per Section 14
- Sales Invoice creation from manifest checks for existing SI on (ecs_easyecom_event_id, company) before insert
- Purchase Receipt creation from GRN checks for existing PR on (ecs_easyecom_grn_id, company) before insert
- Stock Reservation Entry mirroring checks (sales_order, item_code, warehouse) before insert

## 6.5 Replay procedures

### 6.5.1 Single-record replay

- FDE opens the Failed Sync Record / Queue Job in the desk
- Reviews the translated error and suggested actions (Section 25)
- Fixes underlying issue if needed (e.g., adds Item Tax Template mapping)
- Clicks Retry Now action → Sync Record state → Pending, attempts counter unchanged, next attempt enqueued
- Idempotency keys ensure no double-processing on the EasyEcom side

### 6.5.2 Bulk replay

- FDE creates a Replay Plan (Section 19) — multi-step lifecycle with mandatory dry run before commit
- Replay Plan can target hundreds or thousands of Sync Records / Webhook Events / Queue Jobs in a single operation
- Permissions: Replay Plan Commit requires Replay Approver role for plans affecting >100 records or financial impact >₹100k

### 6.5.3 Cursor rewind for periodic pulls

- FDE opens EasyEcom Sync Cursor for the (Company, location, resource) combination
- Edits cursor_value field backward to a specific timestamp via the Rewind Cursor action (System Manager only)
- Action captures actor, before_value, after_value, reason in Configuration Audit (Section 26)
- Next polling cycle pulls everything since the rewound cursor
- Idempotency at the persistence layer ensures duplicates are no-ops

### 6.5.4 Resync a master record

- Open the Item / Customer / Supplier in question
- Use the Force Resync action — pushes ERPNext-owned fields to EE and pulls EE-owned fields back, regardless of hash equality
- Useful for suspected silent drift — when both sides claim to be current but differ
- Available to FDE (any role); overrides the change_hash short-circuit

## 6.6 Webhook ordering and delivery semantics

- EE delivers webhooks at-least-once, not exactly-once — must be deduplicated
- Order across event types is not guaranteed (manifest may arrive after dispatch in degenerate cases)
- Order within a single event type for a single entity is *typically* preserved but not contractually so
- We design every flow to tolerate out-of-order delivery: precondition checks queue or defer rather than fail
- Out-of-order handling: if a downstream event arrives before its precondition, the Queue Job loops with bounded retries (max 5 attempts × 30s back-off) waiting for the precondition; if exhausted, raises an Integration Discrepancy
- Duplicate webhooks deduplicated by Webhook Event composite key (event_type, ee_event_id, company); duplicate insert returns the existing record's name and re-runs processing only if state=Pending

## 6.7 The contract for Frappe document events

Several flows are triggered by Frappe document events (on_submit on Sales Order pushes the SO, on_submit on Sales Invoice pushes the B2B invoice, etc.). The contract these hooks honour:

- All hooks run in their own database transaction; failure of the integration enqueue does NOT block document submission (unless Settings.push_so_block_on_error is True for that flow)
- Hook function naming: ecommerce_super.hooks.{doctype_lower}.{event_name}() (e.g., sales_order.on_submit_push_to_easyecom)
- Hook produces a Queue Job with state=Queued; never makes synchronous EE API calls during the document save transaction
- Hook captures correlation_id at entry; stores on the document via ecs_correlation_id field
- Hook is idempotent: re-running on already-pushed document detects existing Sync Record and no-ops

# 7. The Integration Contract (the base every API and webhook obeys)

Sections 1-7 establish the connection, the data model, the logging records, the queue, and idempotency. This section ties them into a single enforced contract: a fixed set of rules that **every** API call, pull, push, and webhook handler must follow, regardless of which endpoint it touches. The flows in Sections 8-13 and every future per-API integration are implementations of this contract — they choose endpoints and field mappings, but they do not get to invent their own success/failure, logging, surfacing, or retry behaviour. The point is uniformity: when something fails, it fails the same way and shows up in the same place whether it was a product pull, an order pull, a GRN webhook, or a customer push.

The contract has no exemptions. It governs every interaction with EasyEcom, not only the business flows. Generating a JWT, pulling the location list, testing the connection, renewing a token on day 85 — all of these go through the EasyEcomClient and obey the logging, correlation, surfacing, and disposition rules below. There is no "too low-level to log" call. The only distinction the contract draws is between **entity-sync work** (the common case — pulling/pushing items, orders, GRNs, etc., which is company-scoped and produces per-entity Sync Records) and **foundational work** (token, location discovery, connection test — which is account-scoped and produces no per-entity Sync Record). Both are fully logged and surfaced; they differ only in scope and in which records they create. Section 7.7 defines the foundational class precisely, including the one genuine bootstrap dependency.

## 7.1 The unit of work is the record, not the batch

This is the central rule and it answers the question directly: when a pull or push touches many entities and some succeed while others fail, the individual entities fail — the batch does not fail as a whole.

Concretely, using the example of pulling 100 product masters where 2 cannot be created in ERPNext:

- The batch is the **transport**, not the **transaction**. Pulling 100 items is one API interaction (possibly several pages), logged as API Call rows. But each of the 100 items is processed as its own unit of work with its own Sync Record.
- 98 items create/update successfully — 98 Sync Records reach state Success.
- 2 items fail (bad data, unmapped category, validation error) — 2 Sync Records reach state Failed, each carrying its own last_error and matched Error Translation. The other 98 are unaffected and are not rolled back.
- The cursor advances. A per-record failure does not pin the cursor or re-pull the whole page next cycle. The 2 failed records are tracked by their Failed Sync Records, not by replaying the batch.
- The job that ran the batch lands in a **partial-completion** state, not a blanket Failed (see 7.3), and reports the count: 98 succeeded, 2 failed.

The only failures that stop a whole batch are **transport-level**: authentication failure (401), rate-limit exhaustion (429) with no remaining quota, a 5xx, or a network failure — i.e., the batch could not be fetched or sent at all. In that case nothing was processed, the cursor does not advance, and the job retries the transport per Section 6.3.8. Once the transport succeeds, per-record processing resumes under the per-record rule above.

Per-record isolation is mandatory: a handler processing a batch wraps each record in its own savepoint, so one record's exception can never abort its siblings or the surrounding transaction. A record that raises is caught, its Sync Record is marked Failed, and processing continues to the next record.

### 7.1.1 What "one record" means — the atomic document, and per-line detail for nested documents

"The record" is **one atomic ERPNext document** — the thing that succeeds or fails as a unit in ERPNext. This is *declared per flow* in its Section 7.6 declaration; it is never inferred at runtime. The granularity differs by flow because the target documents differ:

- **Independent-entity pulls** (Item, Customer, Supplier — Section 8): each source entity becomes its own ERPNext document, so each is its own unit of work. Pulling 100 products → 100 Items → **100 Sync Records**; per-record isolation means 2 can fail while 98 succeed (the worked example above).
- **Composite documents with nested lines** (GRN→Purchase Receipt in Section 9, Order→Sales Invoice in Section 12, Return→Credit Note/Return in Section 13): the *document* is the atomic unit, because ERPNext cannot half-create it — a Purchase Receipt with one bad line creates zero lines, not nine. So one GRN with 10 SKUs → one Purchase Receipt → **one Sync Record**, not ten. The 10 SKUs are lines of one unit, not ten units.

**But a document-grained Sync Record for a nested document must still carry per-line detail.** "One Sync Record" must not mean "no line-level visibility." For any flow whose source payload has nested lines (GRN, Order, Return — anything with a child array), the Sync Record carries a **child table of line outcomes** (`EasyEcom Sync Record Line`), one row per source line, each recording: the source line identifier (EE SKU / line ref), the mapped ERPNext target (e.g. item_code), the line **status** (OK / Failed / Discrepancy), and a reason when not OK. The two failure kinds resolve as follows:

- **A line problem that blocks document creation** (e.g. an unmapped SKU — ERPNext cannot create the Purchase Receipt at all): the whole Sync Record is **Failed**, no document is created, and the child table (or, where it cannot be persisted because the parent failed pre-insert, the `last_error`) names the offending line(s) — so the FDE sees it was line 7 of 10, not just "GRN failed."
- **A line reconciliation variance** (e.g. the Purchase Receipt's line 7 received quantity or tax doesn't match what was expected, beyond tolerance): this is still a **Failed** Sync Record, not a partial success. The document creation is rolled back — a Failed unit of work must not leave a posted document in the books — the line-7 child row is marked **Discrepancy**, the offending line is named, and an Integration Discrepancy (Section 23) is raised for tracking to closure. The FDE investigates the variance, fixes the cause, and retries. The per-record outcome is binary (Section 7.3): a discrepancy on any line is a failure of the whole unit of work, never a "completed-but-flagged" state — this keeps the failure on the FDE's worklist rather than hiding behind a Success.

This makes line-level outcomes **queryable and structural** ("show all GRN lines that failed / are in discrepancy across all GRNs"), not buried in error text. Single-entity flows (Item/Customer/Supplier) have no nested lines and therefore no line child table — their Sync Record is the whole story.

Implementation note: the `EasyEcom Sync Record Line` child table is a schema addition to the existing EasyEcom Sync Record DocType (Section 31.2.3). It is built when the first line-heavy flow is built (Section 9, GRN — Master Sync in Section 8 is single-entity and does not need it), and populated only by flows whose Section 7.6 declaration specifies nested lines.

## 7.2 Mandatory logging and correlation on every interaction

Every API call and every webhook obeys this logging contract without exception. A per-API integration does not get to skip it; the EasyEcomClient and the webhook receiver enforce it centrally so individual flows cannot forget.

- **Every outbound HTTP call** — success or failure — writes exactly one EasyEcom API Call row (endpoint, verb, request, response, status, latency, attempt number, correlation_id, parent Sync Record if any). Append-only.
- **Every inbound webhook** writes exactly one EasyEcom Webhook Event row (raw payload, token auth result, dedup key, processing result, downstream documents). Append-only.
- **Every entity processed** creates or updates exactly one EasyEcom Sync Record for that (entity, direction). Updated in place across retries.
- **Every async unit of work** is an EasyEcom Queue Job, which may spawn multiple API Call rows across its retries.
- **One correlation_id threads all of them.** It is minted at the entry point (poll tick, webhook receipt, document-event hook, scheduled token-renewal run, or a manual action such as Test Connection / Sync Now), stored on the Queue Job, copied onto every API Call and Sync Record the work produces, and written to the originating ERPNext document's ecs_correlation_id field where one exists. Given any one record, the FDE can pivot to all others sharing the correlation_id — this is what makes the Event Timeline (Section 17.8) and Inspector (Section 17.9) complete rather than partial. Foundational calls (Section 7.7) carry a correlation_id too, even though they have no per-entity Sync Record or originating business document.

If logging itself fails (e.g., the API Call row cannot be written), that is an audit event raised to the FDE, not a silently swallowed error.

## 7.3 The state machine every unit of work shares

Two distinct state machines, deliberately not conflated:

**Per-record (EasyEcom Sync Record):** Not Synced → Pending → In Progress → Success | Failed. The per-record outcome is binary: either the unit of work completed and reconciled cleanly (Success), or it did not (Failed). A Failed record is terminal until an explicit Retry (manual or scheduled), which returns it to Pending with the attempts counter preserved. A line-level problem of any kind — one that blocks document creation, OR a reconciliation variance on a line (e.g. a quantity or tax mismatch beyond tolerance) — makes the whole Sync Record **Failed**: the target document is not left standing (creation is rolled back so the books never hold a document the integration considers failed), the offending line is named, and an Integration Discrepancy is raised (Section 23) for tracking. There is no "partially done" per-record state; a discrepancy is a failure of that unit of work, fixed-and-retried like any other failure.

**Per-job (EasyEcom Queue Job):** Queued → Running → Success | Partial | Failed | Retrying | Cancelled. The addition the contract requires is **Partial**: a job whose batch transport succeeded but where some per-record units failed. A Partial job records succeeded_count and failed_count and links to the Failed Sync Records it produced. Partial is not retried wholesale (that would re-process the 98 good records); instead its failed children are retried individually.

The interlock — closing the edge cases:

- Batch transport fails entirely → Queue Job Failed, no Sync Records advanced, cursor held, transport retried.
- All records succeed → Queue Job Success.
- Some records fail → Queue Job Partial; each failed record has a Failed Sync Record; cursor advances.
- A single-entity job (e.g., one push) whose record fails → Queue Job Failed and Sync Record Failed coincide (batch of one).
- A record fails its final retry → Sync Record stays Failed and an Integration Discrepancy is raised if there is financial or operational impact (per Section 23); pure transient exhaustion without impact stays a Failed Sync Record surfaced in the queue without a Discrepancy.

## 7.4 How the user finds out (the surfacing contract)

A failure that no one sees is the real failure. Every Failed or Partial outcome surfaces through the same channels, so the FDE's experience is identical across APIs:

- **Counts on the Workspace** (Section 17.3 Number Cards): Failed Sync Records, Partial Jobs, open Integration Discrepancies — always visible, per Company.
- **The Sync Record list**, filterable to status=Failed, is the worklist. Each row shows entity, direction, last_error (plain-English via Error Translation), and attempts.
- **A Partial job** shows "98 succeeded, 2 failed" with a direct link to the 2 Failed Sync Records — the FDE never has to diff the batch to find what failed.
- **Alerts** (Section 18) fire by severity: a per-record failure with financial impact pages per the Company's alert routing; a cluster of failures (error-rate threshold) escalates; a transport-level outage surfaces the Connection Down banner.
- **The Event Timeline and Inspector** (Sections 17.8-18.9) let the FDE see the full history of any single failed entity end to end.
- **Nothing fails silently.** The contract forbids a handler from catching an exception and moving on without leaving a Failed Sync Record. "Continue to the next record" always means "mark this one Failed and visible, then continue" — never "swallow and forget."

Answering the order example directly: when an order pull gets some orders through and some fail, the successful orders produce Sales Invoices as normal, the failed orders produce Failed Sync Records that appear in the Workspace Failed count and the Sync Record worklist with a plain-English reason, the job shows as Partial with the success/fail split, and if any failed order carries financial impact an alert routes to the assigned FDE. The user comes to know through the Workspace count and (for impactful failures) an alert; the action is to open the Failed Sync Record, read the translated error, fix the cause, and Retry — individually or in bulk.

## 7.5 What action gets taken (the disposition contract)

For every failed unit of work there is a defined disposition — the framework never leaves a failure in an ambiguous state:

- **Transient transport failures** (429/5xx/network) are retried automatically with back-off per Section 6.3.8, up to max_attempts, before the unit is surfaced as Failed.
- **Per-record data failures** (validation, unmapped reference, bad payload) are not retried blindly — retrying identical bad data just fails again. They land Failed immediately with a translated reason and wait for FDE action.
- **FDE actions** are the standard menu (Section 17.6): Retry Now, Retry With Override (correct the data then retry), Mark as Already Synced (the FDE fixed it on the EE side), Force Resync, or Create Replay Plan for bulk remediation (Section 19).
- **Recurring / bulk failures** (e.g., all items in a category failing for the same reason) are remediated once via a Replay Plan with a dry-run preview rather than clicking Retry on each.
- **Impactful failures** additionally raise an Integration Discrepancy (Section 23) that tracks resolution to closure, so a financially-material failure cannot be quietly retried-and-forgotten without an audit trail.

## 7.6 What a per-API integration must declare

Because the contract above is fixed, adding a new API is reduced to declaring a small, uniform set of facts — and nothing else. Every per-API section (Sections 8-13 today, and each future endpoint) provides exactly:

- Endpoint(s), HTTP verb, and direction (pull / push / webhook)
- Trigger (cron cadence, document event, or webhook type) and whether it is WMS-gated (is_wms_location) or otherwise conditional
- Auth scope: which location_key's JWT the call uses, and the resolved Company
- Pagination style (next-page-URL cursor for bulk reads) and the cursor field advanced
- The Field Mapping ruleset (Section 5) that translates the payload
- The unit of work (what one record is) and the idempotency key for it (Section 6.1). Per Section 7.1.1, state whether the unit is a single entity or a **composite document with nested lines** (GRN, Order, Return); if nested, the flow populates the EasyEcom Sync Record Line child table with per-line outcomes
- The target ERPNext DocType and the create/update semantics
- Flow-specific failure modes beyond the standard ones, if any

Everything else — logging, correlation, the state machine, per-record isolation, surfacing, retry, alerting, the Timeline and Inspector — is inherited from this contract and must not be re-specified or varied per API.

## 7.7 Foundational calls and the bootstrap order

A small set of calls exist below the business flows: they establish or describe the connection itself rather than syncing a business entity. These are token acquisition (/access/token), scheduled token renewal (day 85), location discovery (`/getAllLocation`), and connection test. They obey the full contract — logged as API Call rows, carry a correlation_id, surfaced in the Timeline and Connection Health, retried and alerted on failure — but they differ from entity-sync work in two defined ways:

- **They are account-scoped, not company-scoped.** A token or location-discovery call belongs to the EasyEcom Account, not to any one Company. Where the contract elsewhere requires a resolved Company on a record, foundational API Call rows record the Account and (where applicable) the location_key, with company left blank. This is the single legitimate exception to "every operational record carries a company"; it applies only to this foundational class.
- **They produce no per-entity Sync Record.** There is no "entity" being synced — there is no Item or Order whose state is being tracked. Their success/failure lives on the API Call row (and, for token renewal, on the EasyEcom Location's JWT fields and connection_status). The per-record state machine of 7.3 does not apply; the API Call's own status is the record of what happened.

The two foundational calls that the rest of the contract depends on have a defined **bootstrap order**, because they cannot themselves rely on what they are about to create:

1. **Token acquisition is the first call.** Every other call needs a JWT, so token acquisition cannot itself present a prior JWT — it authenticates with x-api-key + email + password + location_key and returns the JWT. It is still logged as an API Call (with the credentials redacted per Section 3.7) and still throttled under the rate-limit tier. It is the one call whose own precondition is only the account credentials, not a session.

2. **Location discovery is what creates the company-resolution data**, so it cannot resolve company the normal way. The `/getAllLocation` pull returns the account's locations; the integration uses it to create or update EasyEcom Location records. Until the FDE has set is_operational and frappe_company on those records (onboarding), the locations resolve to no Company — and that is correct, not an error. Location discovery therefore runs account-scoped: it writes its API Call row against the Account, creates/updates EasyEcom Location rows as its output, and does not attempt company resolution on itself. Company resolution (the location → frappe_company rule the rest of the contract relies on) only becomes meaningful after these records exist and are mapped.

The practical bootstrap sequence at onboarding, therefore: configure the EasyEcom Account credentials → Test Connection (acquires a token for the primary location) → pull locations (`/getAllLocation`) → FDE flags each location primary/operational and maps frappe_company and warehouse → from this point every subsequent call has both a JWT to present and a location→company map to resolve against, and the entity-sync portion of the contract applies in full.

# Part III — The Integrations

*Each integration below is an implementation of the Integration Contract (Section 7): it declares its endpoints, mapping, unit of work, and idempotency key, and inherits everything else.*

# 8. Master Sync

**Build-order prerequisite.** This section and every flow section that follows (Sections 9-13) is an *implementation* of the Integration Contract (Section 7) — not a standalone build. Per the contract and the phasing plan (Section 29.2), the foundation (connection, the log/queue DocTypes, the EasyEcomClient) and the integration contract itself are built and tested first, against the token and location-discovery calls, before any master or flow in Sections 8-13 is started. Each section below declares only its endpoints, mapping, unit-of-work, and idempotency key (the list in Section 7.6); it inherits all logging, correlation, per-record isolation, the Partial/Failed state machine, surfacing, retry, and disposition from the contract. Do not build a flow before the contract exists, or its failure-handling will be re-invented inconsistently.

Master data — Items, Customers, Suppliers, Warehouses, Tax Categories, Channels — is the foundation of every operational flow. If a Sales Order references an Item that doesn't exist in EasyEcom, the order push fails. If a GRN comes back from EasyEcom referencing a Vendor not in ERPNext, the Purchase Receipt cannot be created. Master sync must be reliable, deterministic, and bidirectional with clear conflict resolution rules.

> **Build order (read before building).** Section 8 is built as a sequence of small, independently-deliverable packets — one master at a time — ordered by *dependency*, not by the subsection numbering below. The subsection numbers (8.1 Item, 8.2 Customer, …) are kept stable because the rest of the spec cross-references them; they are NOT the build order. The build order is:
>
> 1. **Location** (§8.4's location-discovery half) — the resolution substrate: nothing resolves to a Company until locations are discovered and mapped. Pull + FDE map, never pushed.
> 2. **Channel** (§8.6) — the flat Marketplace list pull.
> 3. **Tax Category** (§8.5) — precondition for Item (HSN → Item Tax Template must exist before an Item can sync with correct tax).
> 4. **Item / Product master** (§8.1) — the first hard, bidirectional master; builds the reusable per-record savepoint helper (the Section 7.1 isolation obligation) — though that helper is in fact built earlier, with Location, so every packet from Location onward uses it.
> 5. **Customer master** (§8.2).
> 6. **Supplier / Vendor master** (§8.3).
>
> Lookup tables (§8.7) are folded into whichever master first needs them, not built as a standalone packet. The Warehouse **Source-of-Truth Map** (§8.4.1) is *not* part of the Location packet — it concerns inventory authority and is deferred to the buying/stock flows (Sections 9-10) where stock actually moves. Each packet is built, tested, and signed off on its own; later packets reuse the patterns (savepoint isolation, discovery-pull, the FDE workflow) the earlier ones establish.

Each master gets the same treatment in this section: direction of truth, field-level mapping, conflict resolution, account-global sharing across Companies, edge cases.

Before any individual master, Section 8.0 recaps the Field Mapping engine in the context of master sync. The full engine specification is Section 5 (part of the foundation); what follows here is the short conceptual reminder needed to read Sections 8.1 through 8.7 without flipping back.

> **Open decisions to resolve before building Section 8.** The following points were raised during review and are deliberately left open until this section is reached, so they are decided with the master-sync flow in front of us rather than in the abstract. Do not begin the Section 8 build until each is resolved and this block is replaced with the agreed design.
>
> 1. **Different-identifier mapping (same item, different SKU on each side).** The initial-sync and steady-state logic below matches by item_code (SKU). A real and common case is that the *same* item carries a *different* SKU/identifier in ERPNext than in EasyEcom, yet must be mapped to one record — not duplicated. The current match-by-SKU logic would miss these, then the create-the-missing-ones step would create a duplicate on each side. Section 8 needs an explicit **assisted/manual mapping step**: where auto-match by identifier fails, the FDE maps an ERPNext record to the existing EE record by hand (or via an alias / alternate-identifier table) *before* the create-missing step runs. This applies to Item, Customer, and Supplier alike. **Decide:** the matching ladder (auto-match → assisted map → create → conflict) and where the alias/alternate-identifier mapping is stored.
>
> 2. **Create vs. workflow-gated creation for records that exist on only one side.** Section 8.1.3 currently *auto-creates* an EE-only item into ERPNext with sensible defaults and flags it for FDE review after the fact. An alternative is a **workflow-gated "pending creation" queue** the FDE approves *before* the record is created in the books — safer, because an auto-created item with missing HSN/GST can otherwise flow into a transaction before review. **Decide:** auto-create-then-review (faster onboarding, riskier) vs. workflow-gated creation (safer, more onboarding effort), per entity. Applies to Item, Customer, Supplier.
>
> 3. **Mandatory-field gate, both directions.** EasyEcom's product/customer/vendor masters have mandatory fields; so do ERPNext's Item/Customer/Supplier. A record cannot be created on the receiving side until its mandatory fields are satisfiable from the source plus defaults. **Decide:** the explicit list of EE-side mandatory fields a push must supply, the ERPNext-side mandatory fields a pull must supply, and what happens when a mandatory field cannot be filled (route to the review workflow from point 2 — never silently default a tax-relevant field such as HSN/GST). The field-level translation itself lives in the Field Mapping engine (Section 5); what Section 8 must add is the mandatory-field *gate* that blocks creation when the contract is unmet.
>
> These three apply across Item (8.1), Customer (8.2), and Supplier (8.3); resolve them once here and apply consistently to all three masters.
>
> 4. **Reconcile the shipped Field Mapping rulesets against real EasyEcom payloads (findings from §5 Test Mapping).** The §5.11 rulesets were written from this spec's *assumed* EE payload shapes; running a real `/Products/GetProductMaster` response through the engine surfaced two concrete gaps that §8 must fix when it builds and tunes the Item sync (and the same exercise must be repeated for Customer/Supplier/etc. against their real payloads):
>   - **Natural key is `sku`, not `item_code`.** EE's product payload supplies the SKU as the field **`sku`** (e.g. `"sku": "mob000"`), alongside the EE master IDs `cp_id` and `product_id` — these are three distinct fields. This spec frequently writes "item_code (SKU)" as if interchangeable (e.g. §8.1, §8.1.3); the EE-side path that maps to ERPNext `item_code` on pull is **`sku`**. The Item-Sync ruleset's `easyecom_path` for the item_code rule must read `sku`. The EE master IDs `cp_id`/`product_id` map to `ecs_easyecom_company_product_id` (the mapping/link field), not to `item_code`.
>   - **EE supplies no stock UOM, but ERPNext requires one.** The `GetProductMaster` payload has `size`, `weight`, dimensions, and `accounting_unit` (an accounting code like "111", *not* a UOM), but **no unit-of-measure field**. ERPNext mandates `stock_uom` on every Item. **Decide** the pull-side UOM strategy: a default UOM (e.g. "Nos") applied during the pull, FDE-configurable per client, since it cannot come from EE. This is why a required-`stock_uom` rule on a pull ruleset will (correctly) fail against raw EE data — the engine is right; the ruleset/flow must supply the default.
>   - A real `GetProductMaster` payload sample (multiple product_types: normal_product, variant_parent, combo_product with sub_products) was captured during §5 testing and should be used as the §8 Item-sync test fixture and as the reference for finalising the ruleset paths.

## 8.0 The Field Mapping engine (recap in context)

Every master sync flow translates between an ERPNext document shape and an EasyEcom payload shape, and it does so through the Field Mapping engine specified in Section 5 — declarative, FDE-editable rules shipped as fixtures, rather than translations hardcoded in Python (which would make every payload tweak a code deploy).

> **Policy — every EasyEcom-payload mapping goes through the Field Mapping engine (no exceptions for "simple" flows).** The engine is not only for mappings that vary per client; it is the **insurance layer against EasyEcom changing their API**. EasyEcom is a third party and can rename or restructure a payload field on their own schedule. If that mapping lives in the engine, an FDE fixes the renamed path in the desk (Test Mapping → version → done) in minutes; if it lives in hardcoded Python, the same change is a developer code change + deploy + release cycle, with a flow broken in the meantime. Therefore **even flat, invariant, one-to-one pulls (Location, Channel, Tax Category) map through a ruleset**, not a hardcoded mapper — the flatness of today's payload is no guarantee of tomorrow's. The boundary is: **the engine owns the payload→field *translation* (including small derivations, expressed as ruleset transforms); the flow code owns *orchestration*** (the upsert, the workflow state a new row lands in, the Sync Record, foundational-call logging, re-pull behaviour). A flow calls the engine to translate the payload, then orchestrates around the result. Hardcoding a payload mapping in flow code is not permitted; if the engine genuinely cannot express something, flag it rather than working around it.

### 8.0.1 Why path-based, not flat

EasyEcom payloads are nested. An order has line items. Line items have tax components. Tax components have CGST/SGST/IGST/cess shares. A flat (source_field → target_field) table cannot express:

- orders[].items[].tax_components[].amount where component_type='CGST' on the EasyEcom side maps to items[].cgst_amount on the ERPNext side
- Sum of orders[].items[].tax_components[].amount where component_type IN ('CGST','SGST','IGST') equals the ERPNext line tax total
- orders[].marketplace_meta.amazon_order_id on EE side maps to ecs_marketplace_order_id on ERPNext side, but only when channel='Amazon'
The engine works on JSON paths (a subset of JSONPath syntax) on both sides, with conditional rules and computed fields. Section 5 specifies the path syntax, the operator vocabulary, and the conflict resolution between rules.

### 8.0.2 Convention over configuration

A naive design would require a rule for every field. Most fields don't need one — item_code on the ERPNext side maps to item_code on the EE side with no transform. The engine handles these implicitly:

- Strict mode (default for client-facing data): only rules-listed paths are translated; all others are dropped or raise an error per the rule set's missing_field_policy
- Permissive mode (default for masters with stable shape): rules-listed paths plus same-name paths on both sides translate identically; only differences need rules
Each Field Mapping ruleset declares its mode explicitly. A 'Show Computed Mapping' action in the UI expands the implicit identity matches so what's actually happening is inspectable.

### 8.0.3 Bidirectional declaration

A single Field Mapping ruleset declares both push (ERPNext→EE) and pull (EE→ERPNext) translations as paired rules:

```
# Conceptual rule shape (Section 5 has the formal spec)
- erpnext_path: items[].cgst_amount
  easyecom_path: items[].tax_components[?type='CGST'].amount
  direction: bidirectional
  push_transform: identity
  pull_transform: identity

- erpnext_path: gst_hsn_code
  easyecom_path: hsnCode
  direction: bidirectional
  pull_transform: validate_against_erpnext_hsn_master  # raises if no match

- erpnext_path: has_batch_no
  easyecom_path: batchRequired
  direction: bidirectional
  push_transform: bool_to_yn   # True/False → "Y"/"N"
  pull_transform: yn_to_bool   # "Y"/"N" → True/False
```

Bidirectional declaration prevents the classic two-table drift where the push and pull rules for the same field diverge over time.

### 8.0.4 Where this affects Sections 8.1 through 8.7

Each master sub-section below references its corresponding Field Mapping ruleset by name. Items use the EasyEcom-Item-Sync ruleset, Customers use EasyEcom-Customer-Sync, and so on. The ownership matrices in those sub-sections (e.g., 4.1.2) are still authoritative for which side wins on conflicts; the Field Mapping ruleset is the implementation mechanism that performs the translation. The two work together: ownership defines policy, mapping ruleset defines mechanism.

## 8.1 Item / Product master

### 8.1.1 Direction of truth

Items are bidirectional. An Item may be born on either side:

- Born in ERPNext: a B2B SKU configured by the procurement team, a private-label product that won't sell on marketplaces, an internal-use Item
- Born in EasyEcom: a marketplace listing created by the catalog team in EasyEcom and now needs a financial counterpart for accounting purposes
The integration handles both directions and reconciles continuously.

### 8.1.2 Field-level ownership matrix

Even with bidirectional sync, individual fields are owned by one side. The owning side is authoritative; the non-owning side mirrors. This prevents the classic distributed-systems write-conflict problem.

| Field | Owner | Rationale |
| --- | --- | --- |
| item_code (SKU) | Either (whichever creates first) | The natural key. Once set on either side, it is stable forever. |
| item_name | ERPNext | Books-side description; can be edited freely in ERPNext |
| description | ERPNext | Same — descriptive copy |
| item_group | ERPNext | Inventory categorisation belongs to the books |
| brand | ERPNext | Same |
| uom (stock_uom) | ERPNext | UoM is a books concept; EE adapts via UoM mapping |
| is_stock_item | ERPNext | Books-side classification |
| valuation_method (FIFO/Moving Avg) | ERPNext | Books-side accounting choice |
| hsn_code (gst_hsn_code) | ERPNext | GST classification — owned by books per India Compliance |
| gst_rate | ERPNext | Tax treatment — books authority |
| item_tax_template | ERPNext | Tax template selection |
| weight_per_unit + weight_uom | EasyEcom | Operational dimensions, used for shipping calculations |
| dimensions (LxWxH) | EasyEcom | Operational dimensions |
| barcode | Either | Set wherever convenient; sync mirrors |
| marketplace_skus (table) | EasyEcom | Per-marketplace listing IDs only exist in EE |
| company_product_id (EE master ID) | EasyEcom | EE-issued; mirrored to ERPNext custom field ecs_easyecom_company_product_id |
| mrp | EasyEcom | Operational pricing for marketplace display |
| selling_price (default) | ERPNext | Books-side pricing; pushed to EE as a starting point |
| batch_tracking_enabled | ERPNext | Books-side accounting choice |
| serial_tracking_enabled | ERPNext | Same |
| expiry_tracking_enabled | ERPNext | Same |
| item_image | ERPNext | Stored locally; URL pushed to EE |
| disabled | ERPNext | Books-side decision; if disabled, EE syncs to inactive but does not delete |

### 8.1.3 Initial sync (onboarding)

During FDE-led onboarding, the existing Item populations on both sides are reconciled:

1. Pull all EasyEcom master products via /Products/GetProductMaster
1. Pull all ERPNext Items where item_group is in the marketplace-relevant groups (configurable)
1. Match by item_code (SKU). Matched: link via ecs_easyecom_company_product_id custom field
1. Items only in EasyEcom: create in ERPNext with EE-owned fields populated, ERPNext-owned fields filled with sensible defaults; flag for FDE review (especially HSN code, GST rate)
1. Items only in ERPNext: create in EasyEcom via POST /Products/CreateMasterProduct with mapped fields
1. Conflicts (same SKU exists on both sides but with mismatched fields): produce an Integration Discrepancy of type Master Mismatch — Item; FDE resolves manually before steady-state sync starts
Initial sync is run once during onboarding. After that, steady-state delta sync takes over.

### 8.1.4 Steady-state delta sync

- New Item created in ERPNext → on insert, push to EE via POST /Products/CreateMasterProduct (async, EasyEcom Queue Job)
- Item updated in ERPNext (any ERPNext-owned field changed) → push to EE via POST /Products/UpdateMasterProduct
- Updates only push the ERPNext-owned fields — never the EE-owned fields, even if the local copy looks stale
- New Item created in EasyEcom → detected via daily /Products/GetProductMaster pull (cursor-based, last_pull_master_products); created in ERPNext
- Item updated in EasyEcom → same pull mechanism detects the change; only EE-owned fields are updated locally
- EE update of an ERPNext-owned field is a conflict — see 4.1.5

### 8.1.5 Conflict resolution

Conflicts arise when the same field is changed on both sides between syncs. Resolution rules:

- If the field is owned by one side per the matrix: the non-owning side's change is rejected and an Integration Discrepancy is raised. The owning side wins.
- If the field is Either-owned: last-write-wins, with the timestamp comparison from each system's modified field. The losing change is logged.
- If the conflict cannot be auto-resolved: an Integration Discrepancy of severity Warning is raised; the FDE reviews.

### 8.1.6 Account-global masters (sharing across Companies)

- Items are master data and are account-global: they live at the primary location in EasyEcom and are synced once, not per operational Company.
- In ERPNext the Item is shared across Companies via the standard Item Defaults table — one Item, multiple Company-specific defaults (income account, expense account, default warehouse). The integration does not duplicate the Item per Company.
- Because masters are maintained at the primary location, there is a single EasyEcom master record per SKU for the whole account, not one per Company.
- The mapping is stored in a child table on the Item, ecs_easyecom_mappings, keyed simply by easyecom_company_product_id (the EE master product identifier). Operational locations reference the same master; no per-(Company, location) master row is created.

### 8.1.7 Variants and bundles

- Item Variants in ERPNext (templates and variants) map to EasyEcom's variant model with the template as the master_product and each variant as a separate company_product
- Item Bundles (Product Bundle in ERPNext) map to EasyEcom Combo SKUs — only the Combo gets a company_product_id; the individual components are tracked but not separately listed
- Bundle composition changes are non-trivial — propagate carefully, with FDE review on first sync of each bundle change

### 8.1.8 Edge cases

- Item disabled in ERPNext: EE record set to inactive (not deleted)
- Item deleted in ERPNext: forbidden if any operational document references it; the integration enforces this independently as a safety check before the EE call
- EE archives an Item: detected on daily pull; ERPNext Item is set to disabled, not deleted, with a note in the Item's comments
- HSN code missing on a marketplace-relevant Item: blocks push to EE until populated; FDE alerted via dashboard

## 8.2 Customer master

### 8.2.1 Two distinct customer populations

Customer data must be modelled in two distinct populations because they have entirely different identity and privacy semantics:

- **B2B and direct-D2C customers** — real Customer records in ERPNext with name, GSTIN, billing/shipping addresses. These sync bidirectionally with EE's Wholesale Customer model. Privacy-sensitive but needed for invoicing.
- **Marketplace-anonymous customers** — orders from Amazon, Flipkart, Myntra etc. arrive with anonymised buyer identifiers (e.g., Amazon's encoded buyer email). These do NOT become real Customer records. They are mapped to a Marketplace Anonymous Customer record per (marketplace, marketplace_customer_id), and the actual Sales Invoice is raised against a single per-marketplace pseudo-customer (e.g., Amazon FBA Buyer Pool).
The reasoning: GST invoice rules require a customer for the bill, but marketplace privacy rules forbid us from holding the real buyer's identity. Indian GST has a B2C-large vs B2C-small distinction; marketplace orders are typically B2C-small and do not require buyer GSTIN. The pseudo-customer pattern preserves auditability without holding regulated data.

### 8.2.2 Direction of truth (B2B / D2C)

- Direction is bidirectional, ERPNext-leaning. Most B2B customers are entered first in ERPNext (CRM, sales process)
- Where EE has a Wholesale Customer record that ERPNext doesn't: pulled into ERPNext on daily customer sync
- Where ERPNext has a Customer that EE doesn't: pushed via POST /Wholesale/createCustomer
- EasyEcom's CreateCustomerMaster operation requires email and password fields in the request body; EasyEcom uses these for reference and to create a unique entity on its side. These are properties of the customer record being created in EasyEcom, not the integration's API credentials

### 8.2.3 Field-level ownership matrix (B2B / D2C)

| Field | Owner | Notes |
| --- | --- | --- |
| customer_name | ERPNext | Books-side legal name |
| customer_type (Individual/Company) | ERPNext |  |
| gstin | ERPNext | Tax identity |
| pan | ERPNext |  |
| billing_address | ERPNext | Linked Address record |
| shipping_addresses (table) | Either | Operational addresses; EE may add new shipping addresses for delivery |
| contacts (phone, email) | ERPNext |  |
| customer_group | ERPNext | Books-side categorisation |
| territory | ERPNext |  |
| credit_limit | ERPNext | Books-side risk decision |
| payment_terms | ERPNext |  |
| disabled | ERPNext |  |
| ecs_easyecom_customer_id | EasyEcom | EE-issued; mirrored to ERPNext custom field |

### 8.2.4 Marketplace anonymous handling

- Per-marketplace pseudo-customer: Amazon FBA Buyer Pool, Flipkart Buyer Pool, Myntra Buyer Pool, etc.
- Created during onboarding by the FDE, one per Marketplace per Company
- Sales Invoices for marketplace B2C orders are raised against this pseudo-customer
- Marketplace Anonymous Customer DocType (separate from Customer) holds the (marketplace, marketplace_customer_id, opaque_buyer_hash) mapping for traceability
- If the same anonymous identifier reappears on multiple orders (returning buyer), it can be linked, but not promoted to a real Customer

### 8.2.5 Availability of buyer PII from EasyEcom

Whether any buyer PII (name, mobile, address) arrives with an order at all depends on two factors outside the integration's control, and the pseudo-customer model is designed so the flow works regardless:

- **API user PII access:** EasyEcom only returns PII fields if the API user whose JWT we use has PII Access enabled in EasyEcom account settings. If our integration receives orders with PII fields blank, the first thing the FDE checks is whether the API user has PII Access turned on.
- **Channel-level PII suppression:** some marketplace channels never provide buyer PII regardless of API-user access — Amazon Easyship and Flipkart are known cases. For these channels, blank PII is expected and permanent, not a misconfiguration.

Because PII may be absent for either reason, the integration never depends on buyer PII to post a Sales Invoice. The per-marketplace pseudo-customer is always sufficient; any PII that does arrive is recorded on the Marketplace Anonymous Customer record for traceability but is never required for the financial flow. CreateCustomerMaster-style operations that require buyer identity are out of scope for marketplace B2C.

## 8.3 Supplier / Vendor master

### 8.3.1 Direction of truth

- Suppliers are largely owned by ERPNext — procurement is a books-driven activity
- Push to EasyEcom is required so PO push (Section 9) works — EE needs to know about the Vendor before it can accept a PO referencing it
- Bidirectional but ERPNext-dominant: new Vendors enter through ERPNext, existing-only-in-EE Vendors get pulled in during onboarding for completeness, then ERPNext owns going forward

### 8.3.2 Field-level mapping

| Field | Owner | Notes |
| --- | --- | --- |
| supplier_name | ERPNext |  |
| supplier_type | ERPNext | Individual/Company |
| gstin | ERPNext | Critical for ITC on inward GRN |
| pan | ERPNext |  |
| addresses, contacts | ERPNext |  |
| payment_terms | ERPNext |  |
| supplier_group | ERPNext |  |
| disabled | ERPNext |  |
| ecs_easyecom_vendor_id | EasyEcom | EE-issued; mirrored as custom field |

### 8.3.3 Account-global sharing

- In ERPNext, Supplier is shared across Companies but Supplier Defaults can be Company-specific
- Suppliers are master data maintained at the primary location; there is one EasyEcom vendor record per supplier for the whole account, not one per Company
- Mapping table on Supplier: ecs_easyecom_mappings keyed by easyecom_vendor_id (the EE master vendor identifier)

## 8.4 Warehouse / Location master

This section covers two distinct things that were historically bundled and are now separated:

- **Location discovery and mapping (§8.4.1)** — pulling the account's locations from EasyEcom and mapping each to a Frappe Company and warehouse. This is the *resolution substrate* (the first thing built in the Section 8 sequence) and is purely pull + FDE map.
- **The Warehouse Source-of-Truth Map (§8.4.2)** — which system owns inventory authority per warehouse. This is an *inventory-flow* concern, not a discovery concern, and is **deferred to the buying/stock flows (Sections 9-10)** where stock actually moves. It is NOT part of the Location packet.

### 8.4.1 Location discovery and mapping

EasyEcom locations are **born in EasyEcom and only ever pulled into ERPNext** — ERPNext never creates or pushes a location. The flow is discovery (pull) + FDE mapping (done in ERPNext on the standard form), nothing more. The EasyEcom Location DocType (§31.2.2) already exists; this flow populates and maintains it.

**Discovery pull.** The integration calls EasyEcom's location-list endpoint, **`/getAllLocation`** (confirmed live; returns HTTP 200 with the location array). The payload→field translation goes through the Field Mapping engine via the **`EasyEcom-Location-Pull`** ruleset (per the §8.0 engine-as-API-insurance policy — not a hardcoded mapper), reconciled to the real payload shape captured live (the field table below is that mapping, and the ruleset's authoritative reference + test fixture). The flow orchestrates around the engine's output: it creates or updates one EasyEcom Location row per `location_key`, lands new rows in workflow state To Map, and leaves the ERPNext-side mapping fields (`frappe_company`, `mapped_warehouse`) blank for the FDE. A temporarily-missing EE field on a re-pull does not overwrite an existing value (the engine emits no value for an absent field; the flow filters it rather than clobbering).

**Real `/getAllLocation` payload → EasyEcom Location mapping** (from a live response; this is the §8a ruleset reference and test fixture). The response is `{code, message, data:[ ... ]}`; each element of `data[]` is one location:

| EE payload field | EasyEcom Location field | Notes |
| --- | --- | --- |
| location_key | location_key | The natural key; autoname ECS-LOC-{key} |
| location_name | location_name |  |
| company_id | ee_company_id | EE's internal company id (numeric) |
| stockHandle | is_wms_location | **Derived on discovery:** stockHandle=1 → is_wms_location=1 (operational/stock-handling); 0 → 0. FDE may override on the form |
| is_store | is_store | New field; EE store-vs-warehouse flag |
| copy_master_from_primary | copy_master_from_primary | New field; whether this location inherits masters from primary (recorded; not a primary-detection signal) |
| city | city | New field |
| state | state | New field — **GST place-of-supply critical** |
| country | country | New field |
| zip | pincode | Name differs (EE `zip` → ERPNext-style `pincode`) |
| address | address_line | The flat address string |
| address type.billing_address.* | billing_* (street/state/zipcode/country) | Nested billing address |
| address type.pickup_address.* | pickup_* | Nested pickup address |
| api_token | — (dropped) | **Irrelevant today; a credential-shaped string — never stored, redacted if ever logged** |
| userId, phone number | — (optional/ignored) | `phone number` has a literal space in the key — the mapping engine must handle space-bearing source keys; not currently mapped |

Fields EE does **not** supply that the Location DocType needs: `gstin` (FDE-set per operational location, like the Item stock-UOM gap — never inferred), and `is_primary` (no signal in the payload — FDE designates exactly one). `is_operational` is workflow-derived (set by Go Live), never taken from the payload.

**The FDE mapping workflow.** A discovered location is not passively unmapped — it carries an explicit, FDE-facing workflow state (implemented with Frappe's standard **Workflow** primitive, shipped as a fixture; not a hand-rolled status field), so pending FDE work is visible and filterable upfront (and feeds a future "locations awaiting mapping" task count on the dashboard, Sections 17/24). The states:

| State | Meaning | FDE action |
| --- | --- | --- |
| To Map | Just pulled from EasyEcom; needs Company + warehouse assigned | This is the FDE's task list — filter the Location list to this state |
| Mapped but not Live | Company/warehouse assigned and reviewed, but not yet syncing | Review, then Go Live |
| Live | Mapped and actively syncing operational flows | — (steady state) |
| Skipped | FDE has decided this location is out of scope (master-only primary location, non-ERPNext locations) | Deliberate; not an error |

Transitions are role-gated to **EasyEcom FDE** (System Manager inherits): To Map → Mapped but not Live ("Map"; gated on frappe_company being set, so the workflow enforces that mapping actually happened); Mapped but not Live → Live ("Go Live"; this transition sets is_operational); To Map / Mapped → Skipped ("Mark Not Relevant"); and reverse transitions (Live → Mapped but not Live to pause, Skipped → To Map to reconsider) so no state is a dead end. The discovery pull creates new rows in the **To Map** state automatically.

**The workflow state is the source of truth.** is_operational is *derived* from it — set true by the Go Live transition, false by Live → Mapped but not Live — so there is one authoritative notion of whether a location is on, not two competing booleans. The FDE works entirely through the standard Frappe form and the action buttons the Workflow generates; no custom mapping UI is built.

**Re-pull (steady state).** Discovery runs again on a daily cadence. A location id not seen before is created fresh in the **To Map** state and the FDE is alerted (a new location needs mapping). An already-known location updates in place (its EE-supplied fields refresh) and its workflow state is left untouched — re-pull never auto-advances or resets the workflow. An unmapped location is a normal, expected state (To Map / Skipped), never an error: per §8.4.4, the steady state legitimately includes locations that are out of scope. Existing Location rows entered manually before this discovery flow existed are back-filled into the appropriate state on first migration (mapped-and-operational → Live, mapped-not-yet-on → Mapped but not Live, the rest → To Map).

### 8.4.2 The Warehouse Source-of-Truth Map (built with Location)

The most consequential map for inventory, because it determines which system owns inventory, which system originates Purchase Receipts, and which side wins on stock disputes. It is the explicit, per-location intersection of Frappe warehouses and EasyEcom locations — and it is deliberately partial (see 4.4.3). Every Frappe Warehouse that participates in EasyEcom-mediated operations has a row.

**The full DocType is built with the Location packet, and the FDE configures every field at onboarding** — including the inventory-authority fields. These are business facts about how the client operates (who owns stock, who receives goods, who makes corrections), known up front and decided once during onboarding; the flows that *act* on them (Sections 9-11) are built later and read this map rather than extend it. Config exists first; the flow code that consumes it arrives with each flow.

| Field | Type | Meaning | Acted on by |
| --- | --- | --- | --- |
| warehouse | Link → Warehouse | The Frappe Warehouse | — |
| company | Link → Company | The warehouse's Company. Resolved from the linked location's frappe_company; recorded here for query convenience and isolation | — |
| easyecom_location_key | Link → EasyEcom Location | The EasyEcom location this warehouse maps to. Blank for internal-only warehouses | — |
| is_linked | Check | Computed: True iff easyecom_location_key is set | — |
| inventory_master | Select: ERPNext / EasyEcom | Which system holds the authoritative running stock balance for this warehouse. On a dispute or drift, the master side wins; the other is reconciled to match. | §9 / §10 |
| pr_origination | Select: ERPNext direct / EasyEcom GRN flow | Who originates Purchase Receipts. "EasyEcom GRN flow" = goods received in EE (GRN) and ERPNext creates the PR from the GRN event (§9); "ERPNext direct" = PRs made in ERPNext, warehouse not driven by EE GRNs. | §9 |
| adjustment_origination | Select: ERPNext / EasyEcom | Who originates *stock adjustments* — corrections that are not a sale/purchase/transfer (cycle-count corrections, damage write-offs, found stock, shrinkage). If EasyEcom: the adjustment is made in EE and ERPNext mirrors it with a Stock Reconciliation/Stock Entry. If ERPNext: made in ERPNext and pushed to EE. Distinct from inventory_master because a warehouse may have EE as the running-balance master yet still take adjustments from ERPNext (finance-controlled), or vice versa. | §10 |
| mirror_stock_reservations | Check | Whether an EasyEcom "inventory reserved" event (stock committed to a B2B order before dispatch) is mirrored into ERPNext as a Stock Reservation Entry against the matching Sales Order — so ERPNext's available-to-promise reflects what EE has committed. True where stock-promise accuracy matters (high-value B2B); False where the extra entries are unwanted churn (fast-moving B2C). | §11 |
| enabled | Check | Per-warehouse kill-switch | — |

The map is keyed per location, not per Company. Because location→Company is many-to-one, a single Company may own several rows here (several warehouses, each linked to a distinct EasyEcom location). The "Acted on by" column indicates which later flow's code *reads* a behavior field; the field itself is built and FDE-configured now, with the Location packet.

### 8.4.3 Sync rules

- EasyEcom Location records and the Source-of-Truth Map are configured by the FDE during onboarding
- New EasyEcom locations created post-onboarding: detected on daily `/getAllLocation` pull; FDE alerted to map them (or to mark them not-relevant)
- Frappe Warehouses created post-onboarding: do NOT auto-create EE locations; FDE explicitly maps if needed
- De-mapping a warehouse (setting easyecom_location_key blank) is allowed only if no open operational documents reference the link

### 8.4.4 Partial mapping is normal

The map covers only the warehouses and locations that participate in the integration. Both sides legitimately hold things the other never sees, and the integration treats this as the expected steady state, not an error:

- Frappe warehouses with no linked EasyEcom location (scrap, WIP, quarantine, any purely internal bin) simply have no row, or a row with easyecom_location_key blank. The integration never pushes or pulls stock for them.
- EasyEcom locations that are not relevant to ERPNext (including the primary location when it is master-only, and any location the client uses for non-ERPNext purposes) are never mapped to a warehouse and resolve to no Company. The integration ignores them for operational flows.

Validation must not flag the existence of unmapped warehouses or unmapped locations. Onboarding tooling may surface them as a checklist so the FDE can confirm each is intentionally out of scope, but the steady state legitimately includes unmapped entities on both sides.

## 8.5 Tax (EasyEcom Tax Rule → Item Tax Template mapping)

Tax mapping is critical for Sales Invoice creation (Sections 11, 12) and Purchase Receipt creation (Section 9) to apply correct GST. Without it, downstream invoices have wrong tax lines, ITC is mis-attributed, and reconciliation fails.

### 8.5.1 The EasyEcom tax reality (grounded in real payloads + the EE Tax Master)

EasyEcom has **no separate tax-master API**. Tax rules are configured in EasyEcom's UI (Masters >> Tax Master) and attached per product. Tax therefore arrives **inside the product payload** (`/Products/GetProductMaster`), never from a standalone endpoint. Each product carries:
- **`tax_rule_name`** — the name of the EE tax rule (e.g. `GST`, `5`, `tax_28%`, `TAx Rule5`, `GST-18`). These names are arbitrary, human-typed, and **not parseable** — a rule named `5` applies 5%, a rule named `GST` may apply 5% or 18% depending on price. The name is an **opaque key only**.
- **`tax_rate`** — the **resolved** decimal rate EE applied for that product (e.g. `0.18`). For a slab rule, EE has already evaluated the slab; this is the resolved answer, not the rule definition.
- **`cess`** — per-product cess (in the order payload), separate from the rule.
- **`hsn_code`** — the product's HSN.

An EE tax rule (from the Tax Master) is **HSN or Non-HSN**, calculated on Unit or Selling Price, and may define **price slabs**: Min/Max value ranges each with a GST rate (e.g. rule `GST` = 0–2500 → 5%, 2500+ → 18%). A blank Max means a uniform rate. The slab structure lives **only in EE's UI** — there is no API to read it.

### 8.5.2 ERPNext supports price slabs natively

ERPNext's Item **Taxes** child table (the Item Tax tab) has native **Minimum Net Rate** and **Maximum Net Rate** columns per row. This means a price-slab tax maps directly onto an item: one Taxes row per band (e.g. `GST 5%` [0–2500], `GST 18%` [2500+]), and **ERPNext resolves the correct band by net rate natively at invoice time**. The integration therefore never replicates slab logic — it populates the item's native Taxes rows and lets ERPNext resolve. A flat rule is just a single Taxes row with blank min/max.

### 8.5.3 The EasyEcom Tax Rule Map DocType (§31.2.x)

An FDE-configured mapping, **one document per (tax_rule_name, company)**:
- `tax_rule_name` (Data) — the EE rule name (the opaque key from the payload)
- `company` (Link → Company) — the document is scoped to one company
- Natural key / unique: **(tax_rule_name, company)**
- `taxes` — child table **reusing ERPNext's native "Item Tax" child DocType** (item_tax_template, tax_category, valid_from, minimum_net_rate, maximum_net_rate). Holds **only this company's** Item Tax Templates. One row for a flat rule; multiple banded rows for a slab rule. The FDE picks real templates from the dropdown and enters the bands, reading the slab structure from EE's Tax Master UI (no API to pull it).

Because the child rows hold actual company-specific Item Tax Templates (e.g. `GST 18% - OTC`), there is **no rate→name parsing** — the FDE selects the real template. The document being per-company means opening "GST / OTC" shows only OTC's rows.

### 8.5.4 The resolver (8c-owned; called by Item sync, §8.1 / 8d)

`resolve_and_stamp_tax(item, product)`:
1. Read `product.tax_rule_name` and the item's company.
2. Look up the Tax Rule Map document for **(tax_rule_name, company)**.
3. **Stamp** that document's `taxes` rows (template + min/max band) onto the item's native Taxes table. ERPNext resolves the band at invoice time.
4. **Reconciliation:** check the product's resolved `tax_rate` falls within a mapped band; a mismatch raises a Discrepancy (catches a mis-entered band or a changed EE rule).
5. **CESS** is applied to the item from the product's own `cess` value — outside this map.
6. **Unmapped (tax_rule_name, company):** if no document exists, **auto-create one in workflow state To Configure** and flag the FDE (the "discovered → needs config" pattern). The item's tax is not silently defaulted; the missing mapping is a visible FDE task.

### 8.5.5 FDE workflow

A Frappe Workflow (shipped as a fixture, reusing the 8a/8b pattern) on the Tax Rule Map: **To Configure → Configured**, branch **Ignored**. The Configure transition is gated on the `taxes` table being non-empty. New (rule, company) pairs auto-appear in **To Configure** when Item sync encounters an unmapped one; the FDE may also pre-create them from EE's Tax Master before syncing items. "Configured" means the FDE has set up that (rule, company); per-company completeness (a company with no rows for a rule) surfaces at stamp time as an FDE task, not via the workflow.

### 8.5.6 Build order

Tax (8c) is built **before** Item (8d): the resolver and the map must exist for Item sync to stamp correct tax. 8c owns the DocType, the workflow, and the resolver; 8d calls the resolver. The standard GST-rate Item Tax Templates (`GST 5/12/18/28% - {abbr}`, `Exempted`) are ERPNext/India-Compliance natives the FDE selects from; 8c does not create templates.

### 8.5.7 Failure modes

- **Unmapped (tax_rule_name, company):** auto-creates a To-Configure Tax Rule Map doc; FDE alerted; item tax not silently defaulted.
- **Item's company has no rows for its rule** (rule mapped for other companies but not this one): resolver finds nothing to stamp → FDE task.
- **Resolved `tax_rate` outside all mapped bands:** Discrepancy raised (mis-entered band or changed EE rule).
- **Item with no HSN:** flagged per India Compliance requirements (HSN is required for GST documents); handled in Item sync (8d).

## 8.6 Channel master

In EasyEcom, "marketplace" and "channel" are the **same flat concept** — EasyEcom uses the two words interchangeably for one flat list of order origins/destinations, each entry carrying a numeric `marketplace_id`. This is the structure the integration mirrors. There is **no parent/child hierarchy in EasyEcom**: "Amazon.in" (id 8), "Amazon_FBA" (id 11), and "Amazon.co.uk" (id 51) are *separate sibling entries with separate ids*, not a parent "Amazon" with child channels. The list is heterogeneous by design — it contains B2C marketplaces (Amazon.in, Flipkart, meesho), B2B channels (Cloudtail B2B, AJIO JIT/B2B, ajio b2b offline), quick-commerce, own storefronts (Shopify1…14, WooCommerce), POS/offline pseudo-channels (Customer Cash Sales, Employee Card Sales), and even accounting-connector artifacts (Tally, Xero, SAP) that are not sales channels at all.

The integration models this exactly as EasyEcom presents it — **one flat Channel list keyed by EasyEcom `marketplace_id`** — and does **not** impose an invented two-level Marketplace→Marketplace-Channel hierarchy. Earlier drafts modelled such a hierarchy; that was a modelling error (it does not match the source system) and is corrected here.

### 8.6.1 The Marketplace DocType (the flat channel list)

One DocType, **Marketplace**, holds the flat channel list, one row per EasyEcom channel, keyed by the EasyEcom `marketplace_id`. (The DocType is named "Marketplace" because that is the field name EasyEcom returns — `marketplace_name`/`marketplace_id` — even though the list is broader than B2C marketplaces. The "Channel" accounting dimension of Section 4.4 draws its values from this same list.)

| Field | Type | Notes |
| --- | --- | --- |
| marketplace_id | Int | EasyEcom's numeric id; the stable join key; unique. Autoname on this |
| marketplace_name | Data | EasyEcom's name for the channel (e.g., "Amazon.in", "Cloudtail B2B", "Customer Cash Sales") |
| channel_type | Select | FDE-classified: B2C Marketplace / B2B / Quick-Commerce / Own Storefront / POS-Offline / Connector-Ignore. Drives which flow (§11 vs §12) and whether it is operationally relevant at all |
| is_active | Check | Mirrors the per-account integration status from /current-channel-status (Active/Inactive) |
| reporting_parent | Link → Marketplace | **Optional** rollup for reporting only. Lets several EE channels (Amazon.in + Amazon_FBA + Amazon.co.uk) roll up to a single reporting group (e.g., a "Amazon" row) for channel-wise P&L, *without* implying EE has a hierarchy. Blank for channels that stand alone |
| enabled | Check | Per-channel kill-switch on our side |

The `reporting_parent` is the deliberate, optional answer to "the flat list has 10 Amazon variants — can I group them for P&L?": yes, by pointing each variant's `reporting_parent` at a grouping row. This is a *reporting* convenience on our side, not a claim about EE's structure, and it is never required. It is FDE-set (EasyEcom supplies no grouping), blank by default.

A Frappe Workflow is attached to this DocType (shipped as a fixture, reusing the 8a Location pattern), adding the standard `workflow_state` field with states **Unclassified → Classified → Active**, branch **Ignored**. Discovery creates new rows in **Unclassified**; the Classify transition is gated on `channel_type` being set; `is_active` (EE's pulled integration status) is a separate axis from the workflow state (see §8.6.3).

### 8.6.2 Marketplace Account DocType (deferred to reconciliation)

Per (Company, Marketplace channel) — holds seller_id, GSTIN, default_warehouse, settlement_template, rate_card_subscriptions. Composite unique key (company, marketplace, marketplace_seller_id).

> **Build timing: NOT part of the Channel packet (8b).** Every field here — seller_id, GSTIN, settlement_template, rate_card_subscriptions — is a *settlement/reconciliation* concern, not a channel-discovery concern. The Marketplace Account is therefore built when reconciliation/settlement is built (it is consumed by the settlement and B2B-invoicing flows, §11/§13, and the reco engine), not when the channel list is first pulled. 8b builds only the flat Marketplace channel list + the FDE classification workflow + the optional reporting_parent rollup. See the reconciliation/settlement sections (§11, §13) where this DocType is configured and consumed.

### 8.6.3 Sync

- Channels are read from EasyEcom via **`GET /current-channel-status`** — confirmed live; returns, **for one location**, the channels integrated on that location, each with `marketplace_name`, `marketplace_id`, and a `status` of Active/Inactive. (Earlier drafts also assumed a `/marketplaces/list` "full catalogue" endpoint; it is **not used** — `/current-channel-status` is sufficient.)
- **This is a per-location call, NOT account-scoped.** The JWT is per-location (§3), and `/current-channel-status` answers *for the location whose JWT is used*. So channel discovery is a **sweep over locations**, not a single foundational call:
  - Poll `/current-channel-status` for **every discovered EasyEcom Location** — *all* of them, regardless of mapping state (To Map, Mapped but not Live, Live, Skipped). The channel catalogue must be complete, and a channel can be live on a location the FDE hasn't mapped yet, so polling only mapped/Live locations would miss channels. A JWT can be acquired for any discovered `location_key` (mapping to a Company is our bookkeeping; the JWT is EE-side auth for the location_key), so every location is pollable.
  - Each location's call is an operational, per-location call (uses that location's JWT), **wrapped in the per-record savepoint helper** (§7.1, built in 8a) so one location's failure — e.g. a JWT problem — does not abort the whole sweep; that location is recorded Failed and the rest continue.
- **Union and dedupe by `marketplace_id`.** The same channel (e.g. Flipkart, `marketplace_id` 2) appears across many locations' responses. The channel's identity is **account-level**: one Marketplace row per `marketplace_id`. **If a Marketplace row for that `marketplace_id` already exists, skip creating it** (dedupe) — do not create per-location duplicates. New `marketplace_id`s are created (in workflow state Unclassified).
- **`is_active` is catalogue-level: a channel is Active if it is Active on *any* location.** Per-location channel status (Flipkart active on location A, inactive on B) is not tracked as distinct data in this packet; if a later flow needs per-location channel availability, it is added then. For the channel master, "exists and is live somewhere" is the granularity.
- **The payload→Marketplace translation goes through the Field Mapping engine** (the `EasyEcom-Channel-Pull` ruleset), not a hardcoded mapper — per the engine-as-API-change-insurance policy (§8.0). Real-payload mapping: `marketplace_id` → `marketplace_id` (the join key), `marketplace_name` → `marketplace_name`, `status` (Active/Inactive) → `is_active`. The flow orchestrates the per-location sweep, the dedupe, and the workflow around the engine's output.
- `reporting_parent` is **not** supplied by EasyEcom (EE has no channel grouping) — it is FDE-set, blank by default, an optional reporting rollup (§8.6.1).
- Run during onboarding and on a daily refresh (the sweep re-runs across locations; existing channels are skipped, new ones land Unclassified and alert the FDE).
- **FDE classification workflow.** A pulled channel carries an explicit workflow state (Frappe Workflow, shipped as a fixture — reusing the 8a Location pattern): **Unclassified → Classified → Active**, branch **Ignored**. A newly-discovered channel lands in **Unclassified** and is the FDE's worklist (filter to Unclassified). The FDE sets `channel_type` (B2C Marketplace / B2B / Quick-Commerce / Own Storefront / POS-Offline / Connector-Ignore) — the Classify transition is gated on `channel_type` being set — then Activates it; connector artifacts that are not sales channels are moved to **Ignored**. An unclassified channel is not-yet-operational, never an error. Note `is_active` (EE's pulled integration status, active-anywhere) and the workflow state (our classification lifecycle) are **independent axes**.
- Channels drive the Sales Invoice `ecs_marketplace` field and the "Channel" accounting dimension; `channel_type` decides whether an order from that channel runs the B2C flow (§12) or the B2B flow (§11).

> **Open item for §11/§12 build — channel resolution per flow.** Because the channel list is flat and spans both B2C and B2B, each sales flow must resolve the channel for the document it creates: §12 (B2C) resolves it from the EE order's marketplace_id; §11 (B2B) resolves it from a mapping on the B2B Customer (e.g., orders to the "Zepto"/"Cloudtail B2B" customer carry that channel). The customer→channel mapping mechanism for B2B is to be designed at the §11 build. Both flows then stamp the resolved channel as the "Channel" accounting-dimension value (Section 4.4) on the Sales Invoice.

## 8.7 Lookup tables

Smaller masters that round out the model:

- UoM — pulled from EE on first sync; mapped to ERPNext UoM; new UoMs require FDE approval
- Currency — assumed INR-only in v1.0 (multi-currency deferred to v2.0 per the PRD)
- Brand — bidirectional, last-write-wins on conflict
- Item Group — ERPNext-owned; EE category mapping per ERPNext Item Group is in EasyEcom Category Map DocType
- Country / State — standard frappe lookups, not synced

# 9. Buying and Inwarding Flow

The Purchase-to-GRN-to-Receipt flow is the most demanding integration in the spec. It involves three masters (Item, Vendor, Warehouse), tax categories, batch and serial and expiry tracking, accepted-vs-rejected quantity handling, and bidirectional state across two systems for the duration of the procurement cycle. Get this flow wrong and inventory valuations are wrong; everything downstream depends on it.

This entire flow applies only to **WMS locations** (EasyEcom Location.is_wms_location). A WMS-plan location runs full warehouse operations in EasyEcom — PO, GRN, cycle counting, shelving, putaway — so EasyEcom emits the GRN events this flow consumes. A Non-WMS (OMS-only) location has no GRN/PO workflow in EasyEcom and maintains inventory manually; for such a location this flow is inert, and the location's stock is not driven by EasyEcom GRN events. The integration checks is_wms_location before scheduling or processing any buying/GRN work for a location.

Where the location has serialization enabled (EasyEcom Location.serialization_enabled), GRN quantities are received per individual serial: each unit is pushed and reconciled against its own serial number rather than as an aggregate quantity, and ERPNext Serial No records are created accordingly.

## 9.1 The flow

End-to-end:

```
  ERPNext Purchase Order
       │ on_submit, target_warehouse is mapped to an EE location
       ▼
  Validate preconditions:
       ─ Item synced to EE for this Company  (else FAIL)
       ─ Vendor synced to EE                  (else FAIL)
       ─ Tax Category mapped                  (else FAIL)
       │
       ▼
  EasyEcomQueueJob: push PO to EasyEcom
       │ POST /wms/createPurchaseOrder
       ▼
  EE PO created, queueId returned
       │ poll /getQueueStatus until success
       ▼
  Sync Cursor advances; ERPNext PO custom field
  ecs_easyecom_po_id populated
       │
       │ ... days pass, goods physically arrive at the warehouse ...
       │
       ▼
  Operator does GRN in EasyEcom UI:
       ─ scan items
       ─ enter accepted_qty / rejected_qty per line
       ─ enter batch_no / serial_no / expiry per Item settings
       ─ click "Mark Complete"
       │
       ▼
  EasyEcom emits webhook + GRN appears in /wms/getGrnDetails
       │
       ▼
  Polling cron (every 30 min, or webhook trigger) detects new GRN
       │
       ▼
  EasyEcomQueueJob: create Purchase Receipt in ERPNext
       │ ─ map every GRN field to PR field
       │ ─ apply tax category from ERPNext Item Tax Template
       │ ─ split accepted to default warehouse, rejected to Rejected Warehouse
       │ ─ populate batch / serial / expiry per item settings
       ▼
  Purchase Receipt submitted in ERPNext
       │ ─ Stock Ledger Entries created
       │ ─ GL Entries: stock-received-but-not-billed
       │ ─ Back-references: PR.ecs_easyecom_grn_id, PR.items[].ecs_easyecom_grn_line_id
       ▼
  PO status updated (Partially Received or Received)
```

## 9.2 Preconditions

Before a Purchase Order can be submitted with a target warehouse mapped to EE, all of the following must be satisfied. If any fail, submission fails with a clear error message — never silently bypassed:

| Precondition | Failure mode | Error to user |
| --- | --- | --- |
| Item synced to EE for this Company | Item exists in ERPNext but not in EE | Item {item_code} is not synced to EasyEcom for company {company}. Sync the item before submitting this PO. |
| Vendor synced to EE | Supplier exists but not in EE | Supplier {supplier} is not synced to EasyEcom. Sync the supplier from the Supplier form before submitting. |
| target_warehouse is mapped (Source-of-Truth Map shows is_linked = True) | Warehouse not mapped | Warehouse {warehouse} is not mapped to an EasyEcom location. Either map it via Warehouse Source-of-Truth Map or change target_warehouse on this PO. |
| HSN code populated on every item | Item missing HSN | Item {item_code} has no HSN code. Cannot push to EasyEcom. |
| Tax Category mapped | ERPNext Tax Category has no EE mapping | Tax Category {tax_category} is not mapped to an EasyEcom tax category. FDE configuration required. |
| UoM mapped | Item UoM not in EE UoM mapping | UoM {uom} is not mapped to EasyEcom. FDE configuration required. |

These checks run in the Purchase Order's validate hook. They are deliberately strict — silently allowing a PO to submit and then failing the EE push asynchronously is bad UX (the user moves on, the PO is stuck) and a recon-engine hazard.

## 9.3 PO push: payload and idempotency

- Trigger: Purchase Order's on_submit if any item line has target_warehouse with is_linked = True
- Idempotency key: the ERPNext PO name (e.g., PUR-ORD-2026-00321)
- Payload includes: PO number, supplier (mapped EE vendor_id), target location_key, PO date, expected receipt date, line items with item_code (mapped to EE company_product_id), qty, rate, tax breakdown, HSN code
- Response carries an EE PO ID and a queueId; we poll /getQueueStatus until status = success
- Result stored in ecs_easyecom_po_id custom field on the PO
- Failure: queueId resolves to error → EasyEcom Queue Job moved to Failed; FDE notified; ERPNext PO is NOT cancelled (commercially valid PO is preserved)

## 9.4 Partial PO push (mixed warehouses on one PO)

A single ERPNext Purchase Order may have line items destined for different target warehouses, some linked and some not. The integration handles this:

- Lines with target_warehouse linked to EE are grouped by location_key and pushed as separate EE POs (one per location_key)
- Lines with target_warehouse not linked to EE are NOT pushed and remain ERPNext-only
- The ERPNext PO maintains the unified view; EE sees one or more EE POs each covering a subset of lines
- ecs_easyecom_po_mappings child table on the PO records each (location_key, ee_po_id, frappe_lines) tuple

## 9.5 GRN-in-EasyEcom: detection and ingestion

### 9.5.1 Detection

Polling-first per Section 2.4 of the PRD. GRN detection works as follows:

- Polling cron runs every 30 minutes per operational location (company derived from the location)
- Calls /wms/getGrnDetails with last_pull_grn cursor as since timestamp
- New GRN records discovered → enqueued as EasyEcom Queue Job of type process_grn
- Webhook subscription (if EE supports for this client) accelerates detection — webhook receipt knocks the polling cycle into immediate execution for that location
- Webhook missed? Doesn't matter — the next poll catches it (within 30 min)

### 9.5.2 Idempotency

- Each GRN has a unique EE GRN ID
- Before processing, we check if a Purchase Receipt with ecs_easyecom_grn_id = this_grn_id already exists; if yes, skip
- Webhook + poll concurrency is safe — both paths converge on the same dedup check

## 9.6 GRN → Purchase Receipt mapping

### 9.6.1 Header-level mapping

| EE GRN field | ERPNext PR field | Notes |
| --- | --- | --- |
| grn_id | ecs_easyecom_grn_id | The dedup key |
| po_id (linked EE PO) | purchase_order (resolved via ecs_easyecom_po_id) | Resolves to ERPNext PO; one ERPNext PO may have multiple GRNs (partial receipt) |
| vendor_id | supplier (resolved via ecs_easyecom_vendor_id) |  |
| location_key | set_warehouse (default warehouse on PR header) | Source-of-Truth Map → mapped Frappe Warehouse |
| grn_date | posting_date |  |
| received_at_time | posting_time |  |
| invoice_number | supplier_delivery_note | Vendor's DC reference |
| invoice_date | (custom field) ecs_supplier_invoice_date |  |
| transporter_details | transporters (Transporter doc) | If populated |
| lr_number, lr_date | lr_no, lr_date |  |

### 9.6.2 Line-level mapping

| EE GRN line field | ERPNext PR Item field | Notes |
| --- | --- | --- |
| company_product_id | item_code (resolved via Item.ecs_easyecom_mappings) |  |
| accepted_qty | qty | Goes into the warehouse |
| rejected_qty | rejected_qty | Goes into Rejected Warehouse — see 5.7 |
| unit_rate | rate | Standardise to ERPNext UoM |
| uom | uom (mapped) |  |
| batch_no | batch_no | Only if Item.has_batch_no = True; else error |
| serial_no | serial_no | Only if Item.has_serial_no = True; multiple serials become newline-separated |
| expiry_date | (on Batch) expiry_date | Only if Item.has_expiry_date = True |
| mfg_date | (on Batch) manufacturing_date |  |
| hsn_code | gst_hsn_code | Validated against ERPNext Item.gst_hsn_code |
| tax_amount | tax line | Mapped via Item Tax Template |
| item_remarks | description (extended) |  |
| grn_line_id | ecs_easyecom_grn_line_id | For per-line traceability |

## 9.7 Rejected quantity handling

EE GRN lines may carry rejected_qty for items inspected and refused at receipt. ERPNext handles this via the Rejected Warehouse pattern:

- Each Source-of-Truth Map row has a default_rejected_warehouse (configurable, optional)
- If rejected_qty > 0 on a GRN line: PR Item gets rejected_qty populated, rejected_warehouse set
- If no Rejected Warehouse is configured: PR submission fails with a clear error — FDE configuration required
- ERPNext's standard rejected-quantity handling kicks in: stock entry into rejected warehouse, separate from accepted stock
- Subsequent disposition (return-to-vendor, scrap, eventual acceptance) uses ERPNext's standard flows — not integration-specific

## 9.8 Batch, serial, and expiry

### 9.8.1 The pre-flight check

If Item settings on the two sides disagree, the GRN cannot be ingested cleanly. Pre-flight validates:

- If EE GRN includes batch_no but Item.has_batch_no = False → error, FDE alerted
- If Item.has_batch_no = True but EE GRN omits batch_no → error, FDE alerted
- Same for serial_no and expiry_date
- Mismatches usually indicate Item master misconfiguration on one side

### 9.8.2 Batch creation

- New batch_no in EE GRN: create Batch record in ERPNext with item_code, batch_no, manufacturing_date, expiry_date
- Batch already exists: link the PR to it (multiple GRNs for the same batch are valid for restocks)
- Batch attributes update only if blank — never overwrite

### 9.8.3 Serial number creation

- Each serial in EE GRN becomes a Serial No record in ERPNext linked to the PR
- Serial-no validation: must be unique per item_code
- Conflict (serial already exists for the same item with a different status): integration aborts the GRN ingestion, FDE alerted

## 9.9 Tax category and GST handling

- Tax category for the PR line is resolved from the ERPNext Item Tax Template, not from the EE GRN payload
- EE-side tax amounts are sanity-checked against the ERPNext-derived tax — variance > 1% triggers an Integration Discrepancy of severity Warning
- Place of supply rules per India Compliance app drive CGST/SGST vs IGST split
- ITC eligibility flag is preserved through the GRN → PR mapping
- e-invoice and e-waybill from the supplier (if provided) are stored as PR attachments

## 9.10 Multiple GRNs per PO

A PO with 100 units of an Item may receive 60 in GRN-1 and 40 in GRN-2 weeks apart:

- Each GRN produces a separate Purchase Receipt in ERPNext
- All PRs reference the same Purchase Order
- PO status progression: Draft → Submitted → Partially Received (after GRN-1) → Received (after GRN-2)
- Over-receipt: if cumulative received_qty exceeds PO qty (configurable tolerance, default 0%), the PR submission fails — FDE handles via PO amendment

## 9.11 Failure modes and recovery

| Failure | Detection | FDE recovery procedure |
| --- | --- | --- |
| GRN webhook missed entirely | Polling cron picks it up within 30 min | Automatic; no manual action |
| GRN webhook processed twice | Idempotency dedup on grn_id | Automatic; no manual action |
| GRN ingestion failed mid-process (PR partially built) | EasyEcom Queue Job in Failed state, no PR submitted | Inspect Queue Job error; fix root cause; click Retry on Queue Job — full ingestion re-runs idempotently |
| EE returns GRN before EE PO is fully created | Out-of-order events | Queue Job loops up to 6 times waiting for PO; if persistent, alert |
| Item not synced when GRN arrives | Pre-flight blocks | FDE syncs Item, Retries Queue Job |
| HSN mismatch between EE and ERPNext | Header-level check during PR build | Reconcile HSN; usually edit ERPNext Item; Retry |
| Rejected qty present but no Rejected Warehouse configured | Pre-flight blocks | FDE configures default_rejected_warehouse on Source-of-Truth Map; Retry |
| Batch/serial conflict (already exists with different state) | PR build aborts | FDE investigates — usually data correction needed; Retry |
| PR submission fails on GL posting (account not configured) | Standard ERPNext error | FDE fixes account configuration on Item or Item Group; Retry |

## 9.12 What this enables for reconciliation

The recon engine's Fee-to-Expense reconciliation (Section 13.2.4 of the PRD) requires that every Purchase Invoice posted from a fee Settlement Line correctly reflects the GST ITC split. This depends on the upstream Purchase Receipt having correct tax category mapping. The buying flow's strict pre-flight on tax category mapping is what guarantees this — without it, fee Purchase Invoices have wrong ITC postings, and ITC reconciliation against GSTR-2B fails.

Equally: inventory variance reconciliation (an enhancement post-v0.1) compares ERPNext stock against EasyEcom stock per SKU per location. The buying flow's integrity of GRN → PR is what makes this comparison meaningful.

# 10. Stock Transfer Flows

Stock Transfers between warehouses come in four flavours depending on whether the source and destination are linked to EasyEcom locations. Each flavour has its own choreography. The integration's job is to apply the right one based on the Source-of-Truth Map.

## 10.1 The decision matrix

| Source | Destination | Behaviour |
| --- | --- | --- |
| Non-linked | Non-linked | Pure ERPNext Stock Entry. EasyEcom is not involved at all. Flow handled by ERPNext core. |
| Non-linked | Linked (EE) | Stock Entry out of source in ERPNext + GRN at destination in EE → produces Internal Purchase Receipt in ERPNext on EE GRN completion. See 6.2. |
| Linked (EE) | Non-linked | Dispatch from EE → Stock Entry into destination in ERPNext on EE dispatch event. See 6.3. |
| Linked (EE) | Linked (EE) | EE-side internal transfer. ERPNext mirrors with two Stock Entries (out, in) on EE confirmation. See 6.4. |

## 10.2 Non-linked → Linked (most common case)

Goods leave a non-EE warehouse (e.g., a 3PL or in-house facility not under EE management) and arrive at an EE-managed location. The integration must:

1. Allow the user to create a Stock Entry of type Material Transfer in ERPNext with source = non-linked WH, destination = linked WH
1. On Stock Entry submit: source warehouse is debited (goods leave), but destination warehouse is NOT credited yet — the goods are in transit
1. In-Transit Warehouse pattern: ERPNext's standard support for transit warehouses is used. Stock moves source → in_transit_warehouse
1. Goods physically arrive at EE location; warehouse staff does GRN in EE referencing the Stock Entry as the source document
1. EE GRN polling detects the GRN; integration creates an Internal Purchase Receipt (Material Transfer-In type) in ERPNext
1. Internal PR moves stock from in_transit_warehouse → destination linked warehouse
1. Internal PR carries back-references: ecs_source_stock_entry, ecs_easyecom_grn_id
Variances (qty short, damaged in transit) are handled exactly like vendor GRN variances: rejected_qty splits to a Rejected Warehouse, an Integration Discrepancy is raised if accepted_qty + rejected_qty doesn't match the dispatched qty.

## 10.3 Linked → Non-linked

Goods leave an EE-managed location for a non-EE destination (e.g., transferred to a B2B distributor's warehouse the seller manages):

1. Stock Entry created in ERPNext with source = linked WH, destination = non-linked WH
1. Submit pushes a dispatch instruction to EE (POST /inventory/V3/updateInventory adjustment of type Stock-Out with reason Transfer)
1. EE confirms; ERPNext stock moves source → in_transit_warehouse
1. Goods physically arrive; user marks Stock Entry received in ERPNext (a manual step; the integration does not see the arrival)
1. ERPNext stock moves in_transit_warehouse → destination

## 10.4 Linked → Linked (EE-side internal transfer)

- EE supports its own internal transfer between locations
- Originated either in ERPNext or in EE; the integration handles both directions
- ERPNext-originated: Stock Entry pushes a Transfer instruction to EE; EE executes, fires confirm webhook
- EE-originated: detected on inventory pull; ERPNext creates a corresponding Stock Entry
- In both cases, ERPNext stock movement is a single Stock Entry with both source and destination warehouses, no in-transit warehouse needed
- Confirmation is required from EE before ERPNext considers the transfer settled

## 10.5 Non-linked → Non-linked

ERPNext-only. The integration is not involved. Stock Entry behaves exactly as it does in vanilla ERPNext.

## 10.6 Multi-company transfers

- Transfers crossing Company boundaries are NOT covered by this section
- Inter-Company stock movement uses ERPNext's Inter-Company flow (Sales Invoice + Purchase Invoice) regardless of EE linkage
- The integration does, however, push the resulting movements to EE on each Company's side per the Source-of-Truth Map for the warehouses involved

## 10.7 Edge cases

- Stock Entry created with wrong source/destination type (e.g., destination should have been linked but Source-of-Truth Map row missing): blocks at validate; FDE configures the map and retries
- Goods damaged in transit: the GRN at destination shows accepted_qty < dispatched_qty; difference goes to Inventory Shrinkage account via standard ERPNext mechanisms; an Integration Discrepancy of severity Info is raised for FDE awareness
- Lost in transit: if no GRN arrives within configurable threshold (default 30 days), an Integration Discrepancy of severity Warning is raised; FDE investigates and posts adjustments
- EE-side transfer cancelled mid-flight: detected on next inventory pull as state mismatch; ERPNext Stock Entry must be reverse-and-replayed by FDE

## 10.8 Audit trail

- Every Stock Entry produced by the integration carries ecs_easyecom_source_event (link to EE event ID) and ecs_easyecom_source_type (transfer / GRN / dispatch / adjustment)
- Every Internal Purchase Receipt carries the same back-references plus ecs_source_stock_entry
- Stock Ledger Entries inherit these fields via standard Frappe document parent-link
- An auditor can trace any stock movement to its EE source

# 11. B2B Sales Flow

B2B orders are born in ERPNext (the seller's CRM and sales process produces a Sales Order) and pushed to EasyEcom for fulfilment. This is the inverse of the B2C flow (Section 12) where orders are born in EasyEcom and ERPNext sees them only at the invoice stage. The two flows have entirely different choreography and must not be confused.

## 11.1 The flow

```
  ERPNext Sales Order
       │ on_submit, target_warehouse mapped to EE location
       ▼
  Validate preconditions:
       ─ Customer synced to EE                (else FAIL)
       ─ Items synced for this Company        (else FAIL)
       ─ Warehouse linked to EE               (per Source-of-Truth Map)
       ─ Tax Category mapped                  (else FAIL)
       │
       ▼
  Push as B2B Order to EasyEcom
       │ POST /webhook/v2/createOrder (B2B order type)
       │ Mode: Async (default) or Sync (configurable per Marketplace Account)
       ▼
  EE acknowledges with EE order ID
       │
       ▼
  EE processes order — picking, packing
       │ on inventory reserve event in EE
       ▼
  ERPNext mirrors as Stock Reservation Entry
       │ (ERPNext v16 Stock Reservation, scope expanded across Sales Order / Pick List)
       │
       │ ... operator configuration determines next branch ...
       ▼
  Branch A: EE asks ERPNext for Invoice + e-waybill BEFORE dispatch
       │ EE webhook: invoice_request received
       │ ERPNext generates Sales Invoice (against the SO)
       │ ERPNext generates e-waybill via india_compliance
       │ ERPNext returns invoice + e-waybill to EE
       │ EE prints, attaches, dispatches
       │ EE dispatch event → ERPNext SI status updated to Delivered
       ▼
  Branch B: EE generates its own invoice (operator's choice)
       │ EE dispatches order with EE-generated invoice
       │ EE dispatch event → ERPNext creates Sales Invoice mirroring EE invoice
       │ Stock Reservation released, Delivery Note created, stock moves
```

## 11.2 Preconditions

Same strict-blocking semantics as the buying flow. SO submission fails fast if any precondition is unmet:

| Precondition | Error to user |
| --- | --- |
| Customer synced to EE | Customer {customer} is not synced to EasyEcom for company {company}. Sync the customer before submitting this order. |
| All items synced for this Company | Item {item_code} is not synced to EasyEcom. Sync the item before submitting. |
| target_warehouse is mapped to an EE location | Warehouse {warehouse} is not mapped. Either map it or change target_warehouse on this SO. |
| GSTIN populated on Customer (for B2B-large) | Customer {customer} has no GSTIN. B2B GST invoice cannot be generated. |
| HSN code populated on every item | Item {item_code} missing HSN code. |
| Pricing complete (no zero rates unless explicitly free-of-charge marked) | Item {item_code} has rate 0; mark explicitly as Free of Charge or set price. |

## 11.3 SO push: payload and modes

### 11.3.1 Async mode (default)

- Sales Order on_submit fires the push as an EasyEcom Queue Job
- ERPNext SO submission completes immediately regardless of EE availability
- EE acknowledgement updates ecs_easyecom_so_id custom field on the SO
- Failed pushes retry with back-off; persistent failures alert FDE; SO remains valid in ERPNext
- Trade-off accepted: ERPNext may have a confirmed SO that EE has rejected (e.g., for stock unavailability) for a short window — handled by FDE-monitored failure dashboard

### 11.3.2 Sync mode (configurable per Marketplace Account)

- on_submit blocks until EE confirms acceptance
- If EE rejects (e.g., stock unavailable, customer credit limit, item not found), the ERPNext on_submit fails and the SO is not submitted
- Latency: typically 200ms-2s; user sees a brief save delay
- Trade-off accepted: ERPNext UI hangs during the call; if EE is down, SO submissions are blocked entirely
- Configuration: per Marketplace Account, push_so_mode = Sync, push_so_block_on_error = True
- Recommended for high-value B2B clients where stock-promise consistency matters; not recommended for high-volume D2C

## 11.4 Stock Reservation on EE inventory-reserve event

ERPNext v16 expanded Stock Reservation Entry beyond Sales Order to also cover Pick Lists, Work Orders, and Subcontracting flows. We use the Sales-Order-scoped form to mirror EasyEcom's reservation event. The v16 implementation is more performant than v15's under high reserve/unreserve frequency, which matters for high-volume B2B clients.

### 11.4.1 The flow

1. EE reserves stock for the order — fires a webhook event of type inventory.reserved with EE order ID, location_key, line items, qty
1. Polling cron picks it up if webhook missed
1. Resolve the EE order ID to the ERPNext Sales Order via ecs_easyecom_so_id
1. Create Stock Reservation Entry against the SO for each line item, with reservation_based_on_voucher = Sales Order, against_warehouse = the SO's target_warehouse
1. Stock Ledger Entry shows the reserved qty as committed; available qty for other orders drops accordingly
1. On EE dispatch, the SRE is automatically released as the Delivery Note (or Sales Invoice with stock movement) consumes the reservation

### 11.4.2 Configuration gate

- Stock Reservation must be enabled in ERPNext Stock Settings (on by default in v16 fresh installs)
- Per Source-of-Truth Map row: mirror_stock_reservations = True (default)
- If unchecked, the inventory-reserved event is logged but no SRE is created — used for clients who don't care about real-time reservation visibility

### 11.4.3 Edge cases

- EE reserves more stock than ERPNext shows available: SRE creation fails. Integration Discrepancy of severity Error raised. FDE investigates — usually indicates inventory drift between systems
- EE un-reserves (cancellation, change): inventory.unreserved event releases the SRE
- Partial reservation (EE reserves only some lines): partial SREs created accordingly
- Reservation expires in EE without dispatch: timeout handling — typically EE auto-cancels the reservation; we follow with SRE release

## 11.5 Invoice and e-waybill flow

### 11.5.1 Branch A: EE asks ERPNext for invoice

EE is configured (per Marketplace Account or globally) to require ERPNext-generated invoices before dispatch. Trigger: EE webhook of type invoice.requested with EE order ID.

1. Resolve to ERPNext SO
1. Create Sales Invoice from SO using standard ERPNext mechanism (sales_invoice from sales_order)
1. Post tax lines via India Compliance app
1. Submit the SI
1. Generate e-invoice IRN via India Compliance
1. Generate e-waybill via India Compliance with vehicle_no and transporter_id from EE payload
1. Push back to EE: POST /b2b/uploadInvoice with PDF, IRN, e-waybill number
1. EE acknowledges, prints, dispatches

### 11.5.2 Branch B: EE generates its own invoice

- Operator's choice if they prefer EE-generated invoices for B2B (rare but exists)
- EE generates and dispatches
- Dispatch webhook fires with invoice payload
- Integration creates Sales Invoice in ERPNext mirroring EE invoice values exactly (rate, taxes, totals)
- Variance check: if mirrored SI total differs from SO total by more than 1%, Integration Discrepancy raised

### 11.5.3 Choosing between branches

Configuration on Marketplace Account: invoice_origination = ERPNext / EasyEcom. Default: ERPNext (Branch A) because:

- ERPNext-generated invoice has full tax-template control, complete back-references, and fits cleanly into the GL
- e-invoice and e-waybill compliance via India Compliance is more reliable than relying on EE's GST handling
- ERPNext Sales Invoice is the auditor-readable artefact; mirroring an EE invoice introduces lossy translation
Branch B exists for edge cases: clients with EE-managed B2B catalogues where EE controls all invoice generation.

## 11.6 SI to delivery completion

- EE dispatch event (post-invoice for Branch A, with-invoice for Branch B) fires confirmation webhook
- ERPNext SI moves to Delivered status (custom workflow status; standard SI doesn't have Delivered)
- Stock movement: standard SI mechanism with update_stock = True moves stock from warehouse to outgoing
- Stock Reservation Entry is consumed automatically
- Outstanding amount on the SI sits in Customer's debtors account until payment

## 11.7 Multi-warehouse order

A B2B SO with line items across multiple linked warehouses:

- Pushed as separate EE orders, one per location_key (similar to PO multi-warehouse handling)
- Stock reservations and dispatches happen per EE order
- Single ERPNext SO; multiple ecs_easyecom_so_mappings tracking each EE order
- Single ERPNext SI consolidates the deliveries (or multiple SIs if delivery dates differ)

## 11.8 Failure modes

| Failure | FDE recovery |
| --- | --- |
| EE rejects SO push (Async mode) | Inspect Queue Job error; fix root cause (often missing master sync); Retry |
| EE rejects SO push (Sync mode) | ERPNext SO submission fails; user sees error; fix and resubmit |
| Inventory reserve event arrives but SO doesn't exist in ERPNext yet (race) | Queue Job loops up to 6 times; if persistent, manual investigation |
| EE dispatches without invoice request (Branch A configured but skipped) | Integration Discrepancy raised; FDE generates SI manually using EE dispatch data |
| e-waybill generation fails | ERPNext blocks; FDE generates manually via India Compliance and pushes to EE |
| Mirror SI variance > 1% (Branch B) | Integration Discrepancy; FDE reconciles; if EE invoice is wrong, FDE escalates to vendor; ERPNext SI uses EE values pending resolution |

# 12. B2C / D2C / Marketplace Sales Flow

B2C orders are fundamentally different from B2B. The order is born in EasyEcom (or in a marketplace and routed through EasyEcom). ERPNext does NOT see the Sales Order. ERPNext receives the financial event later — typically at manifest creation or invoice generation. This single architectural fact drives the entire flow.

This is by design and it is correct: B2C orders involve marketplace-anonymous customers, complex per-marketplace fulfilment workflows (FBA, F-Assured, MSA), and channel-specific invoice handling. Pushing every B2C order through an ERPNext SO would create unnecessary churn in the books.

> **Build note — populate the Channel accounting dimension.** B2C is the primary place a marketplace is known, so the Sales Invoice this flow creates must carry the marketplace as the value of the **"Channel" accounting dimension** (Section 4.4), in addition to the `ecs_marketplace` field. Stamp the dimension on the SI so its P&L GL lines (revenue, fees) are attributable marketplace-wise. The dimension is optional (Section 4.4), so this is about *populating* it where the marketplace is known, not enforcing it — SI creation must not fail if the dimension is somehow unset. Confirm during the §12 build that the dimension value is set on the SI header and flows to the GL entries.

## 12.0 The EasyEcom order hierarchy (canonical data model)

Every flow that consumes EasyEcom order data depends on getting this hierarchy right. EasyEcom models an order as:

```
Order (Order_id)
  └── Shipment (Invoice ID / AWB)          ← an order may have MANY shipments
        └── Suborder / line item            ← a shipment may have MANY line items
```

- **One order can split into multiple shipments.** Each shipment is identified by its own **Invoice ID** (the shipment-level identifier, used when importing a unit shipment). When more than one Invoice ID exists against the same Order_id, the order has been split — different line items, or different quantities, ship separately.
- **Each shipment contains one or more suborders / line items** — the individual products and quantities the customer ordered within that shipment.
- The financial document the integration produces is per **shipment**, not per order: an EasyEcom shipment (Invoice ID) maps to one ERPNext Sales Invoice. A split order therefore produces multiple Sales Invoices, one per Invoice ID, each carrying its shipment's line items. They share the same marketplace order identifier for reconciliation.

### Identifier conventions: `_id` vs `_no`

EasyEcom exposes two parallel identifier families, and confusing them is a common source of integration bugs:

| Family | Meaning | Examples |
| --- | --- | --- |
| `_id` (e.g., Order_id, Suborder_id) | EasyEcom **internal** identifiers | Order_id, Suborder_id, Invoice ID |
| `_no` (e.g., order_no, suborder_no) | **Marketplace-level** identifiers | order_no (found via reference_code), suborder_no |

The integration keys its own traceability custom fields on the EasyEcom internal `_id` values (stable within EasyEcom), while the **marketplace** `_no` / reference_code values are what join to settlement data in the recon engine. Specifically: ecs_easyecom_order_id stores the EE Order_id; ecs_easyecom_invoice_id stores the shipment's Invoice ID; ecs_marketplace_order_id stores the marketplace order identifier (from reference_code) and is the recon join key.



```
  Marketplace (Amazon / Flipkart / Myntra / etc.)
  OR Shopify / D2C storefront
       │ buyer places order
       ▼
  EasyEcom (OMS — receives order via marketplace adapter)
       │ ─ creates EE order with anonymised buyer details
       │ ─ allocates from warehouse inventory
       │
       │  (ERPNext does NOT see this yet)
       │
       ▼
  Operator processes in EasyEcom
       │ ─ picks, packs
       │ ─ creates manifest (ready-to-dispatch)
       │
       ▼
  Manifest creation event fires webhook + appears in /manifest API
       │
       ▼
  Polling cron (every 5 min) detects new manifest
       │
       ▼
  EasyEcomQueueJob: create Sales Invoice in ERPNext
       │ ─ resolve customer to per-marketplace pseudo-customer (e.g., Amazon FBA Buyer Pool)
       │ ─ map line items from EE company_product_id to ERPNext item_code
       │ ─ apply tax lines from ERPNext Item Tax Template
       │ ─ set ecs_marketplace (flat channel), ecs_marketplace_order_id custom fields
       │ ─ stamp the Channel accounting dimension (Section 4.4)
       │ ─ generate e-invoice IRN via india_compliance (if ≥ ₹50k threshold)
       │ ─ submit
       ▼
  Sales Invoice posted in ERPNext
       │ ─ Stock Ledger Entries created (update_stock = True)
       │ ─ GL Entries: Marketplace Receivable Control debited, sale credited
       │ ─ Marketplace Order Map record created (bridge to settlement reconciliation)
```

## 12.2 Why this is different from B2B

Three architectural distinctions:

- **Order is born in EE.** There is no Sales Order in ERPNext, ever, for marketplace B2C orders. Trying to retrofit one creates accounting churn (SO submitted then immediately SI'd in same minute) for no business value. The marketplace is the order origin; EE is the OMS; ERPNext is the books.
- **Customer is anonymised.** Per Section 8.2.1, marketplace orders use a per-marketplace pseudo-customer (Amazon FBA Buyer Pool etc.). The actual buyer's identity is never in ERPNext.
- **Channel and marketplace fields drive recon.** Every B2C SI carries ecs_marketplace (the flat EE channel) and ecs_marketplace_order_id. These feed the recon engine — the Order-to-Settlement reconciliation joins on ecs_marketplace_order_id.

## 12.3 Manifest-creation as the trigger event

### 12.3.1 Why manifest, not order-create?

Manifest is the operationally meaningful moment: the order is committed for dispatch, inventory is allocated, the invoice is needed for the shipment package. Earlier events (order-create, order-confirm) are too speculative — orders may be cancelled before manifest. Triggering SI creation at manifest aligns ERPNext entries with what's actually shipping.

Once an order is confirmed it does not silently revert to an on-hold state: EasyEcom only moves a confirmed order back to hold if the seller or the marketplace explicitly marks it so. The integration can therefore treat a confirmed/manifested order as stable for SI creation, while still handling an explicit later cancellation through the cancellation flow (Section 13).

### 12.3.2 Detection

- Polling cron every 5 minutes per operational location (company derived from the location) on /orders/V2/getAllOrders with status filter Manifested or higher
- Cursor field last_pull_orders advances on each successful poll
- Webhook of type ready_to_dispatch knocks the polling cycle into immediate execution
- Webhook of type manifested is the canonical trigger
- Idempotency: the unit of a Sales Invoice is the shipment, identified by its EE Invoice ID. Before creating an SI we check for an existing SI with matching ecs_easyecom_invoice_id. An order with multiple Invoice IDs (a split order) produces one SI per Invoice ID, so the dedup key is the Invoice ID, not the Order_id

## 12.4 SI creation: field-level mapping

| EE order field | ERPNext SI field | Notes |
| --- | --- | --- |
| order_id (EE) | ecs_easyecom_order_id | EE internal order-level identifier (shared across a split order's shipments) |
| invoice_id (EE) | ecs_easyecom_invoice_id | EE internal shipment-level identifier; the SI is created per shipment and deduped on this |
| marketplace_order_id (via reference_code) | ecs_marketplace_order_id | Marketplace-level identifier; the recon-engine join key |
| marketplace_id / marketplace_name | ecs_marketplace (Link) | Resolved to the flat Marketplace channel row by EE marketplace_id |
| marketplace | ecs_marketplace (Link) | Mapped via Marketplace master |
| seller_id | (validated against Marketplace Account) | Sanity check |
| order_date | posting_date |  |
| customer (anonymised) | customer (resolved to per-marketplace pseudo-customer) |  |
| billing_address | (stored in Marketplace Anonymous Customer record) |  |
| shipping_address | shipping_address (or generic marketplace address) |  |
| line.item_code (EE) | items[].item_code (resolved via Item.ecs_easyecom_mappings) |  |
| line.qty | items[].qty |  |
| line.unit_price | items[].rate | EE-quoted price |
| line.discount | items[].discount_amount |  |
| line.tax | (applied via ERPNext Item Tax Template, sanity-checked against EE) | ERPNext-derived tax wins; EE variance > 1% raises Discrepancy |
| line.warehouse | items[].warehouse (resolved via Source-of-Truth Map) |  |
| payment_mode | ecs_payment_mode | Prepaid / COD / etc. |
| awb_number, courier | ecs_awb_number, ecs_courier | For tracking |

## 12.5 Pricing — EE invoice as the source of truth for sale price

The actual price the buyer paid (after marketplace discounts, coupons, mid-flight repricing) may differ from the seller's catalogue price. EE has the actual transacted price; we use that for the SI:

- SI rate field comes from EE order line.unit_price
- ERPNext catalogue price (Item Price for the relevant Price List) is NOT used for SI generation
- Variance between catalogue and actual is captured for the recon engine's pricing diagnostics (Section 4.6 of the PRD: marketplace algorithmic repricing detection)
- Tax computation uses ERPNext Item Tax Template — never EE-supplied tax — to ensure tax correctness

## 12.6 e-invoice handling

- B2C invoices ≥ ₹50,000 require e-invoice IRN per Indian GST rules
- India Compliance app handles IRN generation
- Integration triggers IRN generation immediately after SI submit, before EE acknowledgement
- If IRN generation fails: SI is in Submitted status with no IRN; FDE alerted; standard India Compliance retry mechanism applies
- Below threshold: no IRN required; SI submitted normally

## 12.7 Inventory accounting

- SI created with update_stock = True (so SI movement does the inventory reduction)
- Source warehouse: per Source-of-Truth Map for the EE location_key
- Stock Ledger Entry deducts inventory at SI valuation
- If the SO had an SRE (rare for B2C — usually no SO at all), the SRE is auto-released
- For multi-warehouse orders (rare in B2C but possible): one SI with multiple lines each from its own warehouse

## 12.8 Marketplace Order Map

This DocType is the bridge between the SI and the future settlement reconciliation. Its sole purpose is to be the join target for Settlement Lines arriving days or weeks later:

- Created at SI creation time
- Fields: marketplace, marketplace_order_id, channel, marketplace_account, sales_invoice (Link), settlement_status (Forecast / Partial / Settled / Disputed)
- Settlement Forecast (per Section 4 of the PRD) is created in parallel against this Map record
- When Settlement Lines arrive, recon engine joins on (marketplace, marketplace_order_id) → Marketplace Order Map → Sales Invoice → Settlement Forecast

## 12.9 Variance and discrepancy handling

- EE order amount vs SI total: must match within 1 paisa; mismatch raises Integration Discrepancy
- EE-quoted tax vs ERPNext-computed tax: variance > 1% raises Integration Discrepancy of severity Warning (informational; ERPNext tax stands)
- EE order with no item match in ERPNext: that order's SI creation fails as a per-record failure (Failed Sync Record with a translated reason); other orders in the same pull are unaffected and the job lands Partial (Section 7). FDE fixes the Item sync and retries the failed record
- EE order whose channel (marketplace_id) is not yet in the flat Marketplace list or is unclassified: same per-record treatment — Failed Sync Record for that order; FDE classifies the channel and retries

## 12.10 Multi-channel handling

- Same Item, multiple marketplace listings: handled via marketplace_skus child table on Item
- EE returns the marketplace SKU; integration resolves to ERPNext item_code
- Channel-specific pricing: not modelled in v1.0 — the EE-supplied actual rate is used (which already reflects channel-specific pricing applied at order time)
- Bulk order upload is an EasyEcom UI/panel activity driven by templates within EasyEcom, not an API surface. The integration does not create orders by bulk upload; it ingests orders that already exist in EasyEcom through the order-pull APIs. Bulk upload remains a manual EasyEcom-side operation outside the integration's scope

## 12.11 What this enables for reconciliation

- Order-to-Settlement reconciliation (PRD Section 9.2.1) needs the SI to exist with ecs_marketplace_order_id populated. The B2C flow's SI creation at manifest is what produces this.
- Settlement Forecast (PRD Section 10.3) is created at SI creation, against the Marketplace Order Map
- Net Receivables view (PRD Section 10.3.1) lists open Marketplace Order Map records grouped by marketplace and forecast settlement date
- Per-SKU margin breakdown (PRD Section 10.5) joins Settlement Lines to SI line items via the Map

# 13. Returns and Cancellations

Six distinct flows depending on the (B2B vs B2C) × (cancellation vs return) × (pre-dispatch vs post-dispatch) matrix. Each flow produces a different ERPNext artefact and must be deterministic enough that the recon engine can correlate it to settlement events. Returns and cancellations are where most reconciliation pain hides — they are the single biggest source of variance in industry benchmarks.

## 13.1 The matrix

| Flow | ERPNext artefact | Stock impact |
| --- | --- | --- |
| B2B cancellation pre-dispatch | Cancel SO; release SRE | None (stock was reserved, not moved) |
| B2B cancellation post-dispatch | Credit Note against SI; reverse Delivery Note (if separate) | Stock returned to source warehouse via SR |
| B2B return | Sales Return (SR) referencing SI | Stock returned via SR |
| B2C cancellation pre-manifest | No ERPNext artefact yet (SI not created); EE marks order cancelled, no integration follow-up | None |
| B2C cancellation post-manifest | Credit Note against the SI created at manifest | Stock back via Credit Note (with update_stock) |
| B2C return (RTO or customer return) | Sales Return referencing SI; stock back to warehouse | Stock returned via SR |

## 13.2 B2B cancellation pre-dispatch

1. Triggered by: cancellation in EE before any dispatch event
1. EE webhook order.cancelled or polling detects status change
1. Resolve EE order ID to ERPNext SO via ecs_easyecom_so_id
1. If SI not yet generated: cancel the ERPNext SO (move to Cancelled status)
1. Release the Stock Reservation Entry
1. If a Sales Invoice was created (Branch A pre-dispatch invoice case): cancel the SI; create reversing journal if SI was already submitted
1. Notify FDE if cancellation came in unexpectedly late (post-invoice)

## 13.3 B2B cancellation post-dispatch

Goods are out the door but recalled before customer receipt:

1. EE marks order cancelled and goods en route or at courier
1. Goods physically return to warehouse, processed via EE return-receipt
1. Integration creates a Credit Note against the original Sales Invoice in ERPNext
1. Credit Note carries reverse_stock = True so inventory is added back at the original valuation
1. Marketplace Order Map status moves to Returned
1. Recon engine treats this exactly like a marketplace return event

## 13.4 B2B return (post-delivery, customer-returned goods)

1. Customer initiates return via the seller's process or via EE
1. Return shipment dispatched back to warehouse
1. EE return-receipt event fires (with quality inspection results)
1. Integration creates a Sales Return (Sales Invoice with is_return = True) referencing the original SI
1. Stock returned to source warehouse OR to a quarantine warehouse (configurable)
1. Refund process handled via Payment Entry — the SR creates a debit balance, payment entry clears it
1. Quality-failed items: rejected_qty pattern routes them to Rejected Warehouse instead of standard return location

## 13.5 B2C cancellation pre-manifest

Simple case: no integration involvement at all. Order cancelled in EE before manifest creation; no ERPNext artefact ever existed. EE pre-manifest cancellations do not require any ERPNext action.

- Polling continues to fetch order list; cancelled orders are filtered out from the manifest poll
- If a cancelled order's SI was somehow created (shouldn't happen but safety check): integration detects mismatch and raises Integration Discrepancy

## 13.6 B2C cancellation post-manifest

1. EE order is post-manifest (so SI exists in ERPNext) but cancelled before customer accepts delivery
1. Common case: courier-side RTO before delivery
1. EE return-receipt event fires (RTO)
1. Integration creates Credit Note against the SI in ERPNext
1. Credit Note has reverse_stock = True; stock returns to warehouse at original valuation
1. Marketplace Order Map status: Cancelled-RTO
1. This event correlates to settlement file Refund event from the marketplace — recon engine matches via marketplace_order_id

## 13.7 B2C return (post-delivery)

1. Customer returns the product to the marketplace; marketplace ships back to seller's warehouse
1. EE return-receipt event with return_reason and inspection result
1. Integration creates a Sales Return referencing the original SI
1. Stock impact: depends on inspection result
- Accepted returns: stock back to default warehouse at original valuation
- Damaged returns: stock to a Damaged Returns Warehouse (configurable)
- Refused / lost returns: NO stock back; ERPNext Sales Return shows zero qty; financial reversal proceeds; an Integration Discrepancy of type Return-Without-Goods-Receipt is raised — see PRD Section 9.2.3

## 13.8 The Return-Marked-Goods-Not-Received case

This is the single most material reconciliation pattern. The marketplace deducts a refund from settlement (financial side: money taken from seller) but no physical goods come back (operational side: nothing returned). Industry benchmarks suggest this case alone accounts for 0.5-1.5% of GMV in lost inventory across most marketplace sellers.

- Detected by integration: settlement file shows Refund event; no corresponding EE return-receipt within configurable threshold (default 30 days)
- Integration raises an Integration Discrepancy of severity Error
- Recon engine creates a corresponding Discrepancy for FDE-led claim filing with the marketplace
- Methodology default (per PRD Section 3.5): unreceived returns past 30 days are reclassified as Lost Inventory and posted to Shrinkage account

## 13.9 Refund accounting

- Credit Note creates a debit balance in the Customer (or pseudo-customer) account
- Refund settled via Payment Entry: the marketplace's settlement file Refund event maps to a payment outflow that clears the debit
- For B2C: the marketplace handles refund to buyer; the seller's books only show Customer Refund Payable until the settlement deduction
- For B2B: refund processed directly to customer via Payment Entry against the SR's outstanding

## 13.10 Cancellation/return audit trail

Every cancellation/return artefact carries:

- ecs_easyecom_event_type — cancellation_pre_dispatch / cancellation_post_dispatch / return / rto
- ecs_easyecom_event_id — the EE event ID
- ecs_original_sales_invoice — link to the SI (for SR or CN)
- ecs_original_sales_order — link to the SO (for B2B)
- ecs_easyecom_return_reason — verbatim reason text from EE
- ecs_inspection_result — accepted / damaged / refused / lost

## 13.11 Reconciliation correlation

Each cancellation/return artefact must correlate to a Settlement Line for the recon engine to close the loop:

- B2B SR → no settlement file involvement (B2B is not on marketplace)
- B2C Credit Note (post-manifest cancellation, RTO) → marketplace Settlement Line of type Refund or RTO-Charge
- B2C Sales Return (customer return) → marketplace Settlement Line of type Refund + Return-Commission-Reversal
- Settlement Line carries marketplace_return_id; recon engine joins on this to find the matching ERPNext artefact
- Mismatched cases (SR exists but no settlement event, or vice versa) become reconciliation Discrepancies of the Return Without Credit Note or Return Marked Goods Not Received types per PRD Section 9.2.3

# Part IV — Cross-Cutting Concerns

*Behaviours that span every integration: multi-company isolation, failure recovery, and scale.*

# 14. Multi-Company Specifics

Frappe and ERPNext model legal entities as Company records. Many of our target clients run more than one legal entity from a single EasyEcom account. The integration must isolate data per Company while permitting authorised cross-Company views. This section specifies the topology, isolation, sharing, and permission rules. There is one model; the number of Companies a deployment spans is simply a parameter of it.

## 14.1 The topology (one model)

- **One EasyEcom Account per deployment.** It is the credential and sync boundary: one credential set, JWTs minted per location_key. (Section 3.)
- **One primary location per account**, holding account-global masters (Items, Customers, Suppliers, Tax Categories). Master sync runs against it; masters are shared across all Companies via ERPNext's native cross-company sharing. (Section 8.)
- **Operational locations** each carry a company value in EasyEcom and resolve to a Frappe Company via EasyEcom Location.frappe_company. Resolution is many-to-one: several locations may resolve to the same Company.
- **The number of distinct Companies is just a count.** A single-entity client resolves every operational location to one Company; a multi-entity client resolves them to several. The wiring is identical; only the count differs. The account holds one credential set regardless of how many Companies its locations resolve to.
- **Company identity is always derived from the location**, never assumed from a one-to-one location↔Company correspondence and never read from any other source.
- **The primary location may also be operational.** When it is, it resolves to a Company like any other operational location. When it is master-only, its company value is recorded but not used operationally.

A single Frappe site hosts the deployment. Within it, one Company exists per legal entity that any operational location resolves to. Warehouses are created under those Companies and mapped to locations through the Source-of-Truth Map (Section 8.4), which is per-location and deliberately partial.

## 14.2 Per-Company isolation

- EasyEcom Account: one per deployment; holds credentials and account-wide configuration.
- Per-Company settings record: one per operational Company; holds alert recipients, assigned FDE, and per-Company overrides.
- EasyEcom Location: one per location_key; carries frappe_company (nullable, non-unique) resolving the location to its Company.
- Marketplace Account: per Company. Composite unique key (company, marketplace, marketplace_seller_id).
- All operational records (Sync Record, API Call, Webhook Event, Queue Job, Replay Plan) carry company as a link field, populated by resolving the originating location. It is mandatory for entity-sync work; foundational API Call rows (Section 7.7) are the sole account-scoped exception and leave it blank.
- All Frappe queries in the integration filter by company explicitly — never reliant on global session state.
- Database-level: every operational table is indexed on (company, ...) as the leading composite key for query performance and isolation.
- User Permissions ensure a Company A user cannot see Company B sync state or sync logs.

## 14.3 Permission model (concrete rules)

Five roles defined by the integration, layered on top of Frappe's built-in System Manager:

| Role | Read scope | Write scope | Special permissions |
| --- | --- | --- | --- |
| EasyEcom Operator | Operational records (Sync Record, API Call, Webhook Event) for assigned Companies | Acknowledge, resolve discrepancies; mark webhooks as manually handled | None |
| EasyEcom FDE | All operational records + Account (read-only) + per-Company settings for assigned Companies | All operator powers + Retry Now, Force Resync, Mark as Already Synced, create Replay Plans, edit Field Mapping | Cursor rewind |
| EasyEcom Replay Approver | Same as FDE | Same as FDE + Commit Replay Plans affecting >100 records or financial impact >₹100k | Mark-Manually-Resolved replay strategy |
| EasyEcom System Manager | All records all Companies + the EasyEcom Account | All Account sections including credentials and webhook secrets; all per-Company settings | Cache management, point-in-time queries, Configuration Audit access |
| Auditor (read-only) | Configuration Audit, Field Mapping Versions, all operational records (read-only) | None | Cannot trigger any actions |

User Permissions: every role except System Manager requires a User Permission row scoping the user to specific Companies. The role assignment alone grants no access; access requires (role) AND (User Permission row for the Company). Global, non-Company-specific libraries (Field Mapping, Error Translation, SLA Budget) are editable by System Manager; the methodology team operates through that role.

## 14.4 Master sharing across Companies

Masters are account-global (synced once at the primary location) and shared across Companies using ERPNext's native semantics. The integration adds no per-Company duplication of masters.

| Master | Shared or scoped | Notes |
| --- | --- | --- |
| Item | Shared (account-global) | One Item record; ecs_easyecom_mappings holds the single EE master product reference |
| Customer | Shared (with per-Company Customer Defaults) | One Customer; ecs_easyecom_customer_id holds the EE reference |
| Supplier | Shared (with per-Company Supplier Defaults) | One Supplier; ecs_easyecom_mappings keyed by EE vendor id |
| Warehouse | Per-Company | Standard ERPNext: Warehouse belongs to a Company. Mapped to a location via the Source-of-Truth Map |
| Marketplace (flat channel list) | Shared | One flat list keyed by EE marketplace_id; applies across Companies |
| Marketplace Account | Per-Company | Seller IDs differ per Company |
| Tax Category | Shared | Per India Compliance app |
| Field Mapping | Shared by default; per-Company overrides supported via company_scope child table | Override priority: per-Company > global |
| Error Translation | Shared by default; per-Company overrides supported | Override priority: per-Company > global |
| SLA Budget | Per-Company | Each Company's commitment is its own |

## 14.5 Cross-Company transfers

- Stock movements crossing Company boundaries use ERPNext's Inter-Company flow (Sales Invoice on selling Company + Purchase Invoice on receiving Company)
- Each Company's side independently applies the integration based on its Source-of-Truth Map
- On the EasyEcom side this is a movement between two locations that resolve to different Companies; it appears as a dispatch from the source location and a GRN at the destination location
- Correlation: ecs_intercompany_link field on both the SI and PI carries the same UUID so the two halves can be correlated

## 14.6 GST registration per Company

- Each Company has its own GSTINs (one per state of registration via India Compliance)
- Each Marketplace Account holds the relevant GSTIN for the seller account
- Tax categories and place-of-supply rules apply per-Company
- e-invoice and e-waybill issuance is scoped to the Company's GSTIN

## 14.7 FDE assignment

- Each operational Company has an assigned_fde field (Link to User) on its per-Company settings record — drives auto-routing of Discrepancy creation
- FDEs see only their assigned Companies in standard list views (User Permissions enforce this)
- System Manager bypasses per-Company FDE assignment for cross-Company views
- Workload metrics computed per-FDE: count of assigned Companies × open-discrepancy-weight (Section 22.6)

## 14.8 Cross-Company operational surface

Cross-references to Section 22, which specifies the cross-Company Workspace, cross-Company reports, and configuration-drift detection for deployments spanning more than one Company. These views activate automatically when a deployment resolves to more than one Company.

# 15. Failure Modes and Recovery Playbooks

A catalogue of every recurring failure pattern with the documented FDE response. This is the operational playbook — when something goes wrong in production, the FDE looks here first.

## 15.1 Connection-level failures

| Failure | Detection | FDE response |
| --- | --- | --- |
| EasyEcom API down (5xx for > 15 min) | Connection Health dashboard turns red; alert fires | Verify EE status; if confirmed outage, no action needed (queue will catch up); if our connectivity issue, escalate to Frappe Cloud |
| Rate limit (429) sustained | Health dashboard shows elevated error rate | Reduce per-Company throttle setting; investigate if a runaway sync is causing |
| Authentication failure persistent | Auth errors logged; API Call entries red | Verify credentials in the EasyEcom Account; rotate api_key with EE if compromised; clear JWT cache |
| Webhook token mismatch | Webhook events rejected with 401 | Verify webhook_token matches EE-side configuration; rotate if necessary |
| Webhook endpoint unreachable | EE retries fail; webhook gap visible in cursor lag | Check Frappe Cloud routing; verify endpoint URL in EE webhook settings |

## 15.2 Master sync failures

| Failure | FDE response |
| --- | --- |
| Item push fails — HSN missing | Add HSN to Item; retry from Force Resync action |
| Item push fails — UoM not mapped | Add the UoM mapping (UoM lookup / Field Mapping) and retry |
| Item push fails — variant template not synced | Sync template first; then variants follow |
| Customer push fails — GSTIN format invalid | Correct GSTIN; retry |
| Supplier push fails — supplier_group not mapped | Configure supplier_group mapping (or use default); retry |
| EE category not in EasyEcom Category Map | Add mapping row for the new EE category; retry sync of all affected items |
| Conflict — both sides edited the same field | Per ownership matrix: non-owning side's change rejected. Integration Discrepancy raised. FDE reviews and decides whether to force-update from owning side or reverse the change |

## 15.3 Buying flow failures

- PO push fails (precondition unmet) — fix master sync and resubmit
- PO push fails (EE rejects post-validation) — inspect Queue Job; usually master mismatch; FDE corrects and retries
- GRN webhook missed — automatic catch-up via 30-min poll
- GRN ingestion partial-failure (PR built but submit failed) — fix root cause (often account configuration), retry the Queue Job
- Out-of-order: GRN arrives before EE PO is fully created — Queue Job loops up to 6 times waiting; manual investigation if persistent
- Over-receipt — fail PR submission; FDE amends PO or splits the GRN

## 15.4 Sales flow failures

- B2B SO push fails (Sync mode) — user sees error immediately; fix and resubmit
- B2B SO push fails (Async mode) — Queue Job in Failed; FDE inspects, fixes, retries; meanwhile SO is valid in ERPNext but EE doesn't know
- B2B inventory reserve event mismatch — ERPNext shows lower available than EE expects; SRE creation fails; FDE investigates inventory drift
- B2C manifest event without a matching order in our pull cache — pull orders explicitly first, then process the manifest
- B2C SI creation fails (item not synced) — fix sync, retry Queue Job; meanwhile EE has already dispatched, so the SI will lag dispatch
- e-invoice IRN failure — India Compliance auto-retries; FDE investigates if persistent (often IRP API issues)

## 15.5 Returns and cancellations failures

- Cancellation arrives but original SO/SI not in ERPNext — out-of-order; loop with retries
- SR creation fails — usually Item-level setting mismatch (batch tracking, etc.); fix and retry
- Return-without-goods — not a failure but a Discrepancy; recon engine routes to claim queue per PRD Section 9.4
- Refund Payment Entry fails — usually account configuration; fix and retry

## 15.6 Inventory drift

Periodic full-sync detects and reports drift between ERPNext stock balance and EasyEcom inventory:

- Daily full inventory pull from EasyEcom (/inventory/getInventoryDetailsV3)
- Compared per (Item, location) against ERPNext Bin balance
- Variance > tolerance (default 1 unit) raises Integration Discrepancy of severity Warning
- FDE investigates — common causes: missed webhook, manual override on one side, inter-Company transfer not reflected
- Resolution: usually a Stock Reconciliation in ERPNext to align with EE; never the reverse direction (EE inventory adjusted from ERPNext) without explicit approval

## 15.7 Critical-severity incidents

- Data-loss potential (e.g., webhook indicates GL-impacting event but ingestion failed) → page the on-call FDE immediately
- Cross-Company data leak (Company A user sees Company B data) → kill switch on user; investigate; report
- Credential compromise (api_key leaked, JWT exposed) → rotate immediately; audit all API Call entries since last rotation
- Sustained data divergence (>10 Discrepancies/day for >3 days on one Company) → engage methodology team for root-cause

# 16. Performance and Scale Envelope

This section specifies measurable performance commitments per scale tier, the testing methodology to validate them, and the operational levers (Settings tunables) that govern behaviour under load. These commitments are contractually meaningful — pilot sign-off requires demonstrating each.

## 16.1 v0.1 commitments

Tested and signed off before pilot go-live:

| Metric | v0.1 commitment | How tested |
| --- | --- | --- |
| Order pull latency | 5-min cadence; orders visible within 5-7 min of EE manifest | Synthetic load: 1000 orders/day mix |
| Stock sync staleness | Hourly; max 60-min lag | Continuous polling + drift detection |
| Webhook processing latency | 200 OK in < 200 ms; full processing within 30 s | Load test: 100 webhooks/min burst |
| GRN-to-PR latency | Within 30 min of EE Mark Complete | Polling cron interval |
| B2C SI creation | Within 5 min of manifest event | Manifest webhook + 5-min poll |
| Sales Order push (Async) | Eventually consistent; typical 5-30 s | Standard load |
| Sales Order push (Sync) | < 2 s p95 | Standard load |
| Daily full inventory recon | Completes within 1 hour for 50k SKUs | Synthetic large-catalogue test |
| Master sync (single Item push) | p95 < 800 ms end-to-end (Frappe save → EE confirm) | Loop test: 1000 sequential pushes |
| Field Mapping execution overhead | < 5 ms per record on cached compiled rulesets | Microbenchmark |
| Schema drift hash computation | < 2 ms per response | Microbenchmark |
| Morning Brief generation | Completes within 5 min for any single Company | Daily cron timing observed |

## 16.2 Per-site scale envelopes

- v0.1: tested at 5,000 orders/month, 10,000 SKUs, 5 marketplaces, 1 Company
- v0.5: tested at 30,000 orders/month, 50,000 SKUs, 8 marketplaces, 5 Companies (aggregator)
- v1.0: tested at 100,000 orders/month, 100,000 SKUs, 10 marketplaces, 10 Companies

## 16.3 Frappe Cloud sizing recommendations

Frappe Cloud tier guidance:

| Client profile | Frappe Cloud tier | Background workers | Notes |
| --- | --- | --- | --- |
| Pilot client (v0.1 scale) | Pro | 2 default + 2 additional | Sufficient for 5k orders/month |
| Production client (v0.5 scale) | Pro or Business | 4 default + 4 additional dedicated to integration | Aggregator clients need Business |
| Aggregator (v1.0 scale) | Business minimum | 8 dedicated workers; consider Premium for >50k orders/month | Database tuning may also be needed |

## 16.4 Memory and concurrency envelopes

- Worker memory ceiling: each background worker stays under 512 MB resident memory; jobs streaming large payloads (e.g., bulk Item sync) use chunk-based processing
- API client connection pool: 10 connections per worker per location_key; reused across calls within a worker process
- Redis cache budget: 100 MB per Company for compiled Field Mappings, JWT tokens, master ID lookups
- Database connection pool: shared with Frappe; integration code uses Frappe's connection — no separate pool

## 16.5 Backpressure handling

- Queue depth >Settings.queue_depth_warning_threshold (default 500): scheduler logs Warning and continues
- Queue depth >Settings.queue_depth_critical_threshold (default 1000): scheduler temporarily pauses pull cycles for that Company until depth drops below 50% of threshold; webhook receipt continues unaffected
- Webhook receiver always returns 200 within 200 ms regardless of queue depth (processing fully decoupled)
- Per-Company throttle on outbound EE calls: Settings.max_throughput_per_sec (default 30) enforced via token bucket in Redis
- Persistent backpressure (queue at critical for >30 min) surfaces as a Critical alert per Section 18

## 16.6 Timeout configuration

- HTTP request timeout to EasyEcom: 30 seconds connect + 60 seconds read (configurable per endpoint)
- Webhook receiver max processing time: 200 ms (synchronous portion); deferred work goes to queue
- Job execution max wall time: 5 minutes per attempt (kill and retry if exceeded; classify as transient)
- Scheduler tick interval: 60 seconds (Frappe default; not changed)

## 16.7 Rate limit handling

- EasyEcom rate limits are tiered and documented (Section 3.10); the integration throttles to the account's rate_limit_tier and tracks daily-quota consumption to stay under the cap
- On 429 response: read Retry-After header if present, otherwise back off 60 seconds
- On sustained 429 (>5 in 60 seconds): pause that endpoint group for that Company for 5 minutes
- Token bucket implementation in Redis prevents our own runaway syncs

## 16.8 Stress testing approach for v0.1 sign-off

1. Synthetic data generation: tooling to populate a test EE sandbox + ERPNext site with 1000 orders, 5000 SKUs, all flow types
1. End-to-end flow test: order placed → manifest → SI → settlement file upload → recon. All 10 flows executed at least 100x each
1. Burst test: 200 webhooks/min for 30 minutes — verify no drops, no duplicates, no out-of-order processing failures
1. Failure-injection test: random EE API errors (rate limit, 5xx, auth fail) — verify auto-recovery within tolerance
1. Concurrency test: 4 simultaneous workers per Company processing pulls + pushes + webhooks — verify no race conditions, no double-processing
1. Memory soak: 24-hour run at sustained 500 orders/hour — verify no memory leak, RSS stays under ceiling
1. Data integrity audit: full reconciliation of test-site state vs expected state after stress run

## 16.9 Performance regression detection

- Per-endpoint p50/p95/p99 latency tracked in API Call records
- Hourly cron computes rolling 24-hour percentiles per endpoint
- Regression detected when p95 deviates >2× from prior 7-day baseline
- Alert per Section 18 (Warning severity by default; Error if affecting a flow with active SLA Budget)

# Part V — The Operational Surface

*What the FDE sees and uses: the workspace, alerts, tooling, and the views that make the integration observable and operable.*

# 17. Operational Surface

The operational surface is the set of UI elements, dashboards, action menus, and document affordances that an FDE or operator interacts with daily. The first seventeen sections of this spec define what the integration does. This section defines what the operator sees while it does it. The two are co-equal — an integration that does the right thing but exposes nothing to the operator is operationally a black box.

Design philosophy across this section: every state in the integration is reachable from at most three clicks from the EasyEcom Workspace. Every operational action an FDE might want to take is a button somewhere in the UI, not a bench command. Every error has a route from the alert that fired it to the underlying record that caused it. None of this is true of typical Frappe integrations; we are deliberately raising the bar.

## 17.1 Design philosophy

- Every flow has a UI surface. If a flow exists in the integration, the operator can see its current state, history, and pending work in the desk
- Every action is reversible or replayable. No destructive button is one click away; every retry shows what will happen before committing
- Every error has a story. From the moment an alert fires, the operator can navigate alert → underlying error → relevant document → suggested remediation in a single thread
- Every screen states its scope. Multi-Company users always know which Company they're looking at; Sandbox users always know they're in Sandbox
- Information density over decoration. Lists show enough columns to make decisions without opening rows; status badges use colour and icon, not just colour
- No vendor-staring. The FDE should be doing investigation in our UI 95% of the time, not in the EasyEcom dashboard

## 17.2 The EasyEcom Workspace

Frappe v16's Workspace primitive is the home screen. We ship a default EasyEcom Workspace shipped as a fixture, customisable per Company. Layout convention:

### 17.2.1 Top row — environment and connection

- Environment badge: large, coloured. Sandbox = orange, Production = green. Cannot be missed
- Connection status per Company: green/yellow/red dot per Company in scope, with last-successful-call timestamp
- Pause All Syncs kill-switch: the panic button. One click pauses all background syncs for the current Company; surfaces a desk-wide banner so the operator never forgets it's paused

### 17.2.2 Number Cards row

Seven live KPI tiles, refreshed every 60 seconds (cache-aware via v16 Caffeine). Each tile clickable to its underlying report. The tiles, in order:

- Open Sync Records (failed) — red number. Click to filter Sync Record list to status=Failed
- Partial Jobs (last 24h) — count of Queue Jobs in Partial state, i.e. batches where some records failed. Click to the Queue Job list filtered to state=Partial; each opens to its failed children (Section 7.4)
- API Calls last 1h — total count + success rate. Click to API Call list filtered to last 1h
- Webhook Events last 1h — total count + processed-vs-pending split. Click to Webhook Event list
- Queue Job depth — current count of Queued + Retrying. Click to Queue Job list filtered accordingly
- Cursor lag (max across all pull cursors) — minutes behind real-time. Click to Sync Cursor list
- Open Integration Discrepancies — count, with financial impact rolled up. Click to Discrepancy list

### 17.2.3 Dashboard Charts row

Three multi-day trend charts:

- API success rate over last 7 days, hourly granularity, stacked per endpoint group (auth/orders/inventory/grn/returns)
- API call volume over last 7 days, with the same grouping. Anomaly badges where today's volume diverges from the previous-7-day baseline by >2σ
- Sync Record state distribution over last 30 days — stacked area of Pending/Success/Failed/AlreadySynced per day

### 17.2.4 Shortcut tiles

Direct navigation to the lists and tools FDEs use most:

- EasyEcom Account (account-level configuration) and per-Company settings
- EasyEcom Field Mapping list
- Sync Record list (with default filter to last 24h)
- API Call list
- Webhook Event list
- Queue Job list
- Integration Discrepancy list
- Replay Plan list
- Morning Brief (today's snapshot)
- Error Translation library
- Configuration Audit log

### 17.2.5 Onboarding section

For Company-wise integration setup, an inline checklist using Frappe v16 Onboarding Steps:

- Configure EasyEcom Account → Setup section
- Configure EasyEcom Account → Sync Tuning section
- Configure EasyEcom Account → Webhook Auth section
- Configure EasyEcom Account → GRN Policy section
- Configure per-Company settings → Alerts section
- Pull Locations from EasyEcom
- Map every Location to a Frappe Warehouse via Source-of-Truth Map
- Pull Channels from EasyEcom
- Configure Marketplace Accounts per (Company, Marketplace) in use
- Run sandbox smoke test (PO + GRN + B2C order + cancellation + return)
- FDE sign-off on pre-flight checklist
The checklist is not a wizard — it's a live-state checklist that auto-checks completed items by inspecting actual configuration. An FDE can return to it any time and see what's left.

## 17.3 Number Cards

Beyond the six on the Workspace, additional Number Cards for embedding in other dashboards or Workspaces:

- Today's API call volume by Company
- Average API latency last 1h by endpoint
- Sync Record retry rate (Sync Records that needed >1 attempt)
- Webhook receipt rate vs expected (heuristic — flags sustained anomaly)
- SLA compliance percentage by flow over current week
- Integration Discrepancy ageing (count >24h, >7d, >30d)
- Field Mapping coverage (% of fields in real payloads matched by an explicit rule)
- Schema drift count — number of new schema variants observed last 7d
- Cross-Company sync activity (for aggregators) — calls per Company today
- Top Error Translation hits (which translated errors are firing most)

## 17.4 Dashboard Charts

In addition to the three on the Workspace, charts available for ad-hoc dashboards:

- API latency p50/p95/p99 over time per endpoint
- Queue Job state transitions over time (Queued → Running → Success/Failed) — sankey-style
- Per-flow success rate over time (Master sync, Buying, B2C sales, etc.)
- Integration Discrepancy created vs resolved over time
- Webhook gap heatmap — calendar view showing minutes between consecutive webhooks per type per day
- Per-marketplace sync health — small multiples, one chart per Marketplace Account
- Per-Item sync history — for a selected SKU, every Sync Record event over time

## 17.5 Saved Reports

Frappe Query Reports and Report Builder views shipped as fixtures. Each is FDE-customisable; clients can save filter presets via Frappe's Saved Filter Name feature.

- Daily Sync Health — per Company, per flow: total attempted, succeeded, failed, retried; mean and p95 latency; cursor lag at end-of-day
- Failed Jobs by Reason — aggregated by translated-error-key over selectable time window. The triage report
- Latency by Endpoint — every endpoint's count, mean, p50/p95/p99 latency, error rate
- Drift by SKU — Items where ERPNext stock and EasyEcom stock disagree, sorted by absolute drift
- Discrepancies by Severity and Age — Integration Discrepancies grouped by severity, with ageing buckets
- Field Mapping Coverage — per ruleset: explicitly mapped %, identity-matched %, dropped %, with sample dropped fields
- Webhook Reliability — per webhook type: received count, processed count, failed count, dedup count, gap-detection results
- SLA Compliance — per Company per flow per week: target %, actual %, breach count, breach financial impact
- API Call Audit — full-text searchable across redacted request and response bodies; for forensic investigation
- Replay History — every Replay Plan executed: filter, dry-run result, commit result, who executed
- Configuration Change Log — every Settings, Field Mapping, Source-of-Truth Map change with before/after
- Cross-Company Health Rollup — for aggregator FDEs: side-by-side health view across all managed Companies
- Morning Brief Archive — historical Morning Brief snapshots for retrospectives

## 17.6 Action menus on operational DocTypes

Frappe's Actions dropdown on every operational DocType, with role-gated entries:

### 17.6.1 Sync Record actions

- Retry Now — re-attempts the sync with current configuration
- Retry With Override — opens a dialog to set override values for the retry attempt (e.g., manually correct the WMS code before retry)
- Mark as Already Synced — accepts an EasyEcom-side ID; closes the Sync Record without an API call. Used after FDE manually fixes the EE side
- Force Resync — pulls the EasyEcom-side state and pushes ERPNext state, even if hashes match (for suspected drift)
- View ERPNext Document — opens the underlying ERPNext doc in a new tab
- View EasyEcom Record — opens the EE-side record in a new tab via deep link
- View Related API Calls — filtered API Call list for this Sync Record's correlation ID
- View Translated Error — if status=Failed, jumps to the Error Translation entry that matched
- Cancel Sync — for Pending Sync Records, marks as Cancelled, no further attempts

### 17.6.2 API Call actions

- Replay This Call — re-issues the exact same request (idempotency-key-aware; identical responses are detected)
- View Request Payload — opens a JSON viewer with copy button
- View Response — same
- Download Trace — exports correlation-ID-bound records (this API Call, parent Sync Record, related Webhook Events) as JSON for offline analysis
- View Translated Error — if status=Failed

### 17.6.3 Webhook Event actions

- Reprocess — runs the original payload through the current ingestion logic (useful when ingestion has been bug-fixed since)
- View Raw Payload — JSON viewer
- View Downstream Documents — list of Frappe documents created/updated by this webhook's processing
- Mark as Manually Handled — for webhooks the FDE has resolved out-of-band
- View Auth Result — debug pane showing which header carried the token and whether it matched

### 17.6.4 Queue Job actions

- Retry Now — same as Sync Record Retry, scoped to the job
- Cancel — for Queued or Retrying jobs
- View Stack Trace — for Failed jobs, full Python traceback
- Bulk Retry — from list view, retries all selected Failed jobs
- Bulk Cancel — same for selected Queued jobs

### 17.6.5 Integration Discrepancy actions

- Acknowledge — moves to In Review status, assigns to current user
- Resolve — captures resolution note and closes
- Escalate — re-routes to a different alert recipient list
- Link to Recon Discrepancy — if this Integration Discrepancy has a downstream recon impact
- View Suggested Actions — from Error Translation entry
- Create Replay Plan — generates a Replay Plan pre-filtered to the affected records

## 17.7 Connections panel on every business document

Frappe v16 supports a Connections panel on the right side of every document. We populate it with integration-specific links:

### 17.7.1 On Sales Invoice

- EasyEcom Order — link to the EE order ID
- Marketplace Order Map — the recon join record
- Settlement Forecast — expected settlement
- Sync Records — full sync history of this SI
- API Calls — every call related to creating/updating this SI
- Webhook Events — every webhook that touched this SI (manifest, dispatch, return)
- Integration Discrepancies — any open Discrepancies referencing this SI
- Recon Discrepancies — downstream recon-engine Discrepancies
- Event Timeline — the chronological view (Section 17.8)

### 17.7.2 On Purchase Receipt

- EasyEcom GRN — link to the EE GRN ID
- Source Purchase Order — the parent PO
- Sync Records, API Calls, Webhook Events
- Event Timeline

### 17.7.3 On Sales Order

- EasyEcom Order — if pushed
- Stock Reservation Entries — if mirrored from EE inventory-reserve
- Settlement Forecast
- Sync Records, API Calls, Webhook Events
- Event Timeline

### 17.7.4 On Purchase Order

- EasyEcom POs — list of all EE POs spawned from this ERPNext PO (multi-warehouse case)
- Purchase Receipts — all PRs received against this PO
- Sync Records, API Calls, Webhook Events

### 17.7.5 On Item / Customer / Supplier / Warehouse

- EasyEcom Mappings — the per-Company per-location mapping rows
- Sync Records — full master sync history
- Field Mapping Used — link to the ruleset that governs this entity's sync
- Last Sync, Next Scheduled Sync
- Conflict History — past conflicts and how they were resolved

## 17.8 Event Timeline view

For any business document or any EasyEcom order ID, the FDE can open a chronological timeline showing every integration event that touched it, drawn from all three log DocTypes plus webhook events plus queue jobs. This is the single most operationally valuable view in the entire surface — it answers 'what happened to this order' end-to-end.

### 17.8.1 Anatomy

- Vertical timeline, newest at top
- Each event is a card showing: timestamp (with relative ago), event type icon, source (poll / webhook / push / manual), one-line description, status badge
- Cards are colour-coded by source: blue = inbound from EE, green = outbound to EE, grey = internal Frappe action, red = error
- Click any card to expand to full payload and metadata
- Correlation IDs prominently displayed; clicking a correlation ID filters timeline to that ID
- Time-range filter at top (last 1h / 24h / 7d / 30d / custom)
- Type filter (show only API Calls / Webhooks / Sync Records / Queue Jobs)

### 17.8.2 Where it surfaces

- Tab on Sales Invoice, Purchase Receipt, Sales Order, Purchase Order, Stock Entry
- Standalone page accessible via /app/easyecom-event-timeline?ee_order_id=<id>
- From any Sync Record / API Call / Webhook Event detail page via 'View Full Timeline' button

## 17.9 Inspector view

Paste any EasyEcom identifier (order ID, GRN ID, company_product_id, location_key, return ID, vendor ID) and see everything our system knows about it. The forensic tool — what an engineer reaches for when a client says 'order X is wrong'.

### 17.9.1 Inputs

- Identifier type: auto-detect from format, or explicit dropdown
- Company: defaults to current scope; multi-Company option for aggregator FDEs
- Time range: defaults to last 90 days; expandable

### 17.9.2 Outputs (for an order ID)

- Resolution: this EE order ID maps to ERPNext Sales Invoice X (or 'not yet ingested' / 'multiple matches' / 'cancelled')
- Current state on EasyEcom side: last pulled state, last pulled at, with 'Refresh from EasyEcom' button
- Current state on ERPNext side: SI status, total, customer, key dates
- Drift report: any field that differs between the two sides
- Event timeline: scoped to this identifier
- Related identifiers: marketplace_order_id, awb_number, manifest_id, settlement_batch (if any)
- Open Discrepancies: any integration or recon Discrepancy referencing this identifier
- Suggested actions: based on detected state — 'Force Resync', 'Replay manifest webhook', 'Create Replay Plan', etc.

## 17.10 Sync Now manual triggers

On-demand sync with explicit scope, accessible from the EasyEcom Account header strip Sync Now dropdown. Each option opens a modal:

### 17.10.1 Sync Items modal

- Scope dropdown: Selected Items / Items Modified Since / Items in Item Group / All Items
- If Selected: searchable Item picker (Make Item inline-create option for ad-hoc)
- If Modified Since: datetime picker (default last 24h)
- If Item Group: Item Group picker
- If All: confirmation dialog with count and ETA
- Direction: Push to EasyEcom / Pull from EasyEcom / Both
- Mode: Standard / Force (overrides hash check; pushes even if no change detected)
- Dry run option: shows what would happen without committing
- Continue button: enqueues the work and shows a toast 'Item sync job has been enqueued.' with a link to the resulting Queue Job

### 17.10.2 Other Sync Now options

- Sync Customers — same shape, scope = Selected / Modified Since / All
- Sync Suppliers — same
- Sync Tax Categories — full sync only (no scope; small dataset)
- Sync All Masters — runs Item, Customer, Supplier, Tax Category, Channel sequentially
- Pull Orders Now — bypasses cadence; pulls orders since last cursor immediately
- Pull GRNs Now — same for GRNs
- Pull Returns Now — same for returns
- Pull Inventory Snapshot — full inventory pull on demand (used during drift investigation)

## 17.11 Cache management

The integration uses Frappe v16's Caffeine cache for several hot paths: JWT tokens, mapped Item/Customer/Supplier IDs, Field Mapping compiled rulesets. Stale cache can produce hard-to-diagnose drift; the operator surface exposes cache control:

- Clear All Caches button on the EasyEcom Account (System Manager only); shows toast on success
- Clear JWT Cache button per EasyEcom Location (forces re-auth on next call)
- Clear Field Mapping Cache button on Field Mapping detail (forces recompilation)
- Clear Master ID Cache button on the EasyEcom Account (drops cached Item/Customer/Supplier ID lookups)
- Cache state inspector — read-only view of what's currently cached and when each entry was acquired
- Configuration Audit captures every manual cache clear (who, when, which cache)

## 17.12 Toast and inline feedback

Every operator action gives explicit visual feedback. No silent operations. Patterns:

- Async action enqueued: toast 'Item sync job has been enqueued.' with link to the Queue Job
- Sync action started: toast 'Pulling orders from EasyEcom...' with progress bar where applicable
- Sync action completed: toast 'Pulled 47 orders, 3 errors.' with link to detailed result
- Cache cleared: toast 'Cache cleared.' (simple confirmation)
- Configuration saved: toast 'Settings saved.' plus inline indication if the change requires sync restart
- Replay dry-run completed: results displayed inline with count of records that would be affected
- Error: toast in red with the translated error message and a 'Show details' link to full traceback

## 17.13 Multi-Company UI scoping

Critical for aggregator clients. The UI must never confuse the operator about which Company they're acting on:

- Active Company indicator in the page header (icon + name), always visible
- Switching Company via the standard Frappe Company switcher refreshes all integration views
- Cross-Company actions are explicit: cross-Company views surface automatically when the deployment spans more than one Company (Section 14.8); cross-Company write actions (bulk Replay Plans, cross-Company reports) require System Manager role
- Cross-Company list views show Company as the first column
- Audit log records the Company context of every action

# 18. Notifications, Alerts, and Escalation

Section 17 surfaces the integration's state visually. This section specifies how problems push themselves to operators when they're not actively looking. The vague 'alert FDE' language scattered through the v1.0 spec is replaced here with a concrete framework.

**Relationship to Frappe's built-in primitives:** Frappe ships `Notification` (a DocType for declarative event-driven alerts), `Notification Log` (per-user inbox), and `Email Queue` (the outbound email mechanism). The integration uses all three under the hood: every alert email goes through `frappe.sendmail()` which queues via `Email Queue`; every desk-bell notification creates a `Notification Log` row via `frappe.publish_realtime`. The Integration Alert DocType is a *layer on top* that adds what Frappe primitives do not provide: financial impact attachment (Section 23), explicit lifecycle states (Acknowledged → In Investigation → Resolved → Reopened), suppression and grouping rules, on-call rotations, and per-Company recipient configuration. The Integration Alert lifecycle is a methodology bet — operators care about the alert's *progress through resolution*, not just its emission.

## 18.1 Severity levels

Four severities, applied consistently across the integration. Severity drives routing, not whether an event is logged — every event is always logged regardless of severity.

| Severity | Definition | Default routing | SLA for response |
| --- | --- | --- | --- |
| Critical | Data loss possible, financial impact accruing, or recon engine cannot run. Examples: webhook token consistently failing (potential security issue), credentials compromised, sustained API failure > 30 min | Page to on-call FDE via SMS + email + Slack + dashboard banner | 15 min acknowledgement |
| Error | An operation has failed and won't auto-recover. Examples: SI creation failed for a manifest, GRN ingestion failed for a Purchase Receipt, master sync failed after retries | Email + Slack to FDE | 4 hours acknowledgement |
| Warning | An operation is degrading or trending toward failure. Examples: API error rate > 5% (15-min window), webhook gap > 60 min, queue depth > 500 | Email to FDE; appears on dashboard | 1 business day |
| Info | Notable but non-actionable. Examples: schema drift detected (new field in payload), Daily Sync Health summary | Email digest only; appears in Activity feed | No SLA |

## 18.2 Channel matrix

| Channel | Latency | Suitable for | Configuration |
| --- | --- | --- | --- |
| Frappe Notification | Real-time | All severities; in-desk notification bell | Auto-enabled for all FDE users |
| Email Alert | 1-5 min | Error and above; full context with links | Per-recipient configurable in Settings → Alerts |
| Slack webhook | Real-time | All severities; channel-routed by severity | Webhook URL in Settings → Notifications |
| SMS (via Twilio gateway, optional) | Real-time | Critical only; on-call escalation | Per-recipient phone in Settings → Alerts |
| Dashboard banner | Real-time | Critical and Error; visible to all users with relevant Company access | Auto-enabled when banner_show_to_all_users checked |
| Email digest (daily / weekly) | Scheduled | Info; rolled-up summary | Configured per-recipient in Settings → Alerts |

## 18.3 Per-Company alert configuration

Every Company configures its own alert recipients independently (per Section 3.5.2). This matters in the multi-Company case where one operations team may handle some Companies while another handles others. The system supports:

- Per-Company recipient lists per severity (Critical / Error / Warning recipients can be different sets of people)
- Per-channel recipient sub-lists (email-only recipients, Slack-only recipients, etc.)
- On-call rotations: a Recipient Group can be configured as a rotation with date ranges; the system routes to whoever is on-call at the time of the alert
- Escalation chains: if a Critical alert is not acknowledged within the SLA, escalates to a secondary recipient list
- Quiet hours: per-recipient configurable; non-Critical alerts queued until quiet hours end

## 18.4 Suppression and grouping

Without intelligent grouping, a single root cause can produce hundreds of alerts. Suppression rules:

- Identical alert (same alert_key) within 5 min: deduplicated; the second occurrence updates the existing alert's count, no new notification
- Alert flood detection: if > 50 alerts of the same alert_key fire within 15 min, suppression activates — only the first and a 15-min summary are sent
- Maintenance windows: configurable downtime windows during which non-Critical alerts are suppressed
- Manual snooze: an FDE can snooze an alert family for 1h / 4h / 24h with a required reason (logged)
- Acknowledgement stops re-alerting: once an alert is acknowledged, it does not re-notify until it transitions Resolved or Reopened
- Group-by rules: alerts of the same type within a sliding window are bundled — 'GRN ingestion failed for 7 PRs in last 10 min' instead of 7 separate alerts

## 18.5 Alert lifecycle

Every alert is a record (Integration Alert DocType, distinct from Integration Discrepancy though often linked) with its own lifecycle:

- Fired — alert created and notifications sent
- Acknowledged — operator clicked Acknowledge; pauses re-alerting
- In Investigation — operator opened the underlying record and is working on it
- Resolved — operator marked resolved with a resolution note (mandatory)
- Reopened — if the underlying condition recurs within 1h of resolution
- Auto-closed — for self-recovering alerts (e.g., API error rate dropped back below threshold), with an audit note

## 18.6 Alert templates

Each alert type has a structured template:

```
Alert: GRN Ingestion Failed (Error severity)

Company: <company>
Affected Purchase Receipt: <pr_link or 'not yet created'>
Source EasyEcom GRN: <grn_id> (link to inspector)
Translated error: <plain_english_explanation>
Suspected cause: <from_error_translation_entry>

Suggested actions:
1. <action_1>
2. <action_2>
3. <action_3>

Direct links:
- View Sync Record: <link>
- View API Call (last attempt): <link>
- View Event Timeline: <link>
- Create Replay Plan: <link>
- Acknowledge: <link>

Fired at: <ts>
Will re-alert at: <ts + 4h> if not acknowledged
```

The template is filled by the alert generator using context from the Sync Record, API Call, Webhook Event, and (critically) the Error Translation library. An alert that lands without suggested actions is a bug in the Error Translation coverage.

## 18.7 Daily and weekly digests

### 18.7.1 Daily digest

Sent to digest_recipients at the configured time (default 08:00 IST). One digest per Company. Contents:

- Yesterday's API call success rate vs 7-day baseline
- Yesterday's webhook receipt count vs 7-day baseline
- New Critical and Error alerts fired yesterday (with links)
- New Integration Discrepancies created yesterday with financial impact
- Open Discrepancies > 24h old (count by severity)
- Sync Records that have failed > 5 times (likely permanent failures needing FDE intervention)
- Pointer to today's Morning Brief (Section 24)

### 18.7.2 Weekly summary

Sent on Mondays at the configured time. Contents:

- Last week's SLA compliance per flow
- Last week's volume vs. previous 4-week baseline
- Top 5 Error Translation hits (which translated errors fired most)
- Schema drift summary (new variants observed last week)
- Field Mapping coverage report
- Configuration changes made last week (Settings, Field Mapping, Source-of-Truth Map)
- Replay activity summary

## 18.8 Alert configuration audit

- Every change to alert configuration (recipient lists, thresholds, channel routing) is captured in EasyEcom Configuration Audit
- Quarterly review prompt sent to System Manager: 'review your alert configurations for currency'
- Suppression actions (snooze, maintenance window) are also audited

# 19. Replay and Recovery Tooling

When a flow has failed for many records (master sync didn't propagate after a credential rotation; manifest webhooks were missed during a 4-hour EasyEcom outage; a Field Mapping bug produced bad payloads for a day), the FDE needs more than a per-record Retry button. They need a tool to reason about the failure space, decide what to fix, preview the fix, and commit. This section specifies that tool.

## 19.1 The Replay Plan DocType

A Replay Plan is the FDE's working surface for batch recovery. Lifecycle: Draft → Dry Run → Reviewed → Committed → Completed (or Cancelled). Multi-step on purpose; nothing irreversible happens until Commit.

| Field | Type | Notes |
| --- | --- | --- |
| plan_name | Data | FDE-given name; e.g., 'Replay missed manifests 2026-04-30 outage' |
| company | Link → Company |  |
| target_doctype | Select | Sync Record / API Call / Webhook Event / Queue Job / Marketplace Order Map |
| filter | JSON | The filter expression that selects affected records |
| affected_count | Int (computed on Filter) |  |
| strategy | Select | Retry-As-Is / Retry-With-Override / Reprocess / Force-Resync / Mark-Manually-Resolved |
| override_values | JSON (optional) | Field-level overrides for Retry-With-Override strategy |
| dry_run_state | Select | Not Run / Running / Completed / Failed |
| dry_run_results | JSON | Per-record predicted outcome from dry run |
| dry_run_at, dry_run_by | Audit |  |
| commit_state | Select | Not Run / Running / Completed / Cancelled |
| commit_results | JSON | Per-record actual outcome from commit |
| commit_at, commit_by, commit_reason | Audit |  |
| throttle | Select | Aggressive (no throttle) / Standard (default rate limit) / Gentle (1/3 normal) |
| max_concurrency | Int | Override the Settings concurrency for this plan |

## 19.2 Strategies

### 19.2.1 Retry-As-Is

- Re-attempts the original operation with no changes
- Suitable for: transient failures (rate limit, brief outage) where the underlying records and config are correct
- Idempotency keys ensure no double-processing

### 19.2.2 Retry-With-Override

- FDE supplies override_values that replace specific fields before retry
- Override values can be literal (set hsn_code = '8517' for these 47 Items before retry) or expressions (set easyecom_company_product_id = ERPNext name + '-EE' for these 12 Items)
- Useful when source records have a fixable defect that's faster to override at retry time than to fix in the source data
- Override values are captured in audit trail so the divergence is traceable

### 19.2.3 Reprocess

- Specific to Webhook Event replay — re-runs the original payload through current ingestion logic
- Useful when ingestion code has been bug-fixed since the original webhook arrived

### 19.2.4 Force-Resync

- Pulls EasyEcom-side state and pushes ERPNext-side state regardless of hash equality
- Useful for suspected silent drift — when both sides claim to be current but differ

### 19.2.5 Mark-Manually-Resolved

- Closes the targeted records as resolved without retry
- Used when the FDE has fixed the underlying issue out-of-band (e.g., manually edited the EE record) and the integration just needs to stop trying
- Requires resolution_note for audit

## 19.3 Dry run mode

Before any Commit, a Dry Run is mandatory. Dry Run executes the strategy in a simulated mode that:

- Performs all read operations (fetch source data, evaluate Field Mapping, compute payloads)
- Does NOT perform any write operations against EasyEcom or ERPNext
- Does NOT advance any cursors or modify any records
- Captures predicted outcomes per record: would-succeed / would-fail / would-no-op (with reason)
- Captures the payload that would be sent for spot-check
- Surfaces any rule failures or precondition violations
Dry Run results are persisted in dry_run_results (JSON, per-record).

### 19.3.1 Dry Run review surface

After Dry Run, the FDE reviews via a dedicated UI:

- Summary at top: predicted success count, predicted failure count, predicted no-op count
- Per-record table with predicted outcome, predicted error (if applicable), 'View Payload' inline
- Filter and search the per-record results
- 'Show Sample Payload' to inspect the would-be-sent payload for any record
- 'Compare Against Original' for Retry-With-Override: side-by-side diff of original vs. overridden payload
- Sign-off action: 'I have reviewed and accept the predicted outcomes'. Required before Commit

## 19.4 Bulk filter syntax

The filter field uses a structured filter expression (compatible with Frappe's get_list filter format) plus several integration-specific extensions:

- Standard Frappe filters: status, modified, etc.
- Failed-since filter: failed_since: '2026-04-30 14:00:00'
- Error-contains filter: error_contains: 'HSN'
- Translated-error-key filter: translated_error_key: 'EE_HSN_NOT_FOUND'
- Affected-document filter: affected_doctype + affected_name patterns
- Webhook-gap filter: missing_in_window: ('2026-04-30 14:00', '2026-04-30 18:00') for webhook replay
- Compound filters: AND/OR composition
The filter editor includes a Preview Count button that runs the filter without committing — gives the FDE confidence in the scope before configuring strategy.

## 19.5 Commit phase

- Commit is gated on: Dry Run completed successfully + Dry Run sign-off recorded + commit_reason populated
- Commit creates a parent Queue Job that spawns child Queue Jobs per affected record
- Throttle and max_concurrency apply
- Commit can be Paused or Cancelled mid-execution; the system stops issuing new operations but does not roll back already-committed ones
- Per-record commit results captured in commit_results
- On Commit completion: a summary record posted (success count, failure count, total elapsed) and a notification sent to the FDE

## 19.6 Audit and traceability

- Every Replay Plan is a permanent record
- Every record affected carries back-references in its Sync Record: ecs_replay_plan, ecs_replay_strategy
- Replay Plans appear in the Configuration Audit log
- Reports: 'Replay activity last 30d' shows all plans with their outcomes

## 19.7 Constrained Replay (FDE permissions)

- Replay Plan creation: any FDE
- Dry Run execution: any FDE
- Commit: requires elevated permission (Replay Approver role) for plans affecting > 100 records or financial-impact > ₹100k
- Mark-Manually-Resolved strategy: always requires Replay Approver
- Configurable thresholds per Company

## 19.8 Common patterns the spec accommodates

- 'Replay all manifest webhooks for the last 4 hours' — Webhook Event filter on event_type and received_at
- 'Retry all failed Item pushes whose error contains HSN' — Sync Record filter on status + error_contains
- 'Force-resync the 12 SKUs that are drifting' — Sync Record filter on entity_type + drift_detected
- 'Reprocess all manifests since the field mapping fix at 14:32' — Webhook Event filter on event_type + received_at
- 'Mark-manually-resolved the 47 Items that exist on EE but we don't sell anymore' — Sync Record filter on entity_type + status

# 20. Schema Drift Detection and Mapping Coverage

EasyEcom can change their API at any time. They add fields, change enum values, rename properties, restructure nested objects. None of this announces itself. The integration silently mis-maps for days or weeks before someone notices the recon is off. This section specifies the machinery that catches such drift before it causes financial damage.

## 20.1 The schema-snapshot model

Every API response and every webhook payload is hashed by shape (not value). The hash is computed by:

1. Recursively walking the JSON structure
1. For each leaf, recording (path, type) — not the value
1. Sorting paths lexicographically for determinism
1. Hashing the sorted (path, type) list with SHA-256
Two payloads with the same shape (regardless of values) produce the same hash. Two payloads differing in any structural way (a new field, a missing field, a type change) produce different hashes.

## 20.2 EasyEcom Schema Snapshot DocType

| Field | Type | Notes |
| --- | --- | --- |
| snapshot_hash | Data (Indexed) | The shape hash; primary identifier |
| endpoint | Data | Which endpoint produced this shape (or webhook event type) |
| direction | Select | Outbound Response / Inbound Webhook / Inbound Pull |
| first_seen_at | Datetime | When this shape was first observed |
| last_seen_at | Datetime | Last observation |
| observation_count | Int | How many times we've seen this shape |
| paths_summary | JSON | List of (path, type) pairs that constitute the shape |
| is_known_good | Check | FDE-marked: this is an expected shape |
| fde_notes | Long Text | FDE annotations on what this shape represents |
| sample_payload_link | Link → EasyEcom Payload Sample | The redacted sample for this shape |

## 20.3 EasyEcom Payload Sample DocType

| Field | Type | Notes |
| --- | --- | --- |
| snapshot_hash | Data (Link → Schema Snapshot) |  |
| redacted_payload | Long Text | Sample with sensitive values masked (per Section 3.4 redaction rules) |
| captured_at | Datetime |  |
| api_call_link | Link → EasyEcom API Call | The specific call that produced this sample |

One Payload Sample retained per Schema Snapshot. When a schema is observed multiple times, only the first sample is kept. Storage cost is bounded — even with hundreds of distinct shapes, total storage is small.

## 20.4 Drift detection

Every API response and webhook payload triggers schema-hash computation:

- If hash matches an existing Schema Snapshot: increment observation_count, update last_seen_at, no further action
- If hash is new: create a Schema Snapshot record, capture a Payload Sample, fire a Schema Drift alert (severity Info if shape is similar to a known-good shape, Warning if it's substantially different)
- 'Substantially different' = Jaccard distance on path sets > 0.1 from the closest known-good shape
New schemas don't break the integration immediately — they just get processed by the existing Field Mapping rules, which may or may not handle them correctly. The alert is what gives the FDE a chance to inspect and adjust before silent mis-mapping accumulates.

## 20.5 Drift inspection UI

Schema Drift detail page shows:

- Side-by-side: paths in this new shape vs. paths in the closest known-good shape
- Highlighted differences: added paths in green, removed in red, type changes in yellow
- Sample payloads: the new and the closest known-good, side-by-side
- 'Mark Known Good' action: FDE blesses the shape; future occurrences won't alert
- 'Affects Field Mapping' action: opens the relevant Field Mapping ruleset for editing

## 20.6 Mapping coverage report

Periodic snapshot answering: 'of the fields actually present in real EasyEcom payloads, what % are explicitly mapped, what % match by identity, what % are dropped?'

### 20.6.1 Snapshot computation

Daily cron per Field Mapping ruleset (Mapping Coverage Snapshot DocType):

- Sample the last 1,000 payloads processed by this ruleset
- For each payload, record per-path: mapped_explicitly / mapped_identity / dropped
- Aggregate: % per category, list of most-common dropped fields, list of most-common identity-mapped fields

### 20.6.2 What this enables

- FDE quarterly review: are we explicitly mapping the right fields, or relying too heavily on identity defaults?
- Detection of 'fields we didn't know about' — if a payload has a field that's neither explicitly mapped nor identity-matched, it's dropped silently. The coverage report surfaces these
- Prevention of stealthy expansion of dropped data — a previously-dropped field that's now business-relevant gets noticed

## 20.7 Sample payload archive

- Beyond the per-Schema-Snapshot sample, an archive of payload samples is kept for diagnostic use
- Configurable retention — default 30 samples per endpoint per direction, rolling
- Always redacted using Section 3.4 rules
- Searchable by endpoint, direction, date
- Used in Replay dry-run to validate ruleset changes against representative real payloads

## 20.8 Type evolution detection

Beyond shape (paths and types), the system also tracks value-level patterns for selected fields:

- For string fields: distinct value count over time; new distinct values flagged
- For enum-like fields: alert if a value appears that isn't in any enum_map transformer in any active ruleset
- For numeric fields: range and distribution; outliers flagged for FDE review
Value-level monitoring is opt-in per field via a Schema Watch DocType — not enabled for every field by default to keep cost bounded.

# 21. SLA Budgets, Tracking, and In-Context Indicators

Internal SLAs for the integration's flows turn vague 'should be fast' commitments into measurable performance budgets. They're useful in three ways: (1) the operations team has objective targets, (2) breach detection is automated rather than seat-of-the-pants, (3) the operator surface can show users live progress against the SLA so they're not left wondering.

## 21.1 The SLA Budget DocType

| Field | Type | Notes |
| --- | --- | --- |
| budget_name | Data | Human-readable; e.g., 'B2C SI within 5 min of manifest' |
| company | Link → Company |  |
| flow | Select | Master Sync / Buying / B2B Sales / B2C Sales / Stock Transfer / Returns / Cancellations / Webhook Processing / Pull Cadence / Push Latency |
| specific_event | Select | Specific event within the flow; e.g., 'manifest webhook → SI created' |
| target_seconds | Int | The SLA target |
| target_percentile | Int | The percentile at which the target must be met (typical: 99 or 95) |
| measurement_window_minutes | Int | Rolling window for the percentile computation (typical: 1440 for 24h) |
| active | Check |  |
| alert_on_breach | Select | Critical / Error / Warning / None |

## 21.2 Default SLA budgets shipped with the parent app

| Flow | Event | Target (p99) |
| --- | --- | --- |
| Pull Cadence | Order pull cron staleness | 10 min |
| Pull Cadence | GRN pull cron staleness | 60 min |
| Webhook Processing | Manifest webhook → SI created | 5 min |
| Webhook Processing | GRN webhook → PR created | 10 min |
| Webhook Processing | Return webhook → SR created | 15 min |
| Push Latency | PO submit → EE PO confirmed (Async mode) | 60 sec |
| Push Latency | SO submit → EE SO confirmed (Async mode) | 60 sec |
| Push Latency | Item save → EE Item synced (Async mode) | 5 min |
| Master Sync | Bulk Item sync (per item) | 10 sec |
| B2C Sales | Manifest event → SI ready | 5 min |
| Returns | Return-receipt → SR ready | 15 min |

## 21.3 Tracking machinery

- Every flow event is timestamped at start and end
- End-to-end latency computed and stored on the relevant record (Sync Record, Webhook Event, Queue Job)
- Hourly cron computes per-budget rolling-window percentiles
- Breach detected when computed percentile exceeds target
- Breach record (EasyEcom SLA Breach DocType) created with: budget, breach percentile, breach value, contributing records, suspected root cause
- Alert fires per budget's alert_on_breach severity

## 21.4 SLA Breach DocType

| Field | Type | Notes |
| --- | --- | --- |
| budget | Link → SLA Budget |  |
| breach_window_start, breach_window_end | Datetime |  |
| actual_percentile_value | Decimal | What the percentile actually measured |
| target_percentile_value | Decimal | What it should have been |
| affected_records_count | Int |  |
| affected_records_sample | JSON | Sample of slowest records contributing to the breach |
| suspected_root_cause | Select | EE API slow / Webhook backlog / Queue backed up / Field Mapping error / Unknown |
| financial_impact_estimate | Currency | Computed from recon-engine integration (Section 23) |
| resolution_notes | Long Text |  |
| resolved_at, resolved_by | Audit |  |

## 21.5 In-context document indicators

When a user is looking at a document that's mid-integration (a Sales Order pushed Async, a Sales Invoice waiting on EE confirmation), they shouldn't have to dig to know the integration's progress. The document banner shows live SLA progress.

### 21.5.1 What the banner shows

- On a Sales Order in Push Pending state: 'Pushing to EasyEcom — expected within 60 seconds. (45 seconds elapsed)'
- On a Sales Invoice waiting for IRN: 'Generating e-invoice — expected within 30 seconds.'
- On a Purchase Receipt awaiting GRN ingestion: 'Awaiting EasyEcom GRN webhook. Last expected at 14:32 (3 min ago).'
- Banner updates live via `frappe.publish_realtime` (Frappe's standard websocket primitive). The Queue Job's controller emits a `realtime_event` on every state transition; the document banner JS subscribes to events scoped to the document's Frappe doc name, updates the banner reactively. No polling fallback needed — Frappe Cloud's realtime infrastructure is reliable; if the connection drops, the next page load resyncs.
- Banner colour: green when within budget, yellow when approaching, red when breached
- Click banner: opens the underlying Queue Job / Webhook Event with full diagnostics

### 21.5.2 Where banners appear

- Top of any business document with an active integration commitment (SO push pending, SI awaiting IRN, PO push pending, etc.)
- Sticky on scroll — visible regardless of which tab the user has open within the document
- Auto-dismisses when the integration commitment is resolved (with a brief success toast)

## 21.6 SLA dashboard

Dedicated SLA Compliance dashboard (linked from EasyEcom Workspace and Morning Brief):

- Per Company per flow: current week compliance %, last 4 weeks trend, breach count
- Heat map: SLA budget × time-of-day showing where breaches concentrate
- Top breaching budgets: ranked by frequency × financial impact
- Trend lines: are we getting better or worse

## 21.7 SLA reporting

- Weekly SLA Compliance Report (auto-emailed to digest recipients): per-budget compliance % vs. target, breach count, top breaching periods
- Monthly SLA Trend Report: 4-week comparison, suspected systemic issues
- Quarterly review prompt: revisit budget targets — are they still right for the client's growth?

## 21.8 SLA budget tuning during onboarding

- Default budgets ship as part of the parent app fixtures
- FDE reviews and tunes during onboarding based on client's expectations and observed baseline
- Tunings recorded in Configuration Audit
- Methodology team reviews tunings quarterly across the FDE fleet — outliers indicate either client-specific patterns worth standardising or FDE practices worth correcting

# 22. Cross-Company Aggregator Operations

Aggregator clients run multiple legal entities, each its own Frappe Company, often sharing operations team and FDE. Per-Company views are the right default; aggregator-wide views are critical for the FDE managing the whole group. This section specifies the cross-Company surface.

## 22.1 The aggregator pattern

Common shapes of aggregator clients:

- Brand house: 5-15 sub-brands, each its own legal entity for tax efficiency, sharing warehouses and ops teams
- Multi-marketplace seller: separate entity per marketplace category (electronics on one entity, apparel on another) for risk isolation
- Geographic aggregation: per-state entities for GST simplicity
- Acquired-company portfolio: PE-owned roll-ups where each acquisition stays a separate entity post-acquisition
In all cases the FDE manages multiple Companies as a unit. The integration recognises this without forcing it on single-Company clients.

## 22.2 When cross-Company views activate

- Cross-Company views activate automatically when the deployment resolves to more than one Frappe Company (see Section 14.8).
- When the deployment resolves to a single Company, these views stay hidden — the UI stays uncluttered for single-entity clients.
- Per-user preference: an FDE managing one Company within a multi-Company deployment can opt out of cross-Company views for their own session.

## 22.3 Cross-Company Workspace

A second Workspace, EasyEcom Cross-Company, accessible only when the deployment resolves to more than one Company. Layout:

### 22.3.1 Top row

- Per-Company status grid: each managed Company as a tile with status dot, last-sync, open-discrepancy count
- Aggregate kill-switch: pause syncs across all managed Companies (separate from per-Company kill-switches)
- Aggregate connection status: green only if every Company is green

### 22.3.2 Number Cards

- Total open Discrepancies across all Companies
- Worst-performing Company today (lowest API success rate)
- Aggregate API call volume last 1h
- Aggregate webhook receipt rate
- Aggregate Queue Job depth
- SLA breaches across all Companies last 24h

### 22.3.3 Charts

- API success rate per Company over last 7d (small multiples — one tile per Company)
- Discrepancy financial impact rollup, stacked per Company over last 30d
- Cross-Company variance: which Companies are diverging from the group baseline

### 22.3.4 Cross-Company anomaly view

Specifically for finding the outlier:

- Per-flow per-Company table: pick a flow, see compliance % per Company side-by-side
- Sort by deviation from group median
- Drill down to the worst Company's recent failures
This is the FDE's '11pm Saturday' view — when something feels off across the group, this shows which Company to focus on first.

## 22.4 Cross-Company Reports

- Cross-Company Health Rollup — every metric from per-Company Daily Sync Health, side-by-side
- Aggregator Discrepancy Rollup — open Discrepancies across all Companies, with financial impact roll-up
- Aggregator SLA Compliance — per flow per Company per week, with group rollup
- Aggregator Configuration Drift — Field Mapping rulesets and Settings differing across Companies (when they should usually match)
- Aggregator Master Sync Health — Item / Customer / Supplier sync state across Companies

## 22.5 Configuration drift detection

In an aggregator, Companies usually share most configuration with deliberate per-Company variance only where business reason exists. The integration detects unexpected configuration drift:

- Periodic comparison of Field Mapping rulesets across Companies
- Periodic comparison of EasyEcom Account and per-Company settings (excluding credentials and per-Company-by-design fields)
- Periodic comparison of Source-of-Truth Map patterns
- Drift detected: Integration Discrepancy raised with severity Info; FDE reviews
- FDE can mark detected drift as 'expected' (recorded as decision in Configuration Audit) to suppress future alerts on the same drift

## 22.6 FDE workload distribution

Operationally, an FDE managing 5 Companies is heavier than 5 separate single-Company FDEs because of context-switching cost. The integration captures this:

- FDE workload metric: per FDE, count of Companies managed × open-discrepancy-weight
- Workload imbalance alerts when an FDE's workload exceeds 1.5× the team median
- Per-Company assigned-FDE field for clear ownership
- Discrepancy auto-routing based on assigned FDE per Company

## 22.7 Master sharing across Companies

Distinct from sync direction (already in Section 14), this is about presentation:

- Item / Customer / Supplier are shared records across Companies in ERPNext
- The integration shows ecs_easyecom_mappings (the per-Company per-location mapping rows) as a clear table on the Item / Customer / Supplier detail page
- Aggregator FDEs can see all Company mappings for a master at once
- Single-Company FDEs see only their Company's mappings (filtered automatically)
- Action: 'Force Resync to All Companies' on Item / Customer / Supplier detail (System Manager only)

# 23. Recon-Aware Integration Alerts

This section is the product wedge. Every WMS integration today says 'this thing failed.' Ours says 'this thing failed, and here is what it cost you.' Attaching financial impact to integration alerts changes them from a noise channel for engineers to a signal channel for accountants and operators. It also makes the alerts themselves the most-watched surface in the product.

## 23.1 Why financial impact at the alert level matters

- Triage by impact, not by chronology — a missed manifest worth ₹4L matters more than a Item sync failure worth zero
- Conversation with finance — when an alert says ₹4L exposure, the finance team cares; when it says 'sync_record_47 failed', they don't
- Demonstrable ROI — every recovered alert has a measurable saving; cumulative alert value over time becomes the product's proven value to the client
- Methodology embodiment — the methodology team's view of what each kind of failure costs is encoded in the impact computation, not buried in a checklist

## 23.2 The financial impact attachment model

Every Integration Alert and Integration Discrepancy carries:

| Field | Type | Notes |
| --- | --- | --- |
| financial_impact_estimate | Currency (INR) | The current best estimate of monetary exposure |
| financial_impact_basis | Select | Direct (the actual amount at risk) / Indirect (downstream consequence) / Cumulative (multiple events aggregated) |
| financial_impact_computation | Long Text | How the estimate was derived; readable by finance |
| financial_impact_confidence | Select | Estimated / Likely / Confirmed |
| financial_impact_recovered | Currency | Updated when the issue is resolved; how much of the exposure was actually realised vs. recovered |

## 23.3 Per-flow impact computation rules

| Failure pattern | Impact computation |
| --- | --- |
| Manifest webhook missed | Sum of EasyEcom order amounts for the affected orders during the missed window. Recovered when SI is created retroactively |
| B2C SI creation failed | Order amount for the affected SI. Recovered when SI is successfully created |
| GRN ingestion failed (PR not created) | Goods value at the EE GRN for the affected GRN. Recovered when PR is created |
| Marketplace settlement file Refund event with no return-receipt (Return-Marked-Goods-Not-Received) | Refund amount. Recovered = 0 if not actively claimed; represents the methodology's standard 'lost inventory' value |
| Inventory drift between systems | Drift quantity × Item valuation rate |
| Master sync failure (HSN missing on Item) | Cumulative GMV of orders blocked from SI creation |
| Stock Reservation failure on B2B order | Order amount at risk of stock-out |
| Schema drift on a critical field (e.g., new tax_amount field unmapped) | Cumulative tax exposure since drift first observed |
| SLA breach | Order amount of breached transactions × estimated downstream cost |

## 23.4 The Impact Computation engine

Each failure pattern has a Python function (in a registered impact_calculators module) that computes the financial estimate. The functions:

- Take the failing record (Sync Record / Discrepancy / SLA Breach) as input
- Have access to the recon engine's data (Sales Invoices, Purchase Receipts, Settlement Forecasts) for context
- Return: estimate (currency), basis (string), confidence (enum), human-readable computation
- Are versioned and audited — methodology team owns the calculator definitions
- Run in a sandboxed context (no arbitrary I/O, no permission-bypassing queries)

## 23.5 Impact recovery tracking

- When an alert / discrepancy is resolved, the financial_impact_recovered field captures the actual recovered amount
- Recovered may equal the estimate (full recovery) or less (partial recovery — e.g., a settlement claim partially won) or zero (failed recovery)
- The delta is the realised loss — feeds the methodology's 'leakage by category' metric
- Quarterly aggregate: 'integration prevented ₹X recovery vs. ₹Y realised loss' — the product's cumulative impact metric

## 23.6 Alert routing by impact

- Low-impact (< ₹10k): routes per the standard severity rules in Section 18
- Medium-impact (₹10k - ₹100k): elevates one severity level (Warning becomes Error, Error becomes Critical)
- High-impact (> ₹100k): always Critical, regardless of underlying error type
- Very-high-impact (> ₹1L): Critical + automatic escalation to client's finance leadership (configurable per Marketplace Account)
- Per-Company impact thresholds configurable; defaults are reasonable for SME clients

## 23.7 Alert presentation with financial context

Standard alert template adds financial framing:

```
Alert: GRN Ingestion Failed (Critical — ₹4,20,000 impact)

Company: <company>
Affected Purchase Receipt: <pr_link or 'not yet created'>
Source EasyEcom GRN: <grn_id>
Goods value at risk: ₹4,20,000
Suspected ITC at risk: ₹75,600

Translated error: HSN code 8517.62 not present in Tax Master.
Suspected cause: Item created in EasyEcom without HSN; came in via GRN webhook.

Suggested actions:
1. Add HSN 8517.62 to ERPNext Tax Master
2. Update Item LXLB201 with the HSN
3. Replay this GRN ingestion (preview Replay Plan link)

Direct links:
- View Sync Record: <link>
- View API Call (last attempt): <link>
- View Event Timeline: <link>
- Create Replay Plan (1 GRN, est. 30 sec): <link>
- View Impact Computation: <link>
- Acknowledge: <link>

Fired at: <ts>
Will re-alert at: <ts + 1h> if not acknowledged (Critical-tier)
```

## 23.8 The 'recovered value' dashboard

A specific Workspace surface for the client's leadership: cumulative recovered value attributable to the integration. Visible in the Morning Brief and as a standalone tile.

- Cumulative since deployment: 'Integration has surfaced ₹X exposure, ₹Y recovered, ₹Z realised loss'
- Trended monthly
- Top recovery categories (which kinds of failures contributed most)
- Current open exposure (sum of estimated impact on unresolved alerts)
This is the most important dashboard in the product. Client renewals and expansion are won here.

## 23.9 Limits and caveats

- Estimates are estimates — the calculators err on the side of slight over-estimation to ensure the FDE pays attention
- Confidence labels are honest — 'Estimated' means we're inferring; 'Likely' means we have direct data; 'Confirmed' means a settlement event has confirmed the loss
- Recovered value tracking depends on FDE diligence in updating financial_impact_recovered when issues resolve — methodology mandates this update as part of discrepancy resolution
- Cumulative impact metrics should not be used for client billing or commission — they're directional, not audit-grade
- Methodology team reviews calculator outputs quarterly against actual settlement data to validate accuracy

# 24. Morning Brief

The Morning Brief is the single screen the FDE opens at 9am. It answers, in 30 seconds: what broke overnight worth caring about, what's trending wrong, what's been ignored too long, and what's the health vs. last week. It is the product's most-watched surface and the demo-day artifact.

## 24.1 Why this is its own surface

- Lists are forensic, not directive — they show what happened, not what to do
- Dashboards are diagnostic, not directive — they help analyse, not act
- The FDE's morning question is 'what should I work on today?' — neither lists nor dashboards answer this directly
- A Morning Brief that ranks issues by impact and surfaces the 3-5 things worth time today turns the integration from a passive system into an actively helpful coworker
- This view is the core demo: 'open this on Monday morning, this is your day's plan'

## 24.2 The EasyEcom Morning Brief Snapshot DocType

Materialised view computed nightly at 06:00 IST per Company, accessible via /app/easyecom-morning-brief or via Workspace tile.

| Field | Type | Notes |
| --- | --- | --- |
| company | Link → Company |  |
| snapshot_date | Date |  |
| computed_at | Datetime |  |
| overnight_critical_alerts | JSON | List of critical alerts since previous brief |
| financial_exposure_total | Currency | Sum of open exposure |
| top_3_actionable | JSON | The headline items for today (see 26.3) |
| anomalies | JSON | Trending issues not yet at alert threshold |
| chronic_neglect | JSON | Items open > 3 days |
| health_vs_last_week | JSON | Per-flow comparison |
| recovered_value_yesterday | Currency |  |
| sla_breaches_yesterday | Int |  |
| new_schema_drift_yesterday | Int |  |

## 24.3 The Top 3 Actionable section

The headline. Maximum three items, ranked by financial impact × time-decay × resolvability:

- Each item: title, severity, financial impact, age, suggested next action with one-click route
- Selection algorithm: ranks all open Critical and Error alerts plus all open Discrepancies by impact_score = financial_impact * age_decay * (1 - estimated_difficulty)
- age_decay: 1.0 for items < 6h old, 0.8 for 6-24h, 0.6 for > 24h (acknowledging that older items may be in someone's queue already)
- estimated_difficulty: 0 for items with one-click resolution path, 0.3 for replay-with-override, 0.6 for custom investigation
- Ties broken by: financial impact, then by age
- If fewer than 3 actionable items meet a min_score threshold, fewer items are shown — never padded
Example:

```
TODAY'S TOP 3
─────────────

1. Manifest webhooks missed for Amazon FBA, last 4 hours
   ₹8,40,000 exposure across 47 orders
   Replay Plan ready to commit (dry-run done at 06:02)
   → Review Replay Plan

2. GRN ingestion blocked: 12 PRs awaiting HSN configuration
   ₹4,20,000 procurement value blocked
   3 unique HSN codes need addition (8517.62, 9405.10, 8504.40)
   → Add HSN codes

3. Schema drift detected on /returns/getReturnsV3 endpoint
   New field 'partial_return_pct' observed since 2026-04-29
   No financial impact yet but blocking partial-return recon
   → Review schema drift, update Field Mapping
```

## 24.4 The Anomalies section

Trending issues not yet at alert threshold but worth knowing:

- Volume anomalies: 'B2C order volume yesterday was 67% of 7-day average — confirm Amazon listing health'
- Latency anomalies: 'API p95 latency on /orders/V2/getAllOrders trending up; 850ms today vs 320ms baseline'
- Pattern anomalies: 'Sync Records for Customer entity have a 12% retry rate today vs 2% baseline'
- New error patterns: 'New translated error key EE_ORDER_DUPLICATE_REJECT seen 8 times today; not in error library'
- Inventory drift trends: 'Drift between EE and ERPNext stock has grown for 3 consecutive days for 14 SKUs'

## 24.5 The Chronic Neglect section

Items open beyond their reasonable resolution window:

- Discrepancies open > 3 days: count + sum of impact
- Sync Records failed > 7 days with no resolution: count
- Replay Plans drafted but never committed > 5 days old: count
- Schema drifts unblessed > 30 days: count
Each item linked. The point is to surface the easily-ignored — the things that don't fire alerts but slowly erode integration health.

## 24.6 The Health vs. Last Week section

One-line comparisons per major flow:

- Master sync success rate: 99.4% this week vs 98.9% last week (↑)
- B2C SI creation success rate: 99.1% this week vs 99.7% last week (↓)
- GRN ingestion success rate: 97.8% this week vs 96.2% last week (↑)
- Webhook receipt rate: 98.2% this week vs 99.1% last week (↓ — investigate)
- Average API success rate: 99.3% vs 99.5% (≈)
Arrows are directional indicators (↑ better, ↓ worse, ≈ similar). Significant deteriorations (>1% drop) get an Investigate badge.

## 24.7 The Recovered Value yesterday section

One line: 'Yesterday: ₹X exposure surfaced, ₹Y recovered (Z resolved alerts).' Reinforces the product's value daily.

## 24.8 Distribution

- Email delivery to digest_recipients at 09:00 IST per Company
- Slack message to alert channel at the same time
- In-desk view always available; FDE can refresh on demand
- Aggregator-mode includes a multi-Company brief (one section per Company, plus a group summary)

## 24.9 Generation

- Cron at 06:00 IST per Company; the Morning Brief is computed as a snapshot for that day
- Delivery (email, Slack) cron at 09:00, reads the snapshot generated 3 hours earlier
- Snapshot persisted; an FDE can browse historical snapshots for retrospectives
- Recomputation can be triggered on-demand by an FDE if needed

## 24.10 Why 09:00 and not real-time

Deliberate. A real-time 'most important thing right now' view would be alarmist and unhelpful — operators want a structured start to the day, not a moving target. The Morning Brief is the day's plan, not the day's emergency room. The emergency room is the alert system (Section 18).

# 25. Error Translation Library

Raw EasyEcom errors are useless to operators: HTTP 500 with body {error: 'Validation failed'} tells the FDE nothing actionable. The Error Translation Library is the data structure that maps such cryptic responses to plain-English explanations and concrete next steps. It's small in code, large in operator value, and grows with every incident.

## 25.1 The EasyEcom Error Translation DocType

| Field | Type | Notes |
| --- | --- | --- |
| error_key | Data (Indexed, Unique) | Stable identifier; e.g., EE_HSN_NOT_FOUND |
| matcher_type | Select | Regex / Substring / JSON Path / Compound |
| matcher_pattern | Long Text | The match expression |
| matcher_priority | Int | When multiple translations match, higher priority wins |
| title | Data | One-line plain-English title |
| explanation | Long Text | Plain-English explanation of what this means |
| suspected_causes | Table of (cause, likelihood) | Ranked list of probable root causes |
| suggested_actions | Table of (action_text, action_link_template) | Concrete next steps with parameterised links |
| impact_calculator | Link → Impact Calculator | Reference to a Section 23 calculator |
| severity_override | Select | If this error always indicates a particular severity, override the default |
| confidence | Select | Confirmed / Likely / Speculative — how sure we are this translation is right |
| created_by, last_modified_by, last_modified_at | Audit |  |
| match_count | Int (auto) | How many times this translation has matched |
| last_matched_at | Datetime |  |

## 25.2 Matcher types

### 25.2.1 Substring

Simplest: matches if the substring appears anywhere in the error response body. Useful for matching specific EE error message text:

```
error_key: EE_HSN_NOT_FOUND
matcher_type: Substring
matcher_pattern: HSN code not present in Tax Master
title: HSN code missing
explanation: EasyEcom rejected the operation because the HSN code on this Item is not registered in EasyEcom's tax master. EE expects every item with tax to have a known HSN.
suspected_causes:
  - Item created in EasyEcom without HSN, came back via pull
  - HSN configured on ERPNext side but not pushed to EE
suggested_actions:
  - Verify HSN on the Item is correct in ERPNext
  - Force resync this Item to EE
  - Add the HSN to EasyEcom Tax Master if it's a new HSN code
```

### 25.2.2 Regex

More flexible: matches via regex with capture groups that parameterise the explanation:

```
error_key: EE_RATE_LIMITED
matcher_type: Regex
matcher_pattern: ^Rate limit exceeded.*retry after (\d+) seconds$
title: EasyEcom rate-limited us; retry in {1} seconds
explanation: EasyEcom is rejecting calls due to rate limiting. They've asked us to wait {1} seconds before retrying.
suspected_causes:
  - Sync Tuning has max_throughput_per_sec set too aggressively
  - A bulk sync is running concurrently with normal traffic
suggested_actions:
  - Wait {1} seconds; the system will auto-retry
  - If sustained, reduce max_throughput_per_sec in Sync Tuning
  - Check if a Replay Plan or bulk sync is running
```

### 25.2.3 JSON Path

Matches against specific fields in a structured response:

```
error_key: EE_DUPLICATE_ORDER
matcher_type: JSON Path
matcher_pattern: $.error.code = 'DUPLICATE_ORDER_REF'
title: EasyEcom rejected an order push as duplicate
explanation: We tried to push this Sales Order to EasyEcom but EE has already seen this reference. Usually means a previous push succeeded but our recording of the success failed.
suggested_causes:
  - Network failure on previous push attempt — EE got it but we didn't get the response
  - Idempotency-key collision (rare)
suggested_actions:
  - Use Inspector to check whether the SO exists on EE side
  - If exists: Mark this Sync Record as Already Synced with the EE order ID
  - If not exists: Investigate further; this should not happen
```

### 25.2.4 Compound

Combines multiple matchers (AND/OR) for nuanced cases.

## 25.3 Initial library

Shipped as fixtures with the parent app. The methodology team curates and the FDE fleet contributes via a sanctioned PR process. Initial coverage targets the most common EE error patterns:

- EE_HSN_NOT_FOUND, EE_HSN_INVALID, EE_HSN_RATE_MISMATCH
- EE_ITEM_NOT_FOUND, EE_ITEM_INACTIVE, EE_ITEM_DUPLICATE_SKU
- EE_VENDOR_NOT_FOUND, EE_VENDOR_GSTIN_INVALID
- EE_CUSTOMER_NOT_FOUND, EE_CUSTOMER_GSTIN_INVALID
- EE_LOCATION_INACTIVE, EE_LOCATION_NOT_FOUND
- EE_RATE_LIMITED, EE_AUTH_EXPIRED, EE_AUTH_INVALID
- EE_DUPLICATE_ORDER, EE_DUPLICATE_GRN, EE_DUPLICATE_PO
- EE_INSUFFICIENT_INVENTORY, EE_LOCATION_MISMATCH
- EE_BATCH_REQUIRED, EE_SERIAL_REQUIRED, EE_EXPIRY_REQUIRED
- EE_TAX_MISMATCH, EE_TAX_CATEGORY_NOT_MAPPED
- EE_PAYLOAD_VALIDATION_FAILED (catch-all for malformed payloads)
- EE_INTERNAL_SERVER_ERROR (5xx catch-all)
- EE_TIMEOUT (network timeout catch-all)
Target: 50+ entries at v0.1, growing to 200+ by v1.0 as the FDE fleet contributes.

## 25.4 Translation execution

- Every Failed Sync Record / Failed Queue Job / Failed Webhook Event triggers translation lookup
- Matchers evaluated in priority order; first match wins
- Matched translation populates: translated_title, translated_explanation, translated_actions, suspected_root_cause
- Match recorded — match_count and last_matched_at on the Translation entry are incremented
- Unmatched errors get translation_status = Untranslated and are queued for FDE labelling (see 27.5)

## 25.5 Auto-clustering of untranslated errors

- Periodic cron clusters Untranslated errors by similarity (TF-IDF + cosine similarity on the error text)
- Clusters surfaced in the Translation Library admin view
- FDE can: review a cluster, create a Translation entry from a representative example, the entry then auto-classifies the rest of the cluster
- Clusters with >10 instances and no Translation entry trigger an Info alert to the FDE

## 25.6 Translation analytics

Built into the Library admin view:

- Top translations by match count (which errors fire most)
- Translations matched 0 times in last 30 days (candidates for retirement)
- Untranslated cluster count and size
- Translation coverage % (matched / total failed events)
- Time-to-resolve by translation_key (which translated errors are hard to fix)
Coverage % is itself a methodology KPI — methodology team targets > 90% coverage on production-deployed clients.

## 25.7 Per-Company customisation

- Translation entries are global by default (the standard library)
- FDE can mark entries as Company-specific overrides
- Company-specific overrides take priority over global entries with the same matcher
- Useful when a client has unusual EE configuration that produces non-standard errors

## 25.8 Quality control

- Translations have a confidence field (Confirmed / Likely / Speculative)
- Speculative translations are flagged in the alert template ('our best guess — verify')
- Methodology team reviews new translations from FDE contributions before promoting from Speculative to Likely or Confirmed
- Translation accuracy reviewed quarterly: are the suggested actions actually working? Do they get checked-off as resolutions?

# 26. Time Travel — Point-in-Time State and Configuration Audit

Most operational questions, when something is wrong now, take the form 'when did it start.' Without time-travel capability, the answer requires log-scrolling, guesswork, and luck. The integration ships with first-class point-in-time queries — the FDE can ask 'what did we know at 14:32 yesterday' and get an answer.

**Relationship to Frappe's built-in `Version` DocType:** Frappe automatically tracks document changes via the `Version` DocType when `track_changes: 1` is set on a DocType's JSON. We rely on `Version` for the underlying change capture mechanism — every audited DocType in the integration sets `track_changes: 1`. The EasyEcom Configuration Audit DocType is a *complementary* layer that adds what `Version` does not provide: structured `actor_role` (captured at action time, in case role changes later), `change_reason` (a mandatory free-text justification), `originating_request` (Frappe request ID for cross-system tracing), and aggregation across DocTypes for causal-chain queries. Configuration Audit rows are written by an `on_update` hook in the integration's controller layer; they reference the same change Frappe `Version` is also recording, but with the structured shape needed for time-travel queries. This is a deliberate complement, not a reinvention — both layers exist, with different purposes.

## 26.1 What is captured for time travel

- Every EasyEcom Account and per-Company settings change (per Section 3) — full before/after snapshot
- Every Field Mapping ruleset save — entire ruleset versioned
- Every Source-of-Truth Map row change
- Every Marketplace Account / Marketplace (channel) / Tax Mapping configuration change
- Every cache clear (which cache, who, when, why)
- Every alert configuration change (recipients, thresholds, channel routing)
- Every Replay Plan execution (the plan itself plus the dry-run results plus the commit results)
- Every manual override action (Mark as Already Synced, Force Resync, etc.)
Operational data (Sync Records, API Calls, Webhook Events) is already append-only or modified-only-by-system, so it's effectively time-travelable by date filter on existing fields.

## 26.2 The EasyEcom Configuration Audit DocType

Append-only log of every configuration change.

| Field | Type | Notes |
| --- | --- | --- |
| audit_id | Auto |  |
| timestamp | Datetime (Indexed) |  |
| actor_user | Link → User | Who made the change |
| actor_role | Data | Captured at action time (in case role changes later) |
| company | Link → Company | If Company-scoped |
| target_doctype | Data | What was changed |
| target_name | Data | Specific record name |
| change_type | Select | Create / Update / Delete / Action / Cache Clear / Override |
| before_state | Long Text (JSON) | Full record before change (or null for Create) |
| after_state | Long Text (JSON) | Full record after change (or null for Delete) |
| diff_summary | Long Text | Human-readable diff for fast review |
| change_reason | Long Text | Required for actions outside auto-system changes |
| originating_request | Data | Frappe request ID for trace correlation |

## 26.3 Field Mapping Version DocType

- Created automatically on every Field Mapping save
- Stores the entire ruleset state at that version
- Indefinite retention; bounded payload
- Browseable from the Field Mapping detail's Change History tab
- Diff against any prior version via the Diff Against Version action
- Rollback to any prior version (creates a new version)

## 26.4 Point-in-time queries

API: easyecom.api.point_in_time(target_doctype, target_name, at_timestamp). Returns the configured state of the target as it existed at that timestamp.

### 26.4.1 What works

- EasyEcom Account and per-Company settings — full state at any past time
- EasyEcom Field Mapping — full ruleset at any past time (via Field Mapping Version snapshots)
- Source-of-Truth Map — per-row historical state
- Marketplace Account / Channel / Tax Mapping — historical state
- Sync Cursor positions — what had we pulled by what timestamp
- Cache state — what was in cache at past time (for clearings, with cache_state_at)

### 26.4.2 Limits

- ERPNext core records (Sales Invoice, Purchase Receipt) are NOT time-traveled by this integration — Frappe's native version history applies, separate scope
- Granularity is to the second; sub-second precision not preserved
- Performance: point-in-time is O(log n) on indexed timestamp; should be fast for any reasonable history depth

## 26.5 Time-travel UI affordances

### 26.5.1 Configuration Audit list view

- Default view: chronological, last 7 days
- Filters: actor, company, target_doctype, target_name, change_type, time range
- Search: free text on diff_summary, change_reason
- Group by: actor (per-user activity log) / target_doctype (per-system change log) / company

### 26.5.2 Time-travel inspector

- Form: target DocType, target name, datetime
- Output: rendered DocType form showing the state at the requested time
- 'Compare to current' button: shows diff between past state and current state
- 'Show all changes since' button: lists every Configuration Audit row touching this record since the requested time

### 26.5.3 Diff viewer

- Side-by-side or unified-diff JSON view
- Field-level highlighting: added (green), removed (red), modified (yellow)
- Collapses unchanged sections
- Copy buttons for each side

## 26.6 Causal-chain queries

Beyond simple point-in-time, an FDE often asks: 'what configuration change caused this Sync Record to start failing?' The system supports causal-chain investigation:

- Given a failing Sync Record + the timestamp of failure onset
- Surface every Configuration Audit row in the previous 24h that touched any DocType referenced by the Sync Record's flow
- Ranked by relevance (touches the same Field Mapping ruleset > touches the Settings → touches unrelated config)
- Direct link from any Sync Record's Failed-since timestamp to this query

## 26.7 Audit retention and compliance

- Configuration Audit retention: indefinite by default; configurable lower bound 7 years for compliance-driven clients
- Field Mapping Version retention: indefinite (bounded payload)
- Audit records cannot be deleted by any user — even System Manager
- Audit records cannot be edited
- Audit records are queryable for compliance reporting

## 26.8 What time travel does NOT solve

- Doesn't replay history — you can see what was configured but not how data flowed
- Doesn't time-travel the EasyEcom side — we can see what we knew, not what they knew
- Doesn't time-travel ERPNext core records — Frappe's version history is the tool for that
- Doesn't preserve performance metrics over years — separate retention policy applies to API Call records (default 90 days, configurable)

# Part VI — Delivery and Meta

*What the integration enables downstream, how it is tested, how it is phased, and what remains open.*

# 27. What This Enables for the Recon Engine

The integration's purpose is not to be a generic ERPNext-EasyEcom connector. It is purpose-built to feed the recon engine clean, structured, correlatable data. This section maps each integration flow to the recon engine capability it enables.

## 27.1 Flow-to-recon mapping

| Integration flow | Recon capability enabled |
| --- | --- |
| Master sync — Item | Per-SKU margin breakdown (PRD Section 10.5); item-level variance attribution |
| Master sync — Customer (pseudo-customers) | Marketplace-scoped Sales Invoices that join cleanly to Settlement Lines |
| Master sync — Tax Category mapping | GST ITC reconciliation against GSTR-2B (PRD Section 9.2.5) |
| Buying flow — Purchase Receipt with correct tax lines | Fee-to-Expense reconciliation Purchase Invoices have correct ITC split (PRD Section 9.2.4) |
| Stock Transfer flows | Inventory variance reconciliation — per-Company per-warehouse stock matches EE |
| B2B Sales flow | Order-to-Settlement reconciliation for B2B orders (rare on marketplace settlement files but possible for B2B-marketplace channels) |
| B2C Sales flow with ecs_marketplace_order_id populated | Order-to-Settlement reconciliation join key (PRD Section 9.2.1) — without this, recon cannot run at all |
| B2C Sales — Marketplace Order Map record | Bridge between Settlement Line and Sales Invoice — the central join target |
| Returns / Cancellations with ecs_easyecom_event_id | Return-to-Credit-Note reconciliation (PRD Section 9.2.3) |
| Settlement Forecast tied to SI at creation | Variance computation per Settlement Line |
| Webhook + polling reliability | Recon runs on data that's actually present and current |

## 27.2 The contract this section defines

From the recon engine's perspective, the integration commits to:

- Every B2C marketplace order will produce a Sales Invoice with ecs_marketplace_order_id, ecs_marketplace (flat channel), ecs_marketplace_account populated
- Every Sales Invoice will have a Marketplace Order Map record at creation time
- Every Settlement Forecast will be created concurrently with the SI
- Every Purchase Receipt from EE GRN will have correct tax lines, ITC eligibility, and HSN — usable directly for fee Purchase Invoice posting
- Every cancellation/return will produce a Credit Note or Sales Return with ecs_original_sales_invoice and ecs_easyecom_event_id
- Every Stock Movement (transfer, adjustment) will carry ecs_easyecom_source_event for traceability
- Webhook + polling reliability ensures these artefacts exist within tolerable lag of the EE event

## 27.3 What breaks if the integration breaks

If integration reliability drops below the v0.1 commitments:

- Sales Invoices missing for orders → Order-to-Settlement recon flags entire orders as Unmatched. Cumulative across many missing SIs, recon is meaningless
- Purchase Receipts with wrong tax lines → Fee Purchase Invoice ITC postings are wrong → ITC vs GSTR-2B reconciliation always fails
- Stock movements missing → Inventory variance recon falsely reports drift
- Cancellation/return artefacts missing → Settlement Refund events flag as Unmatched → claim queue swells with false-positive Discrepancies
- In short: integration brokenness destroys recon engine usefulness. The integration is not optional plumbing; it is the primary product surface

# 28. Test Strategy

This section specifies the testing approach for v0.1: what to test, where to test it, what success looks like, and how engineering should structure tests in the codebase. Tests are first-class deliverables alongside production code; the FDE pre-flight checklist (Section 28.7) is the operational sign-off gate, but engineering tests are the developmental quality gate that precedes it.

## 28.1 The test pyramid

The integration uses a four-tier test pyramid, each tier with distinct goals, tooling, and CI gating:

| Tier | Purpose | Tooling | CI gate |
| --- | --- | --- | --- |
| Unit tests | Pure-function logic: Field Mapping rule execution, hash computation, cursor arithmetic, error pattern matching. No I/O, no Frappe context | pytest with custom fixtures; runs in <30s for whole suite | Required to pass; blocks merge |
| Integration tests (Frappe) | Frappe-context: DocType save hooks, permission rules, fixture loading, Field Mapping ruleset compilation. Uses test Frappe site, no external API | Frappe's built-in `bench run-tests`; runs in <5 min | Required to pass; blocks merge |
| Contract tests (mocked EE) | Outbound API call shapes: 'when we push an Item, the request URL/headers/body match expectation'. Uses requests-mock or VCR.py to record/replay EE responses | pytest-vcr with cassettes per endpoint; runs in <2 min | Required to pass; blocks merge |
| End-to-end tests (sandbox EE) | Full-flow: order placed in EE sandbox → manifest webhook → SI created → settlement file uploaded → recon Discrepancy created. Real EE sandbox, isolated test data | pytest with real-network markers; runs in 30-60 min | Run nightly + on release branch; not required per-PR |

## 28.2 EE sandbox account requirement

- EasyEcom must provide a sandbox account for pilot and ongoing development
- Sandbox provides full API surface but with isolated data (not affecting production)
- Sandbox credentials separate from production; never mixed
- If sandbox not available: limited to mock-server testing; flag this as material risk

## 28.3 Per-flow test cases

Estimated 120-150 test cases across the 10 flows plus operational surface. Indicative breakdown:

| Flow | Unit | Integration | Contract | E2E |
| --- | --- | --- | --- | --- |
| Auth and connection (Section 3) | 4 | 2 | 2 | 1 |
| Master sync — Item (Section 8.1) | 8 | 4 | 4 | 2 |
| Master sync — Customer/Supplier/Warehouse/Tax/Channel (Sections 8.2-4.7) | 10 | 6 | 6 | 3 |
| Buying / Inwarding (Section 9) | 10 | 8 | 4 | 4 |
| Stock Transfers (Section 10) | 6 | 4 | 2 | 2 |
| B2B Sales (Section 11) | 8 | 6 | 4 | 3 |
| B2C Sales (Section 12) | 6 | 5 | 3 | 3 |
| Returns and Cancellations (Section 13) | 8 | 6 | 3 | 3 |
| Multi-company (Section 14) | 4 | 4 | 0 | 2 |
| Field Mapping engine (Section 5) | 12 | 4 | 0 | 0 |
| Replay tooling (Section 19) | 6 | 4 | 0 | 1 |
| Schema drift (Section 20) | 6 | 2 | 2 | 0 |
| SLA tracking (Section 21) | 4 | 2 | 0 | 0 |
| Recon-aware alerts (Section 23) | 4 | 4 | 0 | 1 |
| Morning Brief (Section 24) | 2 | 4 | 0 | 1 |
| Error Translation (Section 25) | 8 | 2 | 0 | 0 |
| Time Travel (Section 26) | 4 | 4 | 0 | 0 |

## 28.4 Test categories

- Happy path — normal flow, all preconditions met
- Precondition failures — Item not synced, Vendor missing, etc.
- Idempotency — same event processed twice, replay safety
- Out-of-order — downstream event before upstream
- Webhook-vs-poll convergence — ensure both paths produce same result
- Multi-company isolation — Company A user blocked from Company B data
- Master conflict resolution — bidirectional conflicts resolved per ownership matrix
- Failure injection — random API errors, retries within tolerance
- Replay — Reverse-and-Replay produces clean state
- Field Mapping rule failure — rule raises with correct exception class and rule ID
- Schema drift detection — new shape triggers Drift alert with correct similarity scoring
- SLA breach detection — slow flow triggers correct breach record
- Permission boundaries — each role's read/write scope is correctly enforced

## 28.5 Test fixtures and factories

Engineering ships test fixtures alongside production fixtures. Convention:

- `tests/fixtures/` mirrors `fixtures/` structure — one fixture file per DocType seeded with minimal valid records
- `tests/factories/` provides Python factories: `make_item(...)`, `make_purchase_order(...)`, `make_easyecom_settings(company=...)` — used in test setUp
- `tests/cassettes/` stores VCR.py cassettes per (test_module, test_function) — records of EE API exchanges
- `tests/sample_payloads/` stores representative redacted EE payloads — used for Field Mapping and schema drift tests

## 28.6 Multi-company test fixtures

- Test site with 3 Companies: standalone, aggregator-of-3, single-with-multiple-locations
- Each Company with its own EE sandbox account (or scoped sandbox)
- Cross-Company isolation tests run on every regression cycle
- Aggregator-specific tests for the 5-Companies-in-one-site case

## 28.7 Pre-go-live FDE pre-flight checklist

Operational sign-off, distinct from engineering test sign-off. Performed by the FDE before any client cuts production traffic.

- EasyEcom Account configured with valid credentials
- All EasyEcom Locations created and mapped to Frappe Warehouses
- Source-of-Truth Map populated for every relevant Warehouse
- Marketplace (flat channel list) synced from EasyEcom and classified by the FDE
- Marketplace Account configured per (Company, Marketplace) in use
- All marketplace-relevant Items synced (push status = Synced)
- All Customers (including pseudo-customers) synced
- All Suppliers synced
- Tax Category mappings configured
- HSN-to-Item-Tax-Template fixture loaded; FDE has reviewed coverage for client's catalogue
- Account Role Map (per PRD Section 8.6) configured by client's CA
- Webhook endpoint registered with EasyEcom; webhook_token matched
- Field Mapping rulesets active for all 10 flows; Show Computed Mapping reviewed by FDE
- SLA Budgets configured per Company per flow
- Alert recipients configured per Company per severity
- Connection Health dashboard shows green for all Companies
- End-to-end smoke test: 1 PO + 1 GRN + 1 B2C order + 1 cancellation + 1 return — all flow through cleanly
- Methodology v0 Defaults loaded; client CA has reviewed and accepted
- FDE has signed off on the pre-flight checklist before client go-live

## 28.8 Test data lifecycle

- Test site rebuilt nightly from fixtures (no accumulating state)
- Sandbox EE account state managed via teardown hooks (every test cleans up its own EE-side records)
- Performance and load tests run on dedicated test sites; never against pilot or production
- Test data never includes PII from real clients (synthetic data only)

# 29. Phasing of Integration Build inside v0.1

This section breaks down the integration's contribution to the v0.1 timeline. The integration is roughly 70% of v0.1 engineering effort. The remaining 30% is the recon engine, AI assistant, methodology embedding, and FDE tooling. The integration's large share reflects the substantial operational-surface work specified in Sections 17-29.

## 29.1 Phasing model

Within v0.1 there are two release tiers:

- v0.1-alpha (week 32) — integration mechanics complete; the four must-have operational pieces shipped (path-based Field Mapping, recon-aware alerts, Morning Brief, error translation). Internal-use ready, FDE-supportable, suitable for the first paying client.
- v0.1 (week 46) — alpha plus the remaining six operational directions (queryable analytics, replay tooling, schema drift, SLA tracking, cross-Company ops, time travel). Generally available to FDE team.

## 29.2 Week-by-week build plan

| Phase | Weeks | Deliverable |
| --- | --- | --- |
| Foundation — connection & records | 1-3 | EasyEcom Account (account-level config) and EasyEcom Company Settings (per-Company), EasyEcom Location, three log DocTypes (Sync Record, API Call, Webhook Event), Queue Job; EasyEcomClient with auth, rate limiting, correlation IDs; Webhook receiver with bearer-token auth + IP allowlist; basic Connection Health |
| Foundation — the integration contract | 3-5 | The Section 7 contract made real, before any business flow: per-record-isolation batch processing (savepoint per record), the Queue Job Partial state with succeeded_count/failed_count, the mandatory logging+correlation wiring enforced centrally in the client and webhook receiver, the per-record and per-job state machines, the foundational-call class and bootstrap order (token → location discovery → mapping, Section 7.7), and the surfacing+disposition plumbing (Failed/Partial visible on the Workspace, retry/replay disposition). This is the base every subsequent API and flow plugs into; it is built and tested against the token and location-discovery calls — which are themselves the first calls implemented — before any entity-sync flow begins |
| Field Mapping engine | 5-8 | EasyEcom Field Mapping DocType, path-based syntax, transformer types, conditional rules, computed fields, identity-default modes; ruleset versioning; FDE-editable UI with Show Computed Mapping action |
| Master sync | 9-13 | Item, Customer, Supplier, Warehouse, Tax Category, Channel sync — bidirectional through Field Mapping rulesets, with ownership matrix and conflict resolution |
| Buying flow | 14-18 | PO push, GRN polling and webhook, Purchase Receipt creation with batch/serial/expiry/rejected handling, multi-warehouse PO splitting |
| Stock transfers | 19-21 | All four matrix cases including in-transit warehouse pattern |
| B2C sales | 22-25 | Manifest detection, SI creation, marketplace pseudo-customer, e-invoice integration, Marketplace Order Map |
| B2B sales | 26-28 | SO push (sync and async modes), Stock Reservation Entry mirroring, invoice + e-waybill flow Branch A and Branch B |
| Returns and cancellations | 29-31 | All six flows; refund accounting; correlation to settlement events |
| Must-have operational surface | 29-32 | Parallel with returns/cancellations: Recon-Aware Alerts (Section 23), Morning Brief (Section 24), Error Translation library (Section 25); Operational Workspace (Section 17) |
| v0.1-alpha cut | 32 | Internal release. Integration mechanics + must-have ops surface complete. Pilot-ready |
| Hardening + analytics layer | 33-36 | Queryable analytics on top of three logs (Section 17.3-18.5: number cards, dashboard charts, saved reports) |
| Replay tooling | 33-36 | Parallel: Replay Plan DocType, dry-run mode, bulk replay with filter, conditional retry, payload override (Section 19) |
| SLA tracking + Cross-Company ops | 37-40 | SLA Budget DocType, in-context document indicators, breach tracking (Section 21); cross-Company aggregator workspace (Section 22) |
| Schema drift + Time travel | 41-44 | Schema Snapshot DocType, payload sample archive, mapping coverage report (Section 20); Configuration Audit log, Field Mapping versioning, point-in-time queries (Section 26) |
| v0.1 final hardening | 45-46 | End-to-end soak test, FDE playbook v1.0, pilot-client validation pass, performance test for v0.5 scale |

The plan above assumes five engineers throughout. With four engineers, multiply weeks by 1.25. With six engineers, the parallel tracks shorten by ~10%; the foundational dependencies prevent linear scaling beyond that. The hard ordering constraints are: the integration contract (Section 7) is built and tested — against the token and location-discovery calls — before any entity-sync flow; the Field Mapping engine precedes Master sync; and Master sync precedes the buying, sales, and returns flows. No per-API flow (Sections 8-13) may be started until the contract is complete, because each flow is, by design (Section 7.6), only a declaration of endpoint + mapping + unit-of-work that inherits all of its logging, isolation, state-machine, surfacing, and retry behaviour from the contract. Building a flow first would mean re-implementing that behaviour ad hoc and inconsistently — the exact outcome the contract exists to prevent.

## 29.3 Recon engine and AI parallel tracks

Concurrently with the integration, the recon engine and AI assistant teams build their pieces. The recon team's work feeds back into Section 23 (Recon-Aware Integration Alerts) — the alerts framework cannot meaningfully attach financial impact until the recon engine can compute it.

- Weeks 1-12: recon engine team builds the rate-card library, forecasting engine, and Settlement Forecast DocType (parallelizable since it doesn't need integration outputs yet)
- Weeks 13-24: recon engine builds the five reconciliations, with the integration delivering test data per flow as it lands
- Weeks 18-32: AI assistant team builds the FastAPI service, prompt library, classification rules, retrieval layer; classifier becomes the Error Translation auto-clusterer in Section 25
- Weeks 25-32: integrated end-to-end testing across integration + recon + AI
- Weeks 33-46: continuing alongside Phase 2 operational-surface work — recon engine fees Section 23 alerts and Section 21 SLA breach financial impact

## 29.4 v0.5 integration deliverables

- Settlement Templates for at least 4 marketplaces shipped as fixtures
- Rate Card Library entries for top marketplaces
- Multi-marketplace settlement ingestion at scale
- Aggregator topology validated with one real client
- Cross-Company Discrepancy reporting
- Performance hardening to v0.5 scale targets

## 29.5 v1.0 integration deliverables

- Aggregator topology supporting 10+ Companies in one site
- Self-service onboarding wizard for non-FDE-led integration setup
- Performance hardening to v1.0 scale targets (100k orders/month)
- Frappe Cloud Marketplace listing of the parent app
- Documented partner-FDE certification for the integration setup

# 30. Open Questions and v2 Candidates

## 30.1 Deferred to v0.5

- Direct marketplace API connectors (Amazon SP-API, Flipkart Seller API, Shopify, Meesho, Myntra) — would let us ingest settlement data without file uploads. Substantial scope per marketplace; deferred to keep v0.1 focused on EasyEcom-mediated flows.
- EasyEcom B2B Invoice Generation API — for clients who prefer EE to generate B2B invoices instead of ERPNext (current default is ERPNext). Trivial to add when needed.
- Selective field sync — letting clients sync only some Item attributes (e.g., not pushing item_image to EE). Field Mapping engine makes this straightforward; deferred only because no v0.1 client has asked
- Self-service master sync UI — currently FDE-driven; v0.5 will add a self-service onboarding wizard
- Translation Library admin contributions from non-Anthropic FDEs (governance and review process needed)
- Cross-tenant translation library sharing (with privacy guarantees) — could become a network effect
- Inventory variance recon — separately scoped post-v0.1 enhancement to the recon engine

## 30.2 Deferred to v1.0

- Multi-currency support — orders, settlements, invoices in non-INR. Not v0.5 scope either
- Aggregated SLA tracking with cross-document correlation (single dashboard view of ALL flows' real-time SLA progress) — v0.1 ships per-document banners via frappe.publish_realtime; aggregated view is v1.0 work
- ML-based root cause inference for Untranslated errors — v0.1 uses TF-IDF clustering; ML would be more accurate but heavier infrastructure
- Predictive alerting (alert before failures occur based on patterns) — requires established baseline; depends on enough production data
- Causal graph visualisation for time-travel queries
- Frappe Cloud Marketplace listing of the parent app — requires tenant isolation hardening beyond v0.1 scope

## 30.3 Currently uncertain — to validate during v0.1 build

- EasyEcom per-tier behaviour under burst — the tier ceilings are documented (Section 3.10), but real burst tolerance and quota-reset timing (rolling vs midnight) will be confirmed empirically during pilot
- Webhook reliability — will measure actual delivery rate against polling-detected events; informs whether webhook-as-optimization is worth the configuration cost
- Stock Reservation Entry behaviour at scale — v16's expanded SRE is faster than v15's, but still uncharacterized for the high-frequency reserve/unreserve cycles seen with high-volume B2B clients
- e-waybill auto-generation latency — IRP API responsiveness in production volumes
- JWT concurrency at scale — one account credential set mints a JWT per location_key, renewed on day 85; for accounts with many locations the renewal job fans out many token calls on the same day, so renewals are spread/jittered across the renewal window to avoid a thundering herd
- Field Mapping engine performance overhead — declarative engine has compile-time + runtime cost vs. hardcoded; need to characterise on production-shaped payloads
- Schema drift false-positive rate — too sensitive and the FDE gets noise; too lenient and we miss real drift

## 30.4 Key design decisions and rationale

- Build our own integration rather than depend on erpnext_easyecom — accepted increased engineering scope for control
- ERPNext v16 only — chose feature reliance over backwards compatibility
- Polling-first, webhooks as optimization — chose deterministic catch-up over minimum latency
- Bidirectional Item master with field-level ownership matrix — chose explicit conflict resolution over picking a single direction
- Path-based Field Mapping engine — chose configurability over hardcoded simplicity
- Three-log model (Sync Record + API Call + Webhook Event) — chose separation of concerns over unified log
- Recon-aware alerts with financial impact — chose product wedge over standard 'thing failed' alerts
- Morning Brief as a first-class deliverable — chose directive UX over diagnostic UX
- Error Translation Library — chose plain-English over raw error pass-through
- Time travel via Configuration Audit + Field Mapping Versions — chose forensic capability over storage minimization
- All 10 flows in v0.1 plus full operational surface — chose product completeness over time-to-pilot. Resulting v0.1 timeline: 38-46 weeks
- EasyEcom Account (account-level) and EasyEcom Company Settings (per-Company) — chose an account-scoped credential boundary with location-resolved Companies

## 30.5 Honest reservation

This spec describes a substantial product. The v0.1 scope (38-46 weeks with five engineers) is large for a pre-pilot stage. The authors of this spec acknowledge that an alternative path — ship a much smaller v0.1 (4 must-have ops pieces only, ~20 weeks), get a real client live, then have their operational pain dictate which of the other 6 to build — would be the textbook agile choice.

The choice to ship the larger v0.1 reflects a deliberate methodology bet: this product's operational surface is the differentiator, and shipping it incrementally would leave the first paying client looking at a partial experience that doesn't yet justify the price point. The bet may not pay off; the agile alternative remains available as a fallback if the timeline or capital required becomes prohibitive.

Reviewers of this spec are encouraged to push back on this scope decision before substantial engineering investment is made.

# 31. Implementation Reference

This section is the implementation-ready appendix. Every prior section describes intent, behaviour, and operational surface. This section provides the concrete artifacts engineering and Claude Code will produce: file paths, DocType field schemas, API endpoint URLs, Python function signatures, error classes, fixture inventory, permissions, and Frappe hooks. Where the prior sections say 'a Sync Record DocType,' this section says 'fields ecs_sync_record_id (Data, autoname format ECS-SR-YYYY-MM-DD-######), company (Link → Company, mandatory, indexed), entity_doctype (Data, mandatory)...'.

This section does not introduce new behaviour. Every concept here was specified in Sections 1-30; this section just makes each concept implementation-ready. If you find a contradiction between this section and an earlier section, the earlier section wins (it's the design; this is the schema).

## 31.1 Frappe app file structure

The integration is delivered as two Frappe apps: ecommerce_super (parent app, methodology + integration core) and ecommerce_super_<client> (per-client extension app). The parent app is the source of all DocTypes, fixtures, hooks, and integration code; the client app contains client-specific overrides only.

### 31.1.1 Parent app: ecommerce_super

```
apps/ecommerce_super/
├── ecommerce_super/
│   ├── __init__.py                      # version = "0.0.1" (pre-build; bump to 0.1.0 at the v0.1-alpha cut, Section 29)
│   ├── hooks.py                          # Frappe hook registry (see 31.8)
│   ├── modules.txt                       # Module list
│   ├── patches.txt                       # Migration patches
│   ├── public/                           # Static assets
│   │   ├── js/                           # Custom Frappe JS (action menus etc.)
│   │   └── css/
│   ├── config/                           # Workspace JSON, dashboard chart config
│   │   ├── easyecom_workspace.json
│   │   └── easyecom_cross_company_workspace.json
│   ├── easyecom/                         # Main integration module
│   │   ├── __init__.py
│   │   ├── doctype/                      # All DocTypes (see 31.2)
│   │   │   ├── easyecom_account/
│   │   │   ├── easyecom_company_settings/
│   │   │   ├── easyecom_location/
│   │   │   ├── easyecom_sync_record/
│   │   │   ├── easyecom_api_call/
│   │   │   ├── easyecom_webhook_event/
│   │   │   ├── easyecom_queue_job/
│   │   │   ├── easyecom_sync_cursor/
│   │   │   ├── easyecom_field_mapping/
│   │   │   ├── easyecom_field_mapping_version/
│   │   │   ├── easyecom_replay_plan/
│   │   │   ├── easyecom_schema_snapshot/
│   │   │   ├── easyecom_payload_sample/
│   │   │   ├── easyecom_error_translation/
│   │   │   ├── easyecom_sla_budget/
│   │   │   ├── easyecom_sla_breach/
│   │   │   ├── easyecom_configuration_audit/
│   │   │   ├── easyecom_morning_brief_snapshot/
│   │   │   ├── marketplace_account/
│   │   │   ├── marketplace/
│   │   │   ├── marketplace_order_map/
│   │   │   ├── integration_discrepancy/
│   │   │   └── source_of_truth_map/
│   │   ├── api/                          # @frappe.whitelist() endpoints
│   │   │   ├── __init__.py
│   │   │   ├── webhook.py                # POST /api/method/...webhook receiver
│   │   │   ├── sync.py                   # Sync Now actions
│   │   │   ├── replay.py                 # Replay Plan endpoints
│   │   │   └── inspector.py              # Inspector view backend
│   │   ├── client/                       # EasyEcom HTTP client
│   │   │   ├── __init__.py
│   │   │   ├── auth.py                   # JWT acquisition, refresh, cache
│   │   │   ├── client.py                 # EasyEcomClient class (see 31.4)
│   │   │   ├── endpoints.py              # Endpoint definitions (see 31.3)
│   │   │   ├── rate_limit.py             # Token-bucket rate limiter
│   │   │   └── retry.py                  # Retry policy with back-off
│   │   ├── flows/                        # Integration flow implementations
│   │   │   ├── master_sync/
│   │   │   │   ├── item.py
│   │   │   │   ├── customer.py
│   │   │   │   ├── supplier.py
│   │   │   │   ├── warehouse.py
│   │   │   │   ├── tax_category.py
│   │   │   │   └── channel.py
│   │   │   ├── buying.py                 # PO push, GRN pull, PR creation
│   │   │   ├── stock_transfer.py
│   │   │   ├── b2b_sales.py
│   │   │   ├── b2c_sales.py
│   │   │   └── returns_cancellations.py
│   │   ├── field_mapping/                # Field Mapping engine
│   │   │   ├── __init__.py
│   │   │   ├── compiler.py               # Compiles ruleset → executable form
│   │   │   ├── executor.py               # Applies compiled ruleset to a record
│   │   │   ├── path.py                   # JSONPath subset parser
│   │   │   ├── transformers.py           # Closed transformer vocabulary
│   │   │   └── exceptions.py             # FieldMappingRuleError etc.
│   │   ├── operational/                  # Operational surface backends
│   │   │   ├── __init__.py
│   │   │   ├── morning_brief.py          # Daily snapshot generation
│   │   │   ├── error_translation.py      # Pattern matching + clustering
│   │   │   ├── replay.py                 # Replay Plan execution engine
│   │   │   ├── schema_drift.py           # Hash + Jaccard distance
│   │   │   ├── sla_tracking.py           # Percentile computation, breach detection
│   │   │   ├── alerts.py                 # Alert routing, suppression
│   │   │   ├── time_travel.py            # Point-in-time queries
│   │   │   └── recon_impact.py           # Financial impact computation
│   │   ├── queue/                        # Queue facade over frappe.enqueue / RQ
│   │   │   ├── __init__.py               # enqueue_easyecom_job, cancel, retry
│   │   │   ├── routing.py                # job_type → queue tier + timeout maps
│   │   │   ├── workers.py                # execute_job worker entry point + JOB_TYPE_HANDLERS
│   │   │   └── concurrency.py            # company_concurrency_semaphore (frappe.cache)
│   │   ├── exceptions.py                 # Top-level exception hierarchy (31.5)
│   │   └── utils/
│   │       ├── correlation.py            # UUIDv7 generation
│   │       ├── hashing.py                # SHA-256 with normalisation
│   │       ├── redaction.py              # Credential-aware redaction
│   │       └── jsonpath.py               # JSONPath subset utilities
│   ├── recon/                            # Reconciliation engine
│   │   ├── __init__.py
│   │   ├── doctype/
│   │   │   ├── settlement_template/
│   │   │   ├── settlement_forecast/
│   │   │   ├── settlement_line/
│   │   │   ├── rate_card/
│   │   │   ├── recon_run/
│   │   │   └── recon_discrepancy/
│   │   ├── ingestion/                    # Settlement file ingestion
│   │   │   ├── csv_ingest.py
│   │   │   ├── xlsx_ingest.py
│   │   │   └── pdf_ingest.py
│   │   ├── reconciliations/              # Five reconciliations
│   │   │   ├── order_to_settlement.py
│   │   │   ├── return_to_credit_note.py
│   │   │   ├── fee_to_expense.py
│   │   │   ├── tcs_tds_to_government.py
│   │   │   └── inventory_variance.py
│   │   ├── forecasting/
│   │   │   └── forecaster.py
│   │   └── pricing/
│   │       └── diagnostics.py
│   ├── ai/                               # AI assistant (FastAPI service stub for v0.1)
│   │   ├── __init__.py
│   │   ├── service.py                    # FastAPI app
│   │   ├── prompts/
│   │   ├── classifiers/
│   │   └── retrieval/
│   ├── methodology/                      # Methodology defaults (BRD-driven)
│   │   ├── account_role_map_defaults.py
│   │   ├── disposition_rules.py
│   │   └── policy_defaults.py
│   ├── fixtures/                          # Shipped fixture data (see 31.6)
│   │   ├── easyecom_field_mapping.json
│   │   ├── easyecom_error_translation.json
│   │   ├── easyecom_sla_budget.json
│   │   ├── marketplace.json
│   │   ├── role.json
│   │   └── workspace.json
│   ├── tests/                             # Test suite (see Section 28)
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   ├── factories/
│   │   ├── fixtures/
│   │   ├── cassettes/
│   │   ├── sample_payloads/
│   │   ├── unit/
│   │   ├── integration/
│   │   ├── contract/
│   │   └── e2e/
│   └── www/                               # Custom web pages (Inspector, etc.)
│       ├── easyecom_inspector.html
│       ├── easyecom_event_timeline.html
│       └── easyecom_morning_brief.html
├── pyproject.toml
├── requirements.txt
├── README.md
└── license.txt
```

### 31.1.2 Client app: ecommerce_super_<client>

Per-client app for client-specific overrides. Has the same structure as the parent app but contains only:

- Custom DocTypes specific to this client (rare; usually the parent app suffices)
- Per-client Field Mapping fixtures (overrides to global rulesets)
- Per-client Error Translation entries (Company-scoped overrides)
- Per-client SLA Budget fixtures
- Per-client Methodology defaults (Account Role Map adjustments, etc.)
- Client-specific tests (e2e tests against the client's actual EE sandbox)

### 31.1.3 The hooks.py contract

Frappe's hooks.py is the integration's wiring file. The parent app's hooks.py registers (see Section 31.8 for the full registry):

- doc_events for Sales Order, Sales Invoice, Purchase Order, Purchase Receipt, Stock Entry, Item, Customer, Supplier — drives push hooks
- scheduler_events for periodic pulls and Morning Brief generation
- permission_query_conditions for Company-scoped data isolation
- has_permission for cross-Company permission rules
- override_doctype_class for ERPNext core DocType extensions (rare)
- after_install for first-run setup (creating default Roles, loading initial fixtures)

## 31.2 DocType reference

Every DocType in the parent app is listed here with its full field schema. The format is:

```
fieldname  fieldtype  mandatory  options/notes
```

fieldtype values match Frappe's standard set: Data, Long Text, Select, Check, Int, Float, Currency, Date, Datetime, Time, Link, Dynamic Link, Table, Table MultiSelect, Code, JSON, Password, Attach, Attach Image, Read Only. mandatory is Y or N. options is the link target for Link fields, the option list for Select fields, or notes for others.

### 31.2.1 EasyEcom Account

Account-level configuration (Section 3.3). One record per client deployment. Holds credentials and account-wide operational config. Collapsible policy sections.

```
# Header strip (Section 3.3.1)
account_name             Data       Y   Human-readable label for this deployment's EE account
enabled                  Check      Y   Default 1 (account-wide kill-switch)
connection_status        Select     Y   Connected | Degraded | Down | Disabled (read-only, computed)
last_successful_sync_at  Datetime   N   (read-only)
environment_badge        Select     Y   Sandbox | Production

# Setup section (Section 3.3.2)
api_endpoint             Data       Y   Production https://api.easyecom.io
x_api_key                Password   Y   Account-level; generated only from Primary Seller Account; no auto-expire; regeneration invalidates old instantly. Encrypted at rest
email                    Password   Y   User with multi-location access in primary account. Credential: encrypted, write-only (Section 3.7)
password                 Password   Y   Encrypted at rest
rate_limit_tier          Select     Y   Default | Bronze | Silver | Gold | Diamond (no preset default; FDE sets to the tier EE assigned). Drives throttle + daily quota (Section 3.10)
default_location_key     Link       N   EasyEcom Location (typically the primary location)

# Sync Window section (Section 3.3.3)
sync_window_enabled      Check      N   Default 0
sync_window_start        Time       N   Default 22:00
sync_window_end          Time       N   Default 06:00 (may cross midnight)
sync_window_weekends_only Check     N   Default 0
pause_until              Datetime   N
window_exemptions        Table      N   Sync Window Exemption (child)

# Sync Tuning section (Section 3.3.4)
poll_interval_orders_min Int        Y   Default 5
poll_interval_returns_min Int       Y   Default 15
poll_interval_grn_min    Int        Y   Default 30
poll_interval_inventory_min Int     Y   Default 60
poll_interval_po_status_min Int     Y   Default 60
poll_interval_master_products_hours Int Y Default 24
poll_interval_locations_hours Int   Y   Default 24
max_throughput_per_sec   Int        Y   Derives from rate_limit_tier (Section 3.10); FDE may set lower, never above tier ceiling
max_concurrent_workers   Int        Y   Default 4 (account-wide; per-Company sub-limits in 11.3.7)
batch_size_items_push    Int        Y   Default 50
batch_size_orders_pull   Int        Y   Default 100
sync_enabled_orders      Check      Y   Default 1
sync_enabled_inventory   Check      Y   Default 1
sync_enabled_returns     Check      Y   Default 1
sync_enabled_grn         Check      Y   Default 1
sync_enabled_master_products Check  Y   Default 1
push_so_mode             Select     Y   Async | Sync (default Async)
push_so_block_on_error   Check      Y   Default 0

# Inbound Webhook Auth section (Section 3.3.5)
webhook_enabled          Check      Y   Default 1
webhook_token            Password   Y   Required if webhook_enabled; accepted via Access-token or Authorization: Bearer header
webhook_allowed_ips      Long Text  N   CIDR list, one per line
webhook_max_age_seconds  Int        Y   Default 300
webhook_dedup_window_minutes Int    Y   Default 60
webhook_endpoint_url_display Read Only Y (computed display)

# GRN/Inward Policy section (Section 3.3.6) — account-wide defaults
default_rejected_warehouse  Link    N   Warehouse (account-wide fallback; per-Company override available)
default_in_transit_warehouse Link   N   Warehouse (account-wide fallback)
allow_over_receipt_pct   Percent    Y   Default 0
allow_under_receipt_pct  Percent    Y   Default 0
mandatory_batch_for_groups Table    N   Item Group MultiSelect (child)
mandatory_serial_for_groups Table   N   Item Group MultiSelect (child)
mandatory_expiry_for_groups Table   N   Item Group MultiSelect (child)
tax_variance_tolerance_pct Percent  Y   Default 1
lost_in_transit_threshold_days Int  Y   Default 30

# Internal (not user-visible)
last_full_sync_at        Datetime   N   (read-only)
```

### 31.2.1a EasyEcom Company Settings

Per-Company configuration (Section 3.5). One record per operational Company. Deliberately thin — most config is account-level.

```
# Header strip (Section 3.5.1)
company                  Link       Y   Company (autoname; primary key)
enabled                  Check      Y   Default 1 (per-Company kill-switch)
connection_status        Select     Y   read-only, rollup across this Company's operational locations
last_successful_sync_at  Datetime   N   (read-only)

# Alerts section (Section 3.5.2)
alert_recipients_critical Table     N   Alert Recipient (child)
alert_recipients_error   Table      N   Alert Recipient (child)
alert_recipients_warning Table      N   Alert Recipient (child)
queue_depth_warning_threshold Int   Y   Default 500
queue_depth_critical_threshold Int  Y   Default 1000
api_error_rate_warning_pct Percent  Y   Default 5
api_error_rate_critical_pct Percent Y   Default 20
webhook_gap_warning_minutes Int     Y   Default 60
sync_lag_warning_minutes Int        Y   Default 30
daily_digest_enabled     Check      Y   Default 1
daily_digest_time        Time       Y   Default 08:00
weekly_summary_enabled   Check      Y   Default 1

# Notifications section (Section 3.5.3)
email_template_critical  Link       N   Email Template
email_template_error     Link       N   Email Template
email_template_warning   Link       N   Email Template
slack_webhook_url        Password   N
slack_channel_critical   Data       N
slack_channel_error      Data       N
banner_show_to_all_users Check      Y   Default 0
digest_recipients        Table      N   User MultiSelect (child)
assigned_fde             Link       N   User (drives Discrepancy auto-routing)
default_rejected_warehouse_override  Link  N  Warehouse (optional per-Company override)
default_in_transit_warehouse_override Link N  Warehouse (optional per-Company override)
```

### 31.2.2 EasyEcom Location

One record per location_key (Section 3.4). Carries primary/operational flags, Company resolution, warehouse mapping, JWT cache, per-location pull cursors, and the EE-supplied location attributes from `/getAllLocation` (§8.4.1).

```
location_key             Data       Y   Unique within the account; autoname format ECS-LOC-{key}
location_name            Data       Y
is_primary               Check      Y   Default 0. Exactly one location per account has this set.
                                         FDE-designated — NOT supplied by /getAllLocation
is_operational           Check      Y   Default 0. Workflow-DERIVED (set by Go Live transition);
                                         not user-toggled, not from payload
is_wms_location          Check      Y   Default 0. Derived on discovery from EE stockHandle
                                         (stockHandle=1 → 1). WMS plan (PO/GRN/cycle-count/putaway)
                                         vs OMS-only. Gates the Section 9 buying/GRN flow. FDE may override
serialization_enabled    Check      Y   Default 0. If set, GRN qty pushed per-serial (Section 9)
frappe_company           Link       N   Company. Set iff is_operational. Nullable. NOT unique (many-to-one)
mapped_warehouse         Link       N   Warehouse (within frappe_company); blank for non-operational locations
ee_company_id            Data       N   EasyEcom internal company id (payload: company_id)
is_store                 Check      N   EE store-vs-warehouse flag (payload: is_store)
copy_master_from_primary Check      N   Whether this location inherits masters from primary
                                         (payload: copy_master_from_primary). Recorded; not a primary signal
city                     Data       N   payload: city
state                    Data       N   payload: state — GST place-of-supply critical
country                  Data       N   payload: country
pincode                  Data       N   payload: zip (name differs)
address_line             Data       N   payload: address (flat string)
billing_street           Data       N   payload: address type.billing_address.street
billing_state            Data       N   payload: address type.billing_address.state
billing_zipcode          Data       N   payload: address type.billing_address.zipcode
billing_country          Data       N   payload: address type.billing_address.country
pickup_street            Data       N   payload: address type.pickup_address.street
pickup_state             Data       N   payload: address type.pickup_address.state
pickup_zipcode           Data       N   payload: address type.pickup_address.zipcode
pickup_country           Data       N   payload: address type.pickup_address.country
gstin                    Data       N   Validated against India Compliance (operational locations).
                                         NOT supplied by /getAllLocation — FDE-set, never inferred
jwt_token                Long Text  N   Cached JWT for this location_key, encrypted at rest (read-only)
jwt_acquired_at          Datetime   N   (read-only)
jwt_expires_at           Datetime   N   (read-only) 90-day validity; proactive refresh ahead of expiry
enabled                  Check      Y   Default 1 (per-location kill-switch)
last_pull_orders         Datetime   N   Cursor (read-only)
last_pull_returns        Datetime   N   Cursor (read-only)
last_pull_grn            Datetime   N   Cursor (read-only)

# Dropped from the /getAllLocation payload (not stored):
#  - api_token : irrelevant today; a credential-shaped string — never stored; redacted if ever logged
#  - userId, "phone number" : not currently mapped. NB "phone number" has a literal space in
#    the EE key — the mapping engine must tolerate space-bearing source keys
#  - (the former spec field ee_company_value is removed — no such field in the real payload)

# Validation:
#  - exactly one location per account has is_primary = 1 (FDE-set)
#  - frappe_company presence is governed by workflow state (state-aware invariant):
#      To Map / Skipped (unmapped states)        → frappe_company MUST be empty
#      Mapped but not Live / Live (mapped states) → frappe_company MUST be set
#    Moving to an unmapped state auto-clears frappe_company and mapped_warehouse.
#    The invariant short-circuits when workflow_state is empty (back-fill exemption).
#    (This supersedes the older "required iff is_operational" rule: is_operational=1
#     only occurs in state Live, which already requires a Company.)
#  - frappe_company is non-unique by design (many locations may resolve to one Company)
#  - a location with neither flag set is inert (recorded but not synced or transacted)
#
# Workflow (per §8.4.1): a Frappe Workflow is attached to this DocType (shipped as a
# fixture), adding the standard workflow_state field with states
# To Map | Mapped but not Live | Live | Skipped. The workflow state is the source of
# truth for whether the location is operational; is_operational is DERIVED from it
# (set true by the Go Live transition). Discovery-pull creates new rows in state To Map,
# pre-populating is_wms_location from stockHandle.
```

### 31.2.3 EasyEcom Sync Record

Entity-centric. One row per (ERPNext document, sync direction). Mutable across retries (not append-only).

```
sync_record_id           Data       Y   autoname ECS-SR-YYYY-MM-DD-######
company                  Link       Y   Company (indexed; composite leading)
entity_doctype           Data       Y   Frappe DocType being synced (e.g., 'Item')
entity_name              Dynamic Link Y entity_doctype as link target
entity_type              Select     Y   Item | Customer | Supplier | Warehouse | Tax Category | Channel | Sales Order | Purchase Order | Sales Invoice | Purchase Receipt | Stock Entry | Order | GRN | Return | Cancellation | Manifest | Dispatch
direction                Select     Y   Push | Pull
ee_id                    Data       N   EasyEcom-side identifier (when known)
ee_location_key          Link       N   EasyEcom Location
status                   Select     Y   Pending | Running | Success | Failed | Cancelled | AlreadySynced
correlation_id           Data       Y   UUIDv7 (indexed)
parent_correlation_id    Data       N
idempotency_key          Data       Y   per Section 6.1 (indexed)
attempts                 Int        Y   Default 0
last_attempt_at          Datetime   N
last_attempt_tz          Data       N   e.g., 'Asia/Kolkata'
last_error               Long Text  N
last_error_translation_key Data     N   FK to EasyEcom Error Translation
field_mapping_used       Link       N   EasyEcom Field Mapping
field_mapping_version    Int        N
push_payload_hash        Data       N   SHA-256 of last pushed payload (for change detection)
pull_payload_hash        Data       N   SHA-256 of last pulled payload
last_request_payload     Long Text  N   (redacted)
last_response_payload    Long Text  N   (redacted)
ecs_replay_plan          Link       N   EasyEcom Replay Plan (if replay-induced)
ecs_replay_strategy      Data       N   per Section 19.2

# Indexes:
# (company, entity_doctype, entity_name, direction) UNIQUE
# (company, status) for list queries
# (correlation_id) for trace queries
# (idempotency_key) UNIQUE for retry detection
```

**Child table — EasyEcom Sync Record Line** (added with the first nested-document flow, Section 9 GRN; see Section 7.1.1). Populated only by flows whose unit of work is a composite document with nested lines (GRN, Order, Return); single-entity flows (Item/Customer/Supplier) leave it empty.

```
# child of EasyEcom Sync Record
source_line_ref          Data       Y   EE-side line identifier (e.g. SKU / line id)
target_field             Data       N   mapped ERPNext target (e.g. item_code)
line_status              Select     Y   OK | Failed | Discrepancy
reason                   Long Text  N   plain-English reason when not OK
ecs_integration_discrepancy Link    N   EasyEcom Integration Discrepancy (set when line_status=Discrepancy; Section 23)
```

### 31.2.4 EasyEcom API Call

Call-centric, append-only. One row per outbound HTTP call.

```
api_call_id              Data       Y   autoname ECS-AC-YYYY-MM-DD-########
easyecom_account         Link       Y   EasyEcom Account (always set; the call's account scope)
company                  Link       N   Company (indexed). Set for entity-sync calls; blank for
                                         foundational calls (token/location/test — Section 7.7)
is_foundational          Check      Y   Default 0. True for token, location-discovery, connection-test calls
location_key             Data       N   EasyEcom Location key, where the call is location-scoped
correlation_id           Data       Y   UUIDv7 (indexed)
sub_correlation_id       Data       Y   per-attempt UUID
parent_sync_record       Link       N   EasyEcom Sync Record
parent_queue_job         Link       N   EasyEcom Queue Job
endpoint                 Data       Y   path component, e.g., /Wms/Inventory/getStockSnapshot
http_method              Select     Y   GET | POST | PUT | PATCH
request_url              Data       Y   full URL (query params redacted)
request_headers          Long Text  Y   (Authorization redacted)
request_payload          Long Text  N   (PII redacted)
request_payload_hash     Data       Y   SHA-256
response_status_code     Int        N
response_headers         Long Text  N
response_payload         Long Text  N   (redacted)
response_payload_hash    Data       N   SHA-256 (for schema drift)
status                   Select     Y   Success | Failed | Timeout | Cancelled
latency_ms               Int        N
attempt_number           Int        Y   Default 1
attempted_at             Datetime   Y
completed_at             Datetime   N
error_class              Data       N   Python exception class
error_message            Long Text  N
error_translation_key    Data       N
schema_snapshot          Link       N   EasyEcom Schema Snapshot

# Indexes:
# (company, attempted_at DESC) for recent calls
# (correlation_id) for trace queries
# (endpoint, attempted_at DESC) for per-endpoint analysis
# (status, attempted_at DESC) for failure dashboard

# Retention: 90 days default; Configurable to 365 for compliance clients
```

### 31.2.5 EasyEcom Webhook Event

Inbound-centric, append-only.

```
webhook_event_id         Data       Y   autoname ECS-WE-YYYY-MM-DD-########
company                  Link       Y   Company (indexed)
event_type               Select     Y   manifest | dispatch | order_cancelled | return_received | grn_completed | inventory_reserved | inventory_released | (extensible)
ee_event_id              Data       Y   EasyEcom-supplied event ID
correlation_id           Data       Y   UUIDv7
received_at              Datetime   Y   indexed
http_method              Select     Y   POST (always)
source_ip                Data       Y
raw_payload              Long Text  Y   exact bytes received (redacted on save for PII)
payload_hash             Data       Y   SHA-256
auth_header_used         Select     Y   Access-token | Authorization (which header carried the token)
token_verified           Check      Y   computed at receipt
allowed_ip_check         Select     Y   Pass | Fail | Skipped
processing_state         Select     Y   Pending | Processing | Processed | Failed | Duplicate | Manually Handled
processing_started_at    Datetime   N
processing_completed_at  Datetime   N
processing_error         Long Text  N
spawned_queue_job        Link       N   EasyEcom Queue Job
downstream_documents     Long Text  N   JSON list of {doctype, name} created/updated
schema_snapshot          Link       N   EasyEcom Schema Snapshot

# Indexes:
# (company, event_type, ee_event_id) UNIQUE
# (received_at DESC) for recent webhooks
# (processing_state, received_at DESC)

# Retention: 90 days default
```

### 31.2.6 EasyEcom Queue Job

See Section 6.3.2 for the full schema (already specified there).

### 31.2.7 EasyEcom Sync Cursor

```
cursor_id                Data       Y   autoname ECS-CUR-{company}-{location}-{resource}
company                  Link       Y   Company (indexed)
location_key             Link       Y   EasyEcom Location
resource                 Select     Y   orders | grns | returns | inventory | po_status | master_products
cursor_value             Data       Y   ISO datetime or opaque token from EE
cursor_format            Select     Y   ISO Datetime | Next-Page URL | Opaque Token  (EE bulk APIs use Next-Page URL)
last_advanced_at         Datetime   Y
last_advanced_by         Data       Y   Worker | FDE Rewind | System
records_fetched_total    Int        Y   Default 0
records_fetched_last_run Int        Y   Default 0

# Indexes: (company, location_key, resource) UNIQUE
```

### 31.2.8 EasyEcom Field Mapping

```
mapping_name             Data       Y   Unique
entity_type              Select     Y   Item | Customer | Supplier | Warehouse | Tax Category | Channel | Order | GRN | etc.
direction                Select     Y   Push | Pull | Bidirectional
active                   Check      Y   Default 1
company_scope            Table      N   Company MultiSelect (empty = all)
missing_field_policy     Select     Y   Strict | Permissive | Drop (default Permissive)
preconditions            Long Text  N   Sandboxed Python expression
rules                    Table      N   EasyEcom Field Mapping Rule (child)
computed_fields          Table      N   EasyEcom Computed Field (child)
version                  Int        Y   auto-increment on save
last_modified_by         Link       Y   User
last_modified_at         Datetime   Y
change_reason            Small Text Y   Required on save

# Child: EasyEcom Field Mapping Rule
erpnext_path             Data       Y   JSONPath subset (Section 5.4)
easyecom_path            Data       Y
transform_push           Select     Y   Closed vocabulary (Section 5.5)
transform_pull           Select     Y
transform_args           JSON       N
condition                Long Text  N   Sandboxed Python expression
default_value            Data       N
validate_against         Data       N   Frappe DocType name
required                 Check      Y   Default 0
notes                    Small Text N

# Child: EasyEcom Computed Field
name                     Data       Y
expression               Long Text  Y   Sandboxed Python
output_type              Select     Y   Decimal | Int | String | Date | Datetime | Boolean | JSON
cache_per_record         Check      Y   Default 1
```

### 31.2.9 EasyEcom Field Mapping Version

Snapshot of a Field Mapping ruleset. Created automatically on every save of the parent. Append-only.

```
version_id               Data       Y   autoname ECS-FMV-{mapping_name}-v{version}
parent_mapping           Link       Y   EasyEcom Field Mapping
version                  Int        Y
snapshot_json            Long Text  Y   Full ruleset state as JSON
created_by               Link       Y   User
created_at               Datetime   Y
change_reason            Small Text Y
```

### 31.2.10 EasyEcom Replay Plan

See Section 19.1 for the full schema (already specified there).

### 31.2.11 EasyEcom Schema Snapshot

See Section 20.2 for the full schema (already specified there).

### 31.2.12 EasyEcom Payload Sample

See Section 20.3.

### 31.2.13 EasyEcom Error Translation

See Section 25.1.

### 31.2.14 EasyEcom SLA Budget

See Section 21.1.

### 31.2.15 EasyEcom SLA Breach

See Section 21.4.

### 31.2.16 EasyEcom Configuration Audit

Append-only. Cannot be deleted or edited by any user. See Section 26.2 for full schema.

### 31.2.17 EasyEcom Morning Brief Snapshot

See Section 24.2.

### 31.2.18 Marketplace (the flat channel list)

One row per EasyEcom channel, keyed by EE marketplace_id (account-level identity — deduped across locations). Read from `/current-channel-status` (Section 8.6) — a **per-location** call (per-location JWT), swept across **all** discovered locations and deduped by `marketplace_id` (skip if the row already exists). The payload→field mapping goes through the `EasyEcom-Channel-Pull` ruleset (§8.0 policy), not a hardcoded mapper. No parent/child channel hierarchy — EasyEcom is flat.

```
marketplace_id           Int        Y   EasyEcom numeric id; unique; autoname on this; the stable join key
marketplace_name         Data       Y   EE's name (e.g., 'Amazon.in', 'Cloudtail B2B', 'Customer Cash Sales')
display_name             Data       N
channel_type             Select     N   B2C Marketplace | B2B | Quick-Commerce | Own Storefront | POS-Offline | Connector-Ignore. FDE-classified via the workflow (blank until classified); the Classify transition is gated on this being set
country                  Link       N   Country (default India)
reporting_parent         Link       N   Marketplace — optional rollup for reporting only (e.g., Amazon.in + Amazon_FBA → an 'Amazon' group). FDE-set; not supplied by EE; not a claim about EE structure
default_customer_pattern Data       N   e.g., 'Amazon FBA Buyer Pool' (for B2C marketplace channels)
is_active                Check      Y   Active if the channel is Active on ANY location (catalogue-level, deduped across the per-location sweep). EE's integration status — a SEPARATE axis from workflow_state
enabled                  Check      Y   Default 1 (our-side per-channel kill-switch)

# Workflow (per §8.6.3): a Frappe Workflow is attached (shipped as a fixture, reusing the 8a
# Location pattern), adding workflow_state with states Unclassified → Classified → Active,
# branch Ignored. Discovery creates new rows in Unclassified. is_active (pulled) and
# workflow_state (our classification lifecycle) are independent.
#
# Deferred to reconciliation (NOT built in the Channel packet 8b):
#  - default_settlement_template — a settlement concern; lives with Marketplace Account (§8.6.2),
#    built when reconciliation is built.
```

### 31.2.19 (removed)

The former Marketplace Channel DocType is removed. EasyEcom has no marketplace→channel hierarchy; the flat Marketplace list (31.2.18) is the single channel model. Any prior reference to a separate Marketplace Channel record now means a row in the flat Marketplace list.

### 31.2.20 Marketplace Account

```
account_name             Data       Y   autoname {marketplace}-{company}-{seller_id}
company                  Link       Y   Company (indexed)
marketplace              Link       Y   Marketplace
marketplace_seller_id    Data       Y   the seller ID on the marketplace
gstin                    Data       N   GSTIN registered for this seller account
default_warehouse        Link       N   Warehouse
settlement_template      Link       Y   Settlement Template
auto_finance_leadership_escalation Check N Default 0
finance_leadership_recipients Table N   User MultiSelect

# Indexes: (company, marketplace, marketplace_seller_id) UNIQUE
```

### 31.2.21 Marketplace Order Map

```
map_id                   Data       Y   autoname ECS-MOM-{marketplace_short}-{order_id}
company                  Link       Y   Company
marketplace              Link       Y   Marketplace
marketplace_account      Link       Y   Marketplace Account
marketplace_order_id     Data       Y   the order ID on the marketplace
ecs_easyecom_order_id    Data       Y   the corresponding EE order ID
sales_invoice            Link       N   Sales Invoice (the recon join target)
manifest_event_id        Data       N
dispatch_event_id        Data       N
created_at               Datetime   Y
last_updated_at          Datetime   Y

# Indexes: (company, marketplace, marketplace_order_id) UNIQUE
#         (sales_invoice) for SI → Map lookup
```

### 31.2.22 Integration Discrepancy

```
discrepancy_id           Data       Y   autoname ECS-DISC-YYYY-MM-DD-#####
company                  Link       Y   Company (indexed)
discrepancy_type         Select     Y   (Section 15 enumerates types)
severity                 Select     Y   Critical | Error | Warning | Info
status                   Select     Y   Open | In Review | Resolved | Closed
title                    Data       Y
description              Long Text  Y
related_doctype          Data       N
related_name             Dynamic Link N
related_sync_record      Link       N
related_api_call         Link       N
related_webhook_event    Link       N
financial_impact_estimate Currency  N   per Section 23.2
financial_impact_basis   Select     N   Direct | Indirect | Cumulative
financial_impact_computation Long Text N
financial_impact_confidence Select  N   Estimated | Likely | Confirmed
financial_impact_recovered Currency N
created_at               Datetime   Y
acknowledged_at          Datetime   N
acknowledged_by          Link       N   User
resolved_at              Datetime   N
resolved_by              Link       N   User
resolution_notes         Long Text  N
assigned_fde             Link       N   User
escalation_level         Int        Y   Default 0
```

### 31.2.23 Source-of-Truth Map

Per-location (per-Warehouse) mapping. The full DocType — including the inventory-authority fields — is built with the Location packet (§8.4.1-8.4.2) and FDE-configured at onboarding; the later flows (§9-11) read these fields rather than add them. Keyed per location; location→Company is many-to-one, so a Company may own several rows.

```
map_name                 Data       Y   autoname {company}-{warehouse}
company                  Link       Y   Resolved from the linked location's frappe_company
warehouse                Link       Y   Warehouse
ee_location_key          Link       N   EasyEcom Location (null if ERPNext-only / internal-only)
is_linked                Check      Y   Computed: True iff ee_location_key is set
inventory_master         Select     Y   ERPNext | EasyEcom — authoritative running balance (read by §9/§10)
pr_origination           Select     Y   ERPNext direct | EasyEcom GRN flow — who originates PRs (read by §9)
adjustment_origination   Select     Y   ERPNext | EasyEcom — who originates stock adjustments (read by §10)
mirror_stock_reservations Check     N   Mirror EE reservation → ERPNext SRE (read by §11)
allow_negative_stock     Check      N   Default 0
enabled                  Check      Y   Default 1; per-warehouse kill-switch
notes                    Small Text N

# Indexes: (company, warehouse) UNIQUE
# All fields built with the Location packet; the §9-11 flows read the authority fields.
```

## 31.3 EasyEcom API endpoint reference

Concrete endpoint URLs the integration calls. Path components are templated where applicable. The base URL is from EasyEcom Account.api_endpoint. All authenticated endpoints carry the X-API-KEY header and the JWT Bearer token (per Section 3).

### 31.3.1 Authentication

```
POST /access/token
Headers: X-API-KEY, Content-Type: application/json
Request body: {"email": <email>, "password": <password>, "location_key": <key>}
Response: {"jwt": <token>, "expires_in": <seconds>, ...}
Cached for: 80% of expires_in (renew before expiry)
Rate limit: 1 call per location per 60 seconds maximum
```

### 31.3.2 Master endpoints

```
# Items
POST /Wms/Inventory/itemMasterUpload
GET  /Wms/Inventory/getItemMaster?location_key=<key>&page=<n>&itemCode=<code>

# Customers
POST /Customer/createCustomer
GET  /Customer/getCustomer?location_key=<key>&customerId=<id>

# Suppliers / Vendors
POST /Wms/Vendor/createVendor
GET  /Wms/Vendor/getVendor?location_key=<key>&vendorId=<id>

# Locations
GET  /getAllLocation                          # confirmed live; returns the account's location array (see §8.4.1 for the real payload + field mapping)

# Channels
GET  /current-channel-status                  # confirmed live; channels integrated ON ONE LOCATION (per-location JWT), with Active/Inactive status. Channel sync sweeps this across ALL locations + dedupes by marketplace_id (§8.6.3)
# GET /marketplaces/list  — NOT used (earlier-draft "full catalogue" assumption; /current-channel-status is sufficient)
```

### 31.3.3 Buying flow endpoints

```
# Purchase Orders
POST /Wms/Purchase/createPO
GET  /Wms/Purchase/getPO?location_key=<key>&po_id=<id>
GET  /Wms/Purchase/getPOStatus?location_key=<key>&po_id=<id>

# GRNs
GET  /Wms/Inventory/getGRN?location_key=<key>&from_date=<dt>&to_date=<dt>&page=<n>
GET  /Wms/Inventory/getGRNDetails?location_key=<key>&grn_id=<id>
```

### 31.3.4 Sales flow endpoints

```
# Orders (B2C / marketplace)
GET  /orders/V2/getAllOrders?location_key=<key>&from_date=<dt>&to_date=<dt>
     # bulk response carries a Next-page URL; follow it (with the Base URL) for subsequent pages
GET  /orders/V2/getOrderDetails?location_key=<key>&order_id=<id>

# B2B Sales Orders
POST /b2b/createSalesOrder
GET  /b2b/getSalesOrder?location_key=<key>&so_id=<id>
POST /b2b/uploadInvoice  # body includes PDF, IRN, e-waybill

# Stock Reservations (read-only; mirrored)
GET  /Wms/Inventory/getReservedStock?location_key=<key>&order_id=<id>

# Manifests / Dispatches
GET  /Wms/Inventory/getManifest?location_key=<key>&from_date=<dt>&to_date=<dt>
GET  /Wms/Inventory/getDispatch?location_key=<key>&from_date=<dt>&to_date=<dt>
```

### 31.3.5 Returns and cancellations

```
GET  /returns/getReturnsV3?location_key=<key>&from_date=<dt>&to_date=<dt>&page=<n>
GET  /returns/getReturnDetails?location_key=<key>&return_id=<id>
GET  /orders/V2/getCancelledOrders?location_key=<key>&from_date=<dt>&to_date=<dt>&page=<n>
```

### 31.3.6 Inventory

```
GET  /Wms/Inventory/getStockSnapshot?location_key=<key>&item_code=<sku>
GET  /Wms/Inventory/getStockMovements?location_key=<key>&from_date=<dt>&to_date=<dt>
POST /Wms/Inventory/uploadStockAdjustment
```

### 31.3.7 Webhook receiver (inbound)

```
POST /api/method/ecommerce_super.api.webhook.receive?company=<frappe_company>
Headers: Access-token: <token> OR Authorization: Bearer <token>; X-EE-Event-Id: <id>, X-EE-Event-Type: <type>
Response: 200 with empty body (always; even on dedup); 401 if token missing or invalid

# Possible event types:
# - manifest, dispatch, order_cancelled
# - return_received, grn_completed
# - inventory_reserved, inventory_released
# - po_status_changed, item_updated
```

## 31.4 Internal Python API surface

Python function signatures the integration exposes for use by other modules and tests. All public functions have type hints; private helpers are prefixed with underscore.

### 31.4.1 EasyEcomClient class

```
# ecommerce_super/easyecom/client/client.py

from typing import Optional, Any
from datetime import datetime

class EasyEcomClient:
    def __init__(self, company: Optional[str] = None, location_key: Optional[str] = None) -> None: ...
    # company is optional: foundational calls (§7.7) — token acquisition, location
    # discovery — are account-scoped and have no company (their API Call rows carry
    # company=None, is_foundational=1). Operational calls pass company.

    def get_jwt(self) -> str: ...
    def refresh_jwt(self) -> str: ...

    def get(self, endpoint: str, params: dict | None = None,
            *, timeout: int = 60, idempotency_key: str | None = None,
            correlation_id: str | None = None) -> dict: ...

    def post(self, endpoint: str, payload: dict,
             *, timeout: int = 60, idempotency_key: str | None = None,
             correlation_id: str | None = None) -> dict: ...

    def paginated(self, endpoint: str, params: dict, *,
                  page_size: int = 100, max_pages: int | None = None
                  ) -> Iterator[dict]: ...
    # EasyEcom bulk endpoints return a Next-page URL as the pagination cursor;
    # `paginated` follows that URL (resolved against the Base URL) until exhausted,
    # rather than incrementing a page-number parameter.

# Records every call to EasyEcom API Call DocType automatically.
# Raises EasyEcomAPIError subclasses (Section 31.5) on failure.
# Honours rate limit and retry policy from Settings.
```

### 31.4.2 Field Mapping engine

```
# ecommerce_super/easyecom/field_mapping/executor.py

from typing import Any

class FieldMappingExecutor:
    def __init__(self, mapping_name: str, company: str | None = None) -> None: ...

    def push(self, source_doc: Any) -> dict:
        """ERPNext doc -> EasyEcom payload dict."""

    def pull(self, source_payload: dict) -> dict:
        """EasyEcom payload dict -> ERPNext doc field dict."""

    def show_computed_mapping(self) -> dict:
        """Returns the effective mapping including identity defaults."""

    def test_with_sample(self, sample: dict, direction: str) -> dict:
        """Apply ruleset to a sample without persisting; returns output + per-rule trace."""

# ecommerce_super/easyecom/field_mapping/compiler.py
def compile_ruleset(mapping_name: str) -> CompiledRuleset: ...
def invalidate_compiled_cache(mapping_name: str) -> None: ...
```

### 31.4.3 Queue facade (built on frappe.enqueue / RQ)

All async work is enqueued via this facade, which creates the EasyEcom Queue Job tracking row and calls `frappe.enqueue` referencing it. See Section 6.3 for the full lifecycle.

```
# ecommerce_super/easyecom/queue/__init__.py

# Routing: which job_type → which Frappe queue tier → what timeout
QUEUE_FOR_JOB_TYPE: dict[str, str] = {
    # Short queue (low-latency: webhook responses, fast compute)
    "Webhook Process":       "short",
    "SLA Breach Compute":    "short",
    "Configuration Audit Write": "short",
    # Default queue (routine integration work)
    "Item Push":             "default",
    "Customer Push":         "default",
    "Supplier Push":         "default",
    "PO Push":               "default",
    "SO Push":               "default",
    "B2B Invoice Push":      "default",
    "Order Pull":            "default",
    "GRN Pull":              "default",
    "Return Pull":           "default",
    "Field Mapping Compile": "default",
    # Long queue (bulk and scheduled compute)
    "Inventory Pull":        "long",
    "Master Sync Bulk":      "long",
    "Replay Plan Step":      "long",
    "Schema Snapshot Compute": "long",
    "Mapping Coverage Compute": "long",
    "Morning Brief Compute": "long",
}

TIMEOUT_FOR_JOB_TYPE: dict[str, int] = {
    "Webhook Process":       60,    # webhooks must process fast
    "Item Push":             120,
    "Customer Push":         120,
    "PO Push":               120,
    "SO Push":               120,
    "B2B Invoice Push":      300,   # may upload PDF
    "Order Pull":            180,
    "GRN Pull":              180,
    "Return Pull":           180,
    "Inventory Pull":        1500,  # large batch
    "Master Sync Bulk":      3600,  # full master sync
    "Replay Plan Step":      300,
    "Field Mapping Compile": 30,
    "Schema Snapshot Compute": 600,
    "Mapping Coverage Compute": 600,
    "Morning Brief Compute": 600,
    "SLA Breach Compute":    60,
    "Configuration Audit Write": 30,
}

def enqueue_easyecom_job(
    job_type: str,
    company: str,
    *,
    target_doctype: str | None = None,
    target_name: str | None = None,
    payload: dict | None = None,
    correlation_id: str | None = None,
    parent_correlation_id: str | None = None,
    parent_event: str | None = None,
    parent_sync_record: str | None = None,
    parent_replay_plan: str | None = None,
    priority: int = 5,
    max_attempts: int | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Single entry point for enqueuing async EasyEcom work.

    1. Looks up queue_tier and timeout_seconds from routing tables above
    2. Computes idempotency_key per Section 6.1 if not provided
    3. Inserts EasyEcom Queue Job row with state=Queued
    4. Calls frappe.enqueue with job_name=<row.name>; the worker (execute_job)
       reads the row, dispatches to the job_type-specific handler, updates state.

    Returns the EasyEcom Queue Job name.
    """

def cancel_job(job_id: str, reason: str) -> None:
    """Mark Queued/Retrying job as Cancelled. Best-effort: if the job is
    already Running on a worker, the cancellation is recorded but the
    worker may complete the current attempt; subsequent attempts will
    early-exit by checking state."""

def retry_job(job_id: str) -> None:
    """Re-enqueue a Failed/Cancelled job. Re-uses correlation_id so all
    historical logs link to the same operation. Resets attempts to 0
    and clears next_attempt_at."""

# === Per-Company concurrency (Section 6.3.7) ===

@contextmanager
def company_concurrency_semaphore(company: str):
    """Acquire one of Settings.max_concurrent_workers slots.
    Implementation goes through frappe.cache() (Redis-backed via Frappe's pool;
    no direct Redis connections)."""

# === Worker entry point (Section 6.3.6) ===

def execute_job(easyecom_queue_job: str):
    """Called by frappe.enqueue / RQ worker. Updates DocType state,
    dispatches to JOB_TYPE_HANDLERS[job_type], handles retry / failure."""
```

### 31.4.4 Sync facade

```
# ecommerce_super/easyecom/api/sync.py (whitelisted endpoints)

@frappe.whitelist()
def sync_now(
    company: str,
    entity_type: str,
    scope: str = "all",
    *,
    selected_names: list[str] | None = None,
    modified_since: str | None = None,
    item_group: str | None = None,
    direction: str = "push",
    mode: str = "standard",
    dry_run: bool = False,
) -> dict:
    """Returns {'queue_job': <name>, 'expected_count': <int>}."""

@frappe.whitelist()
def force_resync(doctype: str, name: str, company: str) -> dict: ...

@frappe.whitelist()
def mark_already_synced(sync_record: str, ee_id: str) -> None: ...

@frappe.whitelist()
def cancel_sync(sync_record: str, reason: str) -> None: ...
```

### 31.4.5 Replay engine

```
# ecommerce_super/easyecom/operational/replay.py

def create_plan(
    plan_name: str,
    company: str,
    target_doctype: str,
    filter: dict,
    strategy: str,
    *,
    override_values: dict | None = None,
    throttle: str = "standard",
    max_concurrency: int = 4,
) -> str:
    """Returns Replay Plan name."""

def dry_run(plan_name: str) -> dict:
    """Executes simulated run, populates dry_run_results. Returns summary."""

def commit(plan_name: str, commit_reason: str) -> str:
    """Gated on dry-run sign-off. Returns parent Queue Job name."""

def cancel(plan_name: str, reason: str) -> None: ...
```

### 31.4.6 Schema drift detection

```
# ecommerce_super/easyecom/operational/schema_drift.py

def hash_payload_shape(payload: dict | list) -> tuple[str, list[tuple[str, str]]]:
    """Returns (hash, paths_summary)."""

def record_observation(
    payload: dict | list,
    endpoint: str,
    direction: str,
    api_call: str | None = None,
    webhook_event: str | None = None,
) -> str:
    """Records the observation; creates Schema Snapshot if new.
       Returns Schema Snapshot name. Fires drift alert if Jaccard distance > 0.1."""

def jaccard_similarity(paths_a: set, paths_b: set) -> float: ...
```

### 31.4.7 Error translation

```
# ecommerce_super/easyecom/operational/error_translation.py

def translate_error(
    error_text: str,
    response_payload: dict | None = None,
    company: str | None = None,
) -> dict | None:
    """Returns the matched Translation entry as dict (with parameter substitution),
       or None if no match."""

def cluster_untranslated(threshold: float = 0.7) -> list[dict]:
    """Periodic clustering job. Returns clusters with size > 1."""
```

### 31.4.8 Morning Brief generator

```
# ecommerce_super/easyecom/operational/morning_brief.py

def generate_for_company(company: str, snapshot_date: str | None = None) -> str:
    """Returns Morning Brief Snapshot name."""

def deliver_for_company(company: str, snapshot_date: str | None = None) -> None:
    """Sends email + Slack to digest recipients."""

# Scheduler hooks (configured in hooks.py)
def daily_generate_all() -> None: ...
def daily_deliver_all() -> None: ...
```

### 31.4.9 Time travel

```
# ecommerce_super/easyecom/operational/time_travel.py

def point_in_time(
    target_doctype: str,
    target_name: str,
    at_timestamp: datetime,
) -> dict:
    """Returns the configured state of the target as it existed at at_timestamp."""

def diff(state_a: dict, state_b: dict) -> dict:
    """Returns diff structure for diff viewer."""

def causal_chain(sync_record: str, lookback_hours: int = 24) -> list[dict]:
    """Returns relevant Configuration Audit rows for failure investigation."""
```

## 31.5 Error class hierarchy

All exceptions raised by the integration inherit from a common base class. Each subclass carries a stable error_code attribute used by Error Translation matchers.

```
# ecommerce_super/easyecom/exceptions.py

class EasyEcomError(Exception):
    """Base class for all integration errors."""
    error_code: str = "ECS_ERROR"

# ============ API client errors ============
class EasyEcomAPIError(EasyEcomError):
    error_code = "ECS_API_ERROR"
    def __init__(self, message: str, *, status_code: int | None = None,
                 response_body: dict | None = None,
                 endpoint: str | None = None,
                 correlation_id: str | None = None) -> None: ...

class EasyEcomAuthError(EasyEcomAPIError):
    error_code = "ECS_API_AUTH_ERROR"

class EasyEcomRateLimitError(EasyEcomAPIError):
    error_code = "ECS_API_RATE_LIMIT"
    retry_after: int | None

class EasyEcomTimeoutError(EasyEcomAPIError):
    error_code = "ECS_API_TIMEOUT"

class EasyEcomServerError(EasyEcomAPIError):
    """5xx responses."""
    error_code = "ECS_API_SERVER_ERROR"

class EasyEcomValidationError(EasyEcomAPIError):
    """4xx with structured validation problem."""
    error_code = "ECS_API_VALIDATION_ERROR"
    validation_problems: list[dict]

class EasyEcomDuplicateError(EasyEcomAPIError):
    """EE rejected as duplicate (already exists)."""
    error_code = "ECS_API_DUPLICATE"
    existing_id: str | None

# ============ Field Mapping errors ============
class FieldMappingError(EasyEcomError):
    error_code = "ECS_FM_ERROR"

class FieldMappingCompileError(FieldMappingError):
    error_code = "ECS_FM_COMPILE_ERROR"
    rule_index: int | None
    parse_error: str

class FieldMappingRuleError(FieldMappingError):
    error_code = "ECS_FM_RULE_ERROR"
    rule_id: str
    erpnext_path: str | None
    easyecom_path: str | None
    transform: str | None
    source_value: Any | None

class FieldMappingMissingRequiredError(FieldMappingError):
    error_code = "ECS_FM_MISSING_REQUIRED"
    rule_id: str
    field_name: str

class FieldMappingValidationError(FieldMappingError):
    error_code = "ECS_FM_VALIDATION_ERROR"
    rule_id: str
    validate_against: str
    invalid_value: Any

# ============ Sync errors ============
class SyncError(EasyEcomError):
    error_code = "ECS_SYNC_ERROR"

class SyncPreconditionError(SyncError):
    """Source record cannot be synced because precondition unmet (e.g., HSN missing)."""
    error_code = "ECS_SYNC_PRECONDITION"
    precondition: str

class SyncConflictError(SyncError):
    """Bidirectional conflict resolution failed."""
    error_code = "ECS_SYNC_CONFLICT"
    erpnext_value: Any
    easyecom_value: Any
    field: str

class SyncCancelledError(SyncError):
    error_code = "ECS_SYNC_CANCELLED"

# ============ Webhook errors ============
class WebhookError(EasyEcomError):
    error_code = "ECS_WH_ERROR"

class WebhookTokenInvalidError(WebhookError):
    error_code = "ECS_WH_TOKEN_INVALID"

class WebhookIPNotAllowedError(WebhookError):
    error_code = "ECS_WH_IP_NOT_ALLOWED"

class WebhookTooOldError(WebhookError):
    error_code = "ECS_WH_TOO_OLD"

class WebhookDuplicateError(WebhookError):
    """Returned via 200 OK; not raised externally."""
    error_code = "ECS_WH_DUPLICATE"

# ============ Replay errors ============
class ReplayError(EasyEcomError):
    error_code = "ECS_REPLAY_ERROR"

class ReplayDryRunRequiredError(ReplayError):
    error_code = "ECS_REPLAY_DRY_RUN_REQUIRED"

class ReplayApprovalRequiredError(ReplayError):
    error_code = "ECS_REPLAY_APPROVAL_REQUIRED"
    threshold: str  # 'records_count' | 'financial_impact'

# ============ Configuration errors ============
class ConfigurationError(EasyEcomError):
    error_code = "ECS_CONFIG_ERROR"

class CredentialsMissingError(ConfigurationError):
    error_code = "ECS_CONFIG_CREDS_MISSING"
    field: str

class LocationNotMappedError(ConfigurationError):
    error_code = "ECS_CONFIG_LOCATION_NOT_MAPPED"
    location_key: str

# ============ Multi-Company errors ============
class MultiCompanyError(EasyEcomError):
    error_code = "ECS_MC_ERROR"

class CompanyAccessDeniedError(MultiCompanyError):
    error_code = "ECS_MC_ACCESS_DENIED"
    user: str
    company: str

class CompanyContextRequiredError(MultiCompanyError):
    error_code = "ECS_MC_CONTEXT_REQUIRED"

# ============ SLA errors ============
class SLABreachError(EasyEcomError):
    """Not raised; constructed and persisted as SLA Breach record."""
    error_code = "ECS_SLA_BREACH"
```

## 31.6 Fixture inventory

All fixtures shipped with the parent app, in fixtures/ directory. Loaded via `bench install-app ecommerce_super` and updated via `bench migrate`.

| Fixture file | Records | Notes |
| --- | --- | --- |
| role.json | EasyEcom Operator, EasyEcom FDE, EasyEcom Replay Approver, EasyEcom System Manager, EasyEcom Auditor | Five roles per Section 14.3 |
| custom_field.json | All ecs_ prefixed custom fields on ERPNext-core DocTypes (Section 4.2) | Item, Customer, Supplier, Warehouse, PO, PR, SO, SI, Stock Entry, Bank Transaction, Journal Entry |
| property_setter.json | ERPNext core DocType property tweaks (visibility of certain core fields) | Minimal; mostly read-only flag on integration-managed fields |
| workspace.json | EasyEcom Control Panel and EasyEcom Cross-Company Workspaces | Per Section 17.2 |
| dashboard_chart.json | API success rate, API call volume, Sync Record state distribution, etc. | Per Section 17.2.3 and 18.4 |
| number_card.json | All Number Cards from Section 17.2.2 and 18.3 |  |
| report.json | All saved Query Reports per Section 17.5 |  |
| easyecom_field_mapping.json | Initial library per Section 5.11 (16 rulesets) | FDE customisable; fixture is the methodology default |
| easyecom_error_translation.json | Initial library per Section 25.3 (target 50+ entries) | FDE-extensible |
| easyecom_sla_budget.json | Default budgets per Section 21.2 | Per-Company FDE-tunable |
| marketplace.json | Starter seed of common channels (Amazon.in, Flipkart, Meesho, Myntra, Ajio, Nykaa, JioMart, etc.) with default channel_type | Convenience seed only; the authoritative flat channel list is synced from EE at onboarding (Section 4.3 / 8.6). Keyed by marketplace_id |
| accounting_dimension.json | The "Channel" Accounting Dimension (reference doctype Marketplace), per Section 4.4 | Ships the dimension; per-Company Dimension Defaults rows are set at onboarding, default optional (both Mandatory flags off) |
| onboarding_step.json | 11 onboarding steps per Section 17.2.5 |  |
| notification.json | Frappe Notifications for alerts per Section 18 |  |
| email_template.json | Default Email Templates for Critical/Error/Warning alerts | FDE customisable |

## 31.7 Permission model rules (concrete)

Beyond the role assignments in Section 14.3, the integration enforces row-level permissions via Frappe's permission_query_conditions hook and per-DocType has_permission methods.

### 31.7.1 Company-scoped DocTypes

These DocTypes have a `company` Link field. permission_query_conditions adds:

```
def get_permission_query_conditions(user: str | None = None) -> str:
    """Adds WHERE `{doctype}`.company IN ({allowed companies for user}) to every query."""

# Applied to:
# - EasyEcom Account
# - EasyEcom Company Settings
# - EasyEcom Location
# - EasyEcom Sync Record
# - EasyEcom API Call
# - EasyEcom Webhook Event
# - EasyEcom Queue Job
# - EasyEcom Sync Cursor
# - EasyEcom Replay Plan
# - EasyEcom Schema Snapshot (NB: snapshots can be Company-shared; permission still applies)
# - EasyEcom Payload Sample
# - EasyEcom SLA Budget
# - EasyEcom SLA Breach
# - EasyEcom Configuration Audit
# - EasyEcom Morning Brief Snapshot
# - Marketplace Account
# - Marketplace Order Map
# - Integration Discrepancy
# - Source-of-Truth Map
```

### 31.7.2 Globally-readable DocTypes

These DocTypes are not Company-scoped; all roles can read; only specific roles can write:

- Marketplace (the flat channel list) — read by all integration roles; write by System Manager
- EasyEcom Field Mapping (when company_scope is empty) — read by all FDE roles; write by System Manager
- EasyEcom Error Translation (when not Company-scoped) — same

### 31.7.3 Sensitive sections

Within the EasyEcom Account, certain sections are restricted beyond the document-level permission:

- Setup section (credentials), Inbound Webhook Auth section (webhook token), Notifications section (Slack webhook URL): System Manager only — enforced via permlevel field separation in DocType definition
- Other sections (Sync Window, Sync Tuning, GRN Policy, Alerts): Accounts Manager + System Manager

### 31.7.4 Append-only enforcement

EasyEcom Configuration Audit, EasyEcom API Call, EasyEcom Webhook Event are append-only: no role (including System Manager) has UPDATE or DELETE permission. Enforced via has_permission returning False for these actions.

### 31.7.5 Cross-Company actions

Cross-Company Replay Plans and cross-Company reports require System Manager role. Enforced at the @frappe.whitelist() endpoint level.

## 31.8 Frappe hooks registry

The parent app's hooks.py registers the following hooks:

### 31.8.1 doc_events

```
doc_events = {
    "Sales Order": {
        "validate": "ecommerce_super.easyecom.flows.b2b_sales.validate_pre_push",
        "on_submit": "ecommerce_super.easyecom.flows.b2b_sales.on_submit_push",
        "on_cancel": "ecommerce_super.easyecom.flows.b2b_sales.on_cancel_handler",
    },
    "Sales Invoice": {
        "on_submit": "ecommerce_super.easyecom.flows.b2b_sales.on_submit_invoice_push",
        "on_cancel": "ecommerce_super.easyecom.flows.b2b_sales.on_cancel_invoice_handler",
    },
    "Purchase Order": {
        "on_submit": "ecommerce_super.easyecom.flows.buying.on_submit_po_push",
    },
    "Purchase Receipt": {
        "validate": "ecommerce_super.easyecom.flows.buying.validate_pr_from_grn",
    },
    "Stock Entry": {
        "on_submit": "ecommerce_super.easyecom.flows.stock_transfer.on_submit_handler",
    },
    "Item": {
        "on_update": "ecommerce_super.easyecom.flows.master_sync.item.on_update_enqueue_push",
    },
    "Customer": {
        "on_update": "ecommerce_super.easyecom.flows.master_sync.customer.on_update_enqueue_push",
    },
    "Supplier": {
        "on_update": "ecommerce_super.easyecom.flows.master_sync.supplier.on_update_enqueue_push",
    },
    "EasyEcom Account": {
        "validate": "ecommerce_super.easyecom.doctype.easyecom_account.validate_account",
        "on_update": "ecommerce_super.easyecom.operational.time_travel.audit_settings_change",
    },
    "EasyEcom Field Mapping": {
        "validate": "ecommerce_super.easyecom.field_mapping.compiler.validate_on_save",
        "on_update": "ecommerce_super.easyecom.field_mapping.compiler.snapshot_version_and_invalidate_cache",
    },
}
```

### 31.8.2 scheduler_events

Note: there is no per-minute queue-dispatcher tick. Frappe's RQ workers pick jobs up immediately when `frappe.enqueue` is called. The crons below schedule polling (which itself enqueues per-Company work) and periodic compute.

```
scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "ecommerce_super.easyecom.flows.b2c_sales.poll_orders",
        ],
        "*/15 * * * *": [
            "ecommerce_super.easyecom.flows.returns_cancellations.poll_returns",
        ],
        "*/30 * * * *": [
            "ecommerce_super.easyecom.flows.buying.poll_grns",
        ],
        "0 */1 * * *": [  # hourly
            "ecommerce_super.easyecom.flows.master_sync.poll_inventory",
            "ecommerce_super.easyecom.operational.sla_tracking.compute_breaches",
            "ecommerce_super.easyecom.operational.alerts.compute_thresholds",
            # Reclaim Queue Job rows in state=Running with no live RQ job:
            "ecommerce_super.easyecom.queue.workers.reclaim_orphaned_jobs",
        ],
        "0 6 * * *": [  # daily at 06:00 IST
            "ecommerce_super.easyecom.operational.morning_brief.daily_generate_all",
        ],
        "0 8 * * *": [  # daily at 08:00 IST
            "ecommerce_super.easyecom.operational.alerts.send_daily_digest",
        ],
        "0 9 * * *": [  # daily at 09:00 IST
            "ecommerce_super.easyecom.operational.morning_brief.daily_deliver_all",
        ],
        "0 0 * * 1": [  # weekly Monday
            "ecommerce_super.easyecom.operational.alerts.send_weekly_summary",
        ],
        "0 2 * * *": [  # daily at 02:00
            "ecommerce_super.easyecom.operational.schema_drift.compute_coverage_snapshots",
            "ecommerce_super.easyecom.operational.error_translation.cluster_untranslated",
            # Renew any location JWT that has reached 85 days of age (90-day expiry, 5-day margin);
            # renewals are jittered across the window for accounts with many locations
            "ecommerce_super.easyecom.api.auth.renew_aging_jwts",
        ],
    },
}
```

### 31.8.3 permission_query_conditions and has_permission

```
permission_query_conditions = {
    "EasyEcom Company Settings": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Sync Record": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom API Call": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Webhook Event": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Queue Job": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Sync Cursor": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Replay Plan": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom SLA Budget": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom SLA Breach": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Configuration Audit": "ecommerce_super.easyecom.permissions.company_scope",
    "EasyEcom Morning Brief Snapshot": "ecommerce_super.easyecom.permissions.company_scope",
    "Integration Discrepancy": "ecommerce_super.easyecom.permissions.company_scope",
    "Marketplace Account": "ecommerce_super.easyecom.permissions.company_scope",
    "Marketplace Order Map": "ecommerce_super.easyecom.permissions.company_scope",
    "Source-of-Truth Map": "ecommerce_super.easyecom.permissions.company_scope",
}

has_permission = {
    "EasyEcom Configuration Audit": "ecommerce_super.easyecom.permissions.audit_no_modify",
    "EasyEcom API Call": "ecommerce_super.easyecom.permissions.append_only",
    "EasyEcom Webhook Event": "ecommerce_super.easyecom.permissions.append_only",
}
```

### 31.8.4 fixtures

```
fixtures = [
    {"dt": "Role", "filters": [["role_name", "in", [
        "EasyEcom Operator", "EasyEcom FDE", "EasyEcom Replay Approver",
        "EasyEcom System Manager", "EasyEcom Auditor"
    ]]]},
    {"dt": "Custom Field", "filters": [["fieldname", "like", "ecs_%"]]},
    "EasyEcom Field Mapping",
    "EasyEcom Error Translation",
    "EasyEcom SLA Budget",
    "Marketplace",
    "Workspace",
    "Dashboard Chart",
    "Number Card",
    "Report",
    "Onboarding Step",
    "Email Template",
]
```

### 31.8.5 after_install

```
after_install = "ecommerce_super.install.after_install"

# ecommerce_super/install.py
def after_install():
    """First-run setup: ensures default fixtures are loaded, default Workspace is set,
    default User Permission scaffolding is created."""
    ensure_roles_have_permissions()
    set_default_workspace_for_role("EasyEcom FDE", "EasyEcom")
    register_default_email_templates()
    log_installation_audit()
```

## 31.9 Implementation order

Recommended sequence for engineering, mirroring Section 29's phasing but ordered by dependency:

1. Foundation: hooks.py, exceptions.py, EasyEcomClient skeleton, three log DocTypes, EasyEcom Account, EasyEcom Company Settings, EasyEcom Location, queue facade (queue/__init__.py + workers.py + routing.py + concurrency.py — all built on frappe.enqueue)
1. Field Mapping engine: compiler, executor, transformers, exceptions, FDE editing UI
1. Master sync (Item, Customer, Supplier, Warehouse, Tax Category, Channel) — uses Field Mapping engine; first real exercise of the integration
1. Buying flow (PO push, GRN pull, PR creation)
1. Stock transfers
1. B2C sales (manifests, SI creation, Marketplace Order Map)
1. B2B sales (SO push, Stock Reservation Entry mirror, B2B invoice flow)
1. Returns and cancellations
1. Must-have operational surface: Recon-Aware Alerts, Morning Brief, Error Translation library, Operational Workspace shell
1. v0.1-alpha cut at week 32 — internal release
1. Hardening + analytics layer (queryable analytics on logs)
1. Replay tooling
1. SLA tracking + Cross-Company ops
1. Schema drift + Time travel
1. v0.1 final hardening at week 46 — pilot-ready

## 31.10 Summary for Claude Code

When implementing this spec, Claude Code should:

- Treat this section as the schema source of truth; treat earlier sections as the design rationale
- When in doubt about a field name or fieldtype, prefer the convention here over inventing new
- Implement DocTypes in the order specified in 31.9 — dependencies are real
- Always include the indexes specified in field schemas; index choices were made for query patterns the spec depends on
- Always raise the specific exception class from 31.5; do not raise generic Exception
- Always pass correlation_id through the call stack; do not generate new IDs at intermediate layers
- Always call the Field Mapping engine for translation; do not write hardcoded translations even if 'simpler'
- Always call the Error Translation library for failed records; do not bypass it for 'common' errors
- Always include a test for every flow at the unit + integration tier minimum (Section 28.1)
- Always update the Configuration Audit log when modifying configuration; never skip the audit even for 'small' changes

