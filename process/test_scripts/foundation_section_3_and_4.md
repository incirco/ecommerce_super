# Foundation (§3 + §4) — Connection Model + Data Model — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the combined foundation build: §3 (Authentication & Connection) and §4 (Data Model). Derived from the Acceptance criteria in `docs/SPEC.md` §3.11 plus the §4 data-model, §3.5.4 permissions, §3.7 credential, and §4.4 dimension requirements. Assume EasyEcom **sandbox** credentials.

**Build under test:** commit / branch ____________  ·  **Deployed to:** ____________  ·  **Tester:** ____________  ·  **Date:** ____________

### Preconditions
- [ ] `ecommerce_super` app installed on the staging site, migrated clean
- [ ] EasyEcom sandbox credentials available (api_key, email, password) with multi-location access
- [ ] At least two EasyEcom locations exist in the sandbox account (so primary vs operational can be tested)
- [ ] You have System Manager access on the staging site

### Steps — happy path

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| 1 | Create an EasyEcom Account record; fill api_endpoint, x_api_key, email, password; try to save **without** setting rate_limit_tier | Save is blocked — rate_limit_tier is mandatory with no default | ☐ P ☐ F |
| 2 | Set rate_limit_tier = Default; save | Saves. Re-open the record and inspect x_api_key/password fields | ☐ P ☐ F |
| 3 | Confirm credential storage | x_api_key and password are not readable as plain text (masked/encrypted); not exposed in the DB | ☐ P ☐ F |
| 4 | Click Test Connection | Inline success message; a JWT is acquired for the primary location | ☐ P ☐ F |
| 5 | Open the EasyEcom API Call list | A row exists for the token call: easyecom_account set, **company blank**, is_foundational = 1, and the stored payload has credentials **redacted** | ☐ P ☐ F |
| 6 | Run location discovery (the Sync Locations / getAllLocation action) | EasyEcom Location records are created for the sandbox account's locations | ☐ P ☐ F |
| 7 | Inspect the created locations | Exactly one allows is_primary; is_operational is a separate flag; setting is_operational requires frappe_company; two operational locations can point to the **same** Company | ☐ P ☐ F |
| 8 | Make a second authenticated call against the same location (any read) | No new token is acquired — the cached JWT is reused; jwt_acquired_at / jwt_expires_at are populated (≈90 days out) | ☐ P ☐ F |
| 9 | Open Connection Health | Shows last successful auth per location, success rate, and daily-quota consumption against the Default tier | ☐ P ☐ F |

### Steps — negative / edge cases

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| N1 | Enter a wrong password; Test Connection | Clear failure message (not a stack trace); connection_status reflects the failure | ☐ P ☐ F |
| N2 | Invalidate the cached JWT (ask Claude Code for the test hook), then make a call | Client re-authenticates once and the call succeeds; caller never sees the 401 | ☐ P ☐ F |
| N3 | Back-date a location's jwt_acquired_at to 86 days ago; run the renewal job | The JWT is renewed (new jwt_acquired_at); no manual action needed | ☐ P ☐ F |
| N4 | Send a webhook to the receiver with no auth header | Rejected with 401 | ☐ P ☐ F |
| N5 | Send a webhook with a valid token in `Access-token` header, then again in `Authorization: Bearer` | Both accepted | ☐ P ☐ F |
| N6 | With tier = Default, drive calls past 5/sec (test harness) | Throughput is throttled to the tier; sustained 429 surfaces a Degraded/Down status and the configured alert | ☐ P ☐ F |
| N7 | Change rate_limit_tier to Diamond; repeat N6 at up to 30/sec | Cap is now 30/sec with no code change | ☐ P ☐ F |

