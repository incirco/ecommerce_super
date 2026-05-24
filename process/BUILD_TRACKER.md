# Build Tracker

The frontier. One row per buildable section, one column per stage of the loop (see `process/PROCESS.md`). Update by hand as a section advances. This tracks **sequencing**, not defects — defects live in GitHub Issues.

**Legend:** ☐ not started · 🔶 in progress · ✅ done · — n/a yet

**Current focus:** _§3+§4 combined into one Foundation packet (build-order decided 24-May: connection DocTypes → data model → client). Packet ready at spec_sections/foundation_section_3_and_4.md. Awaiting Approved ✅ + build instruction._

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
| 3+4. Foundation: Connection Model + Data Model | ✅ | ✅ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 5. Field Mapping engine | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 6. Idempotency, Replay, Correlation, Queue | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 7. The Integration Contract | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

## Integrations (each implements the Section 7 contract)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8. Master Sync | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
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
