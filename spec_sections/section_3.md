# Build Packet — Section 3: Authentication & Connection Model

> **This is a build input for Claude Code.** It is self-contained: everything needed to build Section 3 is in this file. Build *only* this section. Do not build master sync, flows, or any later section. The authoritative human spec is `docs/SPEC.md`; this packet is the carved, approved Section 3 plus its build context.

---

## Prerequisites already built

**None — Section 3 is the foundation root.** The only thing that must exist before this section is the environment (Section 0): a Frappe v16 bench, a site with ERPNext + India Compliance installed, the `ecommerce_super` app created and installed, and the GitHub repo connected. Section 3 is the first code of the integration itself; nothing in the integration precedes it.

Because there are no prior built sections, there is no dependency recap to verify for this packet. (Later sections will carry a recap of the foundation they build on, plus a verification step. Section 3 establishes that foundation.)

## What this section establishes for everything after it

These are the primitives every later section will depend on — build them as reusable foundations, not one-off code:

- **The EasyEcomClient** — the single class through which all EasyEcom calls flow. Every later section calls EasyEcom *only* through this client; none of them re-implement auth, headers, retry, or logging.
- **The credential/connection model** — EasyEcom Account (account-level, one credential set), EasyEcom Company Settings (per-Company), EasyEcom Location (per location_key, with primary/operational flags and JWT cache).
- **Foundational-call logging** — the account-scoped API Call logging pattern (company blank, is_foundational=1) that the full logging contract (Section 7) will later generalize.
- **The rate-limit-tier throttle** and **day-85 JWT renewal**.

Build these as clean, importable foundations. Sections 4–13 will lean on them heavily.

---

## Recap verification

Section 3 has no upstream built code to verify against (it is the root). **However**, once you have built Section 3, the *next* packet you receive (Section 4) will include a recap of what Section 3 built, and will ask you to verify that recap against this code. So: build Section 3 cleanly and in line with the spec below, because later sections will be checked against it. If during the build you deviate from the spec below for any reason, note the deviation in your build report so the human can update the spec — do not let code and spec silently diverge.

---

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
| email | Data | EasyEcom account email (must be a user with multi-location access in the primary account) |
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
- `frappe_company` is mandatory when `is_operational` is set, and must be empty when `is_operational` is unset.
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

Permission rules: only users with Accounts Manager or System Manager role can read or modify the EasyEcom Account. The Setup, Inbound Webhook Auth, and Notifications sections of the Account are further restricted (System Manager only) because they contain credentials. Per-Company settings records are readable and editable by the assigned FDE for that Company. Sync workers run as a dedicated EasyEcom Sync user with access scoped per Company via User Permissions.

## 3.6 The EasyEcomClient class

A single Python class encapsulates every interaction with EasyEcom. No code outside this class talks to EasyEcom directly. The client is constructed against the EasyEcom Account (for credentials) and a location_key (for JWT scope). Responsibilities:

- Token acquisition via POST /access/token with the account's email, password, and the target location_key
- Token caching, scoped per location_key (one account credential set; one JWT per location). EasyEcom JWTs are valid for 90 days. A scheduled daily job renews each enabled location's JWT once it reaches 85 days of age (a 5-day safety margin before the 90-day expiry), writing the new token to the location's jwt_token cache. On any 401 the client also re-authenticates immediately as a fallback, so an unexpected early invalidation never blocks a flow
- Two mandatory headers on every authenticated call: `x-api-key: {account api_key}` and `Authorization: Bearer {jwt}`. Missing either is a 401. The x-api-key is sent even on the token-acquisition call
- Automatic re-authentication on HTTP 401 — no caller has to handle auth retry
- Exponential back-off with jitter on 429 (rate limit) and 5xx — initial 1s, doubling, max 60s, max 6 retries before raising EasyEcomTransientError
- Connection-error retries (TCP-level failures) with the same back-off
- Request and response logging to EasyEcom API Call with credentials redacted
- Mandatory request_id header on every outbound call (UUID4) — used for cross-system trace correlation
- Tier-aware rate limiting: the client throttles to the account's rate_limit_tier request-rate and tracks consumption against the tier's daily quota (Section 3.10), so it slows or pauses before EasyEcom returns 429 rather than only reacting to it
- Mandatory location_key on every operational call; the resolved Company is derived from the location, never inferred from globals
```
# Illustrative shape, not full implementation
class EasyEcomClient:
    def __init__(self, account: str, location_key: str): ...
    def get(self, endpoint: str, params: dict) -> dict: ...
    def post(self, endpoint: str, body: dict, idempotency_key: str | None = None) -> dict: ...
    def authenticate(self) -> str: ...  # returns JWT for this location_key
    def _refresh_token_if_needed(self): ...
    def _log(self, request, response, latency_ms): ...

class EasyEcomTransientError(Exception): pass
class EasyEcomAuthError(Exception): pass
class EasyEcomBadRequestError(Exception): pass
class EasyEcomNotFoundError(Exception): pass
```

## 3.7 Credential redaction in logs

EasyEcom API Call entries store the request payload, response body, and headers for audit purposes. Before any value is persisted, a redaction pass removes:

- Any field whose name matches: x_api_key, x-api-key, authorization, password, token, secret, jwt
- Any field whose value matches a Bearer token pattern
- Any field marked sensitive in a redaction config
Redaction is applied to both request and response. The redaction function is centralised — every log write goes through it. Redaction failures are themselves an audit event.

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

This is the build-and-test contract for Section 3. Claude Code builds to it; the FDE team test script (`process/test_scripts/section_3.md`) verifies it on staging. Section 3 is done when all of the following hold:

- **Account config exists and is editable.** An EasyEcom Account record can be created with api_endpoint, x_api_key, email, password, and a mandatory rate_limit_tier (no preset default). Credentials are stored encrypted (not readable in plain text from the desk or the DB).
- **Token acquisition works.** With valid credentials, a Test Connection action acquires a JWT for the primary location via POST /access/token and reports success inline. With invalid credentials it reports a clear failure, not a stack trace.
- **Both headers are sent on every call.** Every outbound request carries `x-api-key` and `Authorization: Bearer {jwt}`. A call with either header removed (test harness) returns 401 and is handled as an auth failure.
- **JWT is cached per location and reused.** A second call against the same location does not re-acquire a token; the cached JWT (90-day validity) is reused. jwt_acquired_at / jwt_expires_at are populated.
- **Day-85 renewal is scheduled.** The renewal job (`renew_aging_jwts`) is registered in scheduler_events and, when a JWT's age crosses 85 days (simulated by back-dating jwt_acquired_at), renews it on the next run.
- **On-401 re-auth works.** If a call returns 401 (simulated by invalidating the cached JWT), the client re-authenticates once and retries transparently; the caller does not see the 401.
- **Locations are discovered and recorded.** A location pull (/getAllLocation) creates EasyEcom Location records. Exactly one can be flagged is_primary; is_operational is independent; frappe_company is required iff is_operational and may repeat across locations (many-to-one). A location with neither flag is inert.
- **Foundational calls are logged account-scoped.** The token and location-discovery calls each write an EasyEcom API Call row with easyecom_account set, company blank, is_foundational = 1, credentials redacted in the stored payload.
- **Rate-limit tier drives the throttle.** With tier = Default, outbound throughput is capped at 5 req/sec and the daily-quota counter increments; with tier = Diamond the cap is 30 req/sec. Changing the tier field changes the effective cap with no code change.
- **429 and 5xx back off and surface.** A simulated 429 triggers back-off and requeue; sustained failure raises the configured alert and the Connection Health status reflects Degraded / Down.
- **Webhook auth is bearer-token.** The webhook receiver accepts a valid token in either `Access-token` or `Authorization: Bearer` header and rejects a missing/invalid token with 401. (Full webhook processing is tested with the flows that use it; this criterion covers only auth.)
- **Connection Health reflects reality.** The dashboard shows last successful auth per location, success rate, and daily-quota consumption, and updates after the above actions.

---

## DocType schemas (authoritative field definitions)

These are the exact field definitions for the three DocTypes this section builds, lifted from the Implementation Reference. Build the DocTypes to match these field names, types, mandatoriness, and defaults. (Field semantics and validation rules are in the section spec above.)

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
email                    Data       Y   User with multi-location access in primary account
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

One record per location_key (Section 3.4). Carries primary/operational flags, Company resolution, warehouse mapping, JWT cache, and per-location pull cursors.

```
location_key             Data       Y   Unique within the account; autoname format ECS-LOC-{key}
location_name            Data       Y
is_primary               Check      Y   Default 0. Exactly one location per account has this set
is_operational           Check      Y   Default 0. Whether operational flows run against this location
is_wms_location          Check      Y   Default 0. WMS plan (PO/GRN/cycle-count/putaway) vs OMS-only Non-WMS. Gates the Section 9 buying/GRN flow
serialization_enabled    Check      Y   Default 0. If set, GRN qty pushed per-serial (Section 9)
frappe_company           Link       N   Company. Set iff is_operational. Nullable. NOT unique (many-to-one)
ee_company_value         Data       N   Company value EE records on this location; reference only, never operational
mapped_warehouse         Link       N   Warehouse (within frappe_company); blank for non-operational locations
ee_company_id            Data       N   EasyEcom internal company ID (as reported by EE)
gstin                    Data       N   Validated against India Compliance (operational locations)
pincode                  Data       N
jwt_token                Long Text  N   Cached JWT for this location_key, encrypted at rest (read-only)
jwt_acquired_at          Datetime   N   (read-only)
jwt_expires_at           Datetime   N   (read-only) 90-day validity; proactive refresh ahead of expiry
enabled                  Check      Y   Default 1 (per-location kill-switch)
last_pull_orders         Datetime   N   Cursor (read-only)
last_pull_returns        Datetime   N   Cursor (read-only)
last_pull_grn            Datetime   N   Cursor (read-only)

# Validation:
#  - exactly one location per account has is_primary = 1
#  - frappe_company required iff is_operational = 1; must be empty otherwise
#  - frappe_company is non-unique by design (many locations may resolve to one Company)
#  - a location with neither flag set is inert (recorded but not synced or transacted)
```

---

## Build instruction

Build **only** Section 3, to the spec above and the acceptance criteria in §3.11.

- Create the DocTypes: EasyEcom Account, EasyEcom Company Settings, EasyEcom Location (per §3.3, §3.4, §3.5 and the schemas in the Implementation Reference of `docs/SPEC.md` — ask for the relevant appendix extract if you need it).
- Build the EasyEcomClient (§3.6) with: token acquisition, both mandatory headers, per-location JWT cache, on-401 re-auth, tier-aware throttle, foundational-call logging with credential redaction.
- Register the day-85 JWT renewal scheduled job (§3.6).
- Build the webhook receiver's bearer-token auth (§3.8) — auth only; full webhook processing comes with the flows.
- Build Connection Health (§3.9) and the rate-limit handling (§3.10).
- Write automated tests covering every bullet in §3.11 (Acceptance criteria).
- Do **not** build master sync, field mapping, the queue, or any flow. If this section seems to need one of those, stop and report — it should not.
- Stop when: all §3.11 acceptance criteria are met, automated tests pass, and `bench migrate` runs clean.
- In your build report, list anything where you deviated from this spec, so the human can reconcile `docs/SPEC.md`.
