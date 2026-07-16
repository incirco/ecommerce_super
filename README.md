# ecommerce_super

ERPNext-native integration between ERPNext (v16) and **EasyEcom**, with marketplace settlement reconciliation. FDE-deployed.

## Where to start

- **FDEs (testing / onboarding):** start at **[`process/primers/START_HERE_FDE.md`](process/primers/START_HERE_FDE.md)**. It walks you through the primers, the test scripts, and the build status board, in order.
- **Developers / build context:** read `CLAUDE.md` (working agreement and the single-writer rule), then `SPEC.md` (the full specification). `BRD.md` and `PRD.md` give the business and product context.
- **Custom GSP contract (EE / partner devs, next maintainer):** **[`docs/custom_gsp_contract.md`](docs/custom_gsp_contract.md)** — public reference for our three whitelisted endpoints (`/gettoken`, `/einvoice/update`, `/ewaybill/update`). Request/response schemas, failure modes, curl playbook, change policy. Canonical source of truth.

## Layout

| Path | What's there |
| --- | --- |
| `process/primers/` | FDE primers — understand the product and what's built |
| `process/test_scripts/` | How-to-test guide + per-section test checklists |
| `process/BUILD_TRACKER.md` | Live status board: what's built, tested, next |
| `spec_sections/` | The frozen build packets, one per section |
| `docs/` | The full spec as a Word document + playbooks |
| `SPEC.md` | The canonical specification (Markdown source of the docx) |
| `easyecom/` | The application code |

The spec and packets are the source of truth; they are written through a single controlled path (see `CLAUDE.md`). Please don't edit them ad hoc.
