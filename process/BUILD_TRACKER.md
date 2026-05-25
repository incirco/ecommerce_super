# Build Tracker

The frontier. One row per buildable section, one column per stage of the loop (see `process/PROCESS.md`). Update by hand as a section advances. This tracks **sequencing**, not defects — defects live in GitHub Issues.

**Legend:** ☐ not started · 🔶 in progress · ✅ done · — n/a yet

**Current focus:** _8a Location DONE — discovery pull (/getAllLocation), 4-state Workflow fixture (To Map → Mapped but not Live → Live → Skipped), full Source-of-Truth Map, reusable per-record savepoint helper (easyecom/flows/_isolation.py), back-fill, + trigger surface (Discover Locations button, daily scheduler, new-location notification placeholder pending §18). State-aware company/workflow invariant. Built, 281 green, and SMOKE-TESTED LIVE against sandbox (3 real locations → To Map, is_wms from stockHandle, button-triggered, workflow walked, back-fill sane). Pending commit + push. NEXT: 8b Channel (flat Marketplace list pull) — packet MUST carry the workflow-fixture gotchas noted on the 8b row._

---

## Orientation (approve-only — no build; these set framing & principles)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1. Introduction | ✅ | n/a | ✅ 24-May | n/a | n/a | n/a | n/a | n/a |
| 2. Architectural Principles | ✅ | n/a | ✅ 24-May | n/a | n/a | n/a | n/a | n/a |

## Foundation (build first, in this order)

> §3 and §4 are built together as one packet (`foundation_section_3_and_4.md`): §3's client/logging/health depend on §4's log DocTypes, so connection DocTypes → §4 data model → §3 client. §5–§7 follow.

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3+4. Foundation: Connection Model + Data Model | ✅ | ✅ | ✅ 24-May | ✅ 24-May | ✅ 24-May | 🔄 | 🔄 FDE | ☐ |
| 5. Field Mapping engine | ✅ | ✅ | ✅ 24-May | ✅ 24-May | ✅ 24-May | 🔄 | 🔄 FDE | ☐ |
| 6. Idempotency, Replay, Correlation, Queue (completion — most built in foundation) | ✅ | ✅ | ✅ | ✅ | ☐ | ☐ | ☐ | ☐ |
| 7. The Integration Contract (verify-and-carry; not a build) | ✅ verified | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## Integrations (each implements the Section 7 contract)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8. Master Sync (split into 6 dependency-ordered packets below) | — | — | — | — | — | — | — | — |
| 8a. Location (pull + FDE map; resolution substrate) | ✅ | ✅ | ✅ | ✅ smoke | ☐ | ☐ | ☐ | ☐ |
| ↳ 8a refactored to use the Field Mapping engine (EasyEcom-Location-Pull ruleset) instead of a hardcoded mapper — engine = API-change insurance (§8.0 policy). stockHandle→is_wms_location transform now in the ruleset. §5 path validator relaxed to allow space-bearing keys. Re-pull now preserves existing values when EE omits a field. 309 green. | — | — | — | — | — | — | — | — |
| 8b. Channel (flat Marketplace list pull) | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| ↳ 8b packet MUST include a "Workflow-fixture mechanics (learned in 8a)" block: (1) ship each transition twice, once per role — Workflow Transition.allowed is a single Role link, no inheritance; (2) active workflow auto-applies on insert (factories insert in first state + transition, or db.set_value to stamp); (3) test role-cache flush — clear_cache(user) + set_user after granting a custom role; (4) sanitise savepoint names to alphanumeric+underscore (MariaDB rejects dashes). | — | — | — | — | — | — | — | — |
| 8c. Tax Category (mapping; precondition for Item) | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 8d. Item / Product master (first hard master; builds savepoint helper) | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 8e. Customer master (incl. anonymous pseudo-customers) | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 8f. Supplier / Vendor master | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| (Lookups — UOM, Brand, Item Group, Category Map — folded into whichever master first needs them, not a standalone packet) | — | — | — | — | — | — | — | — |
| 9. Buying & Inwarding | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 10. Stock Transfers | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 11. B2B Sales | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 12. B2C / Marketplace Sales | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 13. Returns & Cancellations | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

## Operational surface & rest (later band — build after integrations are stable)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 14. Multi-Company | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 15. Failure Modes & Recovery | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 16. Performance & Scale | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 17. Operational Surface | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 18. Notifications & Alerts | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 19. Replay & Recovery Tooling | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 20. Schema Drift & Coverage | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 21. SLA Budgets & Tracking | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 22. Cross-Company Operations | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 23. Recon-Aware Alerts | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 24. Morning Brief | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 25. Error Translation Library | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 26. Time Travel & Config Audit | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

---

## Note on Section 0 — environment

Before Section 3 can be built, the local environment must exist: Frappe bench (v16), a site with ERPNext + India Compliance installed, the `ecommerce_super` app created via `bench new-app`, and the GitHub repo connected. If that is not yet done, it is the true first task — treat it as Section 0 and complete it before signing off Section 3.