### Steps — §4 data model (DocTypes exist with correct schema)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| D1 | Open the DocType list; confirm the foundation DocTypes exist | EasyEcom Account, EasyEcom Company Settings, EasyEcom Location, EasyEcom Sync Record, EasyEcom API Call, EasyEcom Webhook Event, EasyEcom Queue Job, EasyEcom Sync Cursor all present | ☐ P ☐ F |
| D2 | Inspect EasyEcom API Call fields | Has easyecom_account (mandatory), company (optional), is_foundational, correlation_id, endpoint, status, latency_ms, redacted request/response; append-only (no edit/delete even as System Manager) | ☐ P ☐ F |
| D3 | Inspect EasyEcom Webhook Event | Append-only; unique on (event_type, ee_event_id, company); has auth_header_used, token_verified, processing_state | ☐ P ☐ F |
| D4 | Inspect EasyEcom Sync Record | Has company (mandatory), entity_doctype/name, direction, status, correlation_id, idempotency_key; unique on (company, entity_doctype, entity_name, direction) | ☐ P ☐ F |
| D5 | Inspect EasyEcom Queue Job | State select includes Queued/Running/Retrying/Success/**Partial**/Failed/Cancelled; has succeeded_count/failed_count, queue_tier | ☐ P ☐ F |
| D6 | Try to edit or delete an existing API Call / Webhook Event / Configuration Audit row as System Manager | Blocked — these are append-only for every role | ☐ P ☐ F |
| D7 | Confirm flow-owned custom fields are **absent** | Sales Invoice / Purchase Receipt / Sales Order / Stock Entry do **not** yet carry ecs_ marketplace/grn/so custom fields — those belong to their flows (§4.2), not this foundation build | ☐ P ☐ F |

### Steps — permissions & roles (§3.5.4, created by code, no manual step)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| P1 | On the fresh migrated site, open Role list | The five custom roles exist as fixtures: EasyEcom Operator, EasyEcom FDE, EasyEcom Replay Approver, EasyEcom System Manager, EasyEcom Auditor — none created by hand | ☐ P ☐ F |
| P2 | As a user with **EasyEcom FDE** but **not** EasyEcom System Manager, open an EasyEcom Account | Can read the Account, but the credential fields (api_key, email, password, webhook_token, Slack fields) are **not visible** (permlevel-restricted) | ☐ P ☐ F |
| P3 | As EasyEcom FDE, open a per-Company settings record for an unassigned Company | Not visible / not editable — Company-scoped via User Permission | ☐ P ☐ F |
| P4 | Confirm no manual permission setup was needed | DocPerms, permlevel restriction, and roles all present on fresh install; only the sync user + per-Company User Permissions remain as onboarding steps | ☐ P ☐ F |

### Steps — credential safety (§3.7, write-only, never readable back)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| C1 | As EasyEcom System Manager, open the Account and attempt to reveal x_api_key (form reveal, if any) | No plaintext shown — masked, set/not-set only; no reveal affordance | ☐ P ☐ F |
| C2 | Attempt to read a credential via API / get_password / a report / an export | No surface returns plaintext for credential fields — set-only, even for System Manager | ☐ P ☐ F |
| C3 | Overwrite (rotate) the api_key with a new value; save | Succeeds — rotation is allowed; the new value is then equally unreadable | ☐ P ☐ F |
| C4 | Inspect an API Call row's stored request/response and a received Webhook Event payload | Credentials and tokens are redacted; email and slack_webhook_url also redacted | ☐ P ☐ F |

### Steps — Channel accounting dimension (§4.4)

| # | Action | Expected result | Pass / Fail |
| --- | --- | --- | --- |
| A1 | Open Accounting Dimension list on the fresh site | A "Channel" dimension exists (fixture), reference doctype Marketplace | ☐ P ☐ F |
| A2 | Inspect its Dimension Defaults | Optional by default — **not** Mandatory For Balance Sheet, **not** Mandatory For P&L (per-Company rows are an onboarding step, may be empty here) | ☐ P ☐ F |
| A3 | Create a manual Stock Reconciliation with an adjustment (P&L line) leaving Channel blank | Saves and submits with no block — confirms the dimension does not force a marketplace onto non-channel transactions | ☐ P ☐ F |

### Overall result
- [ ] **PASS** — every step passed
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number, the Expected cell, what you saw, and a screenshot.
