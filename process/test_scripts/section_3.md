# Section 3 — Authentication & Connection — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Derived from the Acceptance criteria in `docs/SPEC.md` §3.11. Assume EasyEcom **sandbox** credentials.

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

### Overall result
- [ ] **PASS** — every step passed
- [ ] **FAIL** — issues raised below

### Issues raised (GitHub)
| Step # | Issue link | One-line summary |
| --- | --- | --- |
|  |  |  |

---
**On any failure:** raise a GitHub Issue with the test-failure template, referencing the step number, the Expected cell, what you saw, and a screenshot.
