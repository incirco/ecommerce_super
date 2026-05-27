# §8.1 Item — spec amendments from live Harmony bring-up

*Apply these to SPEC.md §8.1. They reconcile the spec against what was confirmed/discovered on the live sandbox. Single-writer rule applies — the USER edits SPEC.md; this is the change list.*

## §8.1.x — EE identifier semantics (NEW subsection, add near the push contract)

EasyEcom's product identifiers are inconsistent across read and write paths. The map and push code handle this; the spec must state it so future flows don't re-trip:

- `GetProductMaster` (read) returns `product_id` and `cp_id` (both snake_case) on standalone products. On combo sub-products: `product_id` (snake), `cpId` (camelCase), `combo_cp_id` (snake).
- `UpdateMasterProduct` keys on `productId` (camelCase int) whose value is the **cp_id** from read — not the master product_id.
- `ActivateDeactivateProduct` keys on `product_id` (snake int) whose value is also the **cp_id**.
- `CreateMasterProduct` returns `data.product_id`, which semantically **is the cp_id** for subsequent update/activate calls. The Item Map stores it to **both** `ee_product_id` and `ee_cp_id`.

**Rule of thumb:** the value used to address a product for update/lifecycle is the **cp_id**, regardless of which field name a given endpoint wraps it in.

## §8.1.x — EE field-naming and type contract (NEW)

- Product name maps to EE **`ModelName`**, not `ItemName` (which EE silently ignores).
- Bundle components: **`subProduct`** (singular) on CREATE, **`subProducts`** (plural) on UPDATE. Build canonicalizes on plural; renames at the CREATE wire boundary.
- Bundle component quantity: **integer only**.
- Weight: **integer grams**. Length/Height/Width: **integer cm**. TaxRate: **integer in {0,3,5,12,18,28}**.
- EANUPC: may be literal `"NA"`; junk filtered. EAN push filtered to `barcode_type='EAN'`.

## §8.1.x — EE response handling (NEW)

- Empty page returns `{"data":"No Data Found"}` (string, not list).
- **HTTP 200 with body `code` ≥ 400 is a failure** — classifier inspects body code, not just HTTP status.
- `nextUrl` for GetProductMaster is a relative path.
- `"Product Already Exists"` → EE-side dedup; existing product_id returned; treat as success.
- `"Same SKU creation is already in progress"` → race protection; wait and re-check the map row.

## §8.1.5 (push) — amendments

- Pull reads from the **primary location only** when `includeLocations=1` returns per-(SKU,location) records; dedupe on SKU identity.
- **UpdateMasterProduct sends a sparse payload** (productId + changed fields), not the full record. First push of a record sends full; subsequent sends sparse against a stored snapshot (`ecs_last_pushed_payload`).
- **§8c tax is pull-direction only.** ERPNext-origin items require a manually-added Item Tax row before push; push refuses items with no resolvable TaxRate (does not fabricate one).

## §8.1.4 (combos/bundles) — amendment

- Combos validate on **total component qty ≥ 2** (not distinct-component-count ≥ 2) — this admits multi-pack combos (1 component × N qty). Confirmed live (4 combos + 1 multi-pack on Harmony).
- `child_product` type **is creatable as a standalone Item** (revised from the earlier "variant/child → always FNC" position; confirmed live). Variant *parents* and kits/BOMs remain FNC.

## §8.1.x — UOM-aware dimension conversion (NEW, ties to §8.0 engine)

Weight and L/H/W conversions are `custom_python` Field Mapping rules, FDE-editable in the desk:
- Weight UOM table: Kg/Gram/Mg/Lbs/Oz/Tonne → grams; no UOM → grams (back-compat).
- Dimension UOM table: Cm/M/Mm/Inch/Ft → cm via optional `ecs_dim_uom`; no UOM → passthrough cm.
- Engine change: sandbox `ALLOWED_NAMES` gained `int`, `float`, `round` (closed a compile-vs-runtime gap).

## §8.1.8 (drift) — confirmation (no change, just validated live)

Flip → drift → dismiss confirmed live: drift detected, ERPNext preserved (not overwritten), dismiss returns row to Mapped with drift_fields cleared. Drift Sync Record status = **Discrepancy** (not Failed). No "Accept EE Value" action.

## Cross-cutting — operational safety note (add to §17 or §7.7 ops)

**Never run `bench run-tests` against a live site.** The test-factory cleanup wiped a live account's EE config once during build. Cleanup is now restricted to explicit test-name prefixes (regression: `test_cleanup_safety.py`). Live work uses `bench execute` only.

## §8.1.x — Discover-Products async-by-default (NEW, commit `9280d58`)
Discover Products enqueues into the `long` queue (3600s timeout) via `frappe.enqueue` and returns immediately with the RQ job_id. The synchronous path tripped Frappe's 120s desk-whitelist budget on real-client catalogues (>2000 products); the server pull continued in the worker but the browser disconnected, surfacing a misleading "(network or permission)" error. Async-by-default is now the only pathway. Progress visible via Account-form refresh + Item Map list. Same fix applied to Discover-Customers (§8.2.x) and Discover-Suppliers (§8.3.x).

## §8.1.9 — Per-row FDE actions on Item Map (NEW, commit `4108048`)
- **Re-evaluate from EE** (`re_evaluate_one_product` whitelist; depends_on `status==Created-Flagged`). Walks GetProductMaster (≤200 pages) for the row's `ee_sku`, runs standard `process_one_product` on the match. Use case: FDE fixed the source of a flag (set `tax_rule_name` in EE, corrected UOM) and wants this single row re-evaluated NOW. Cleanly surfaces "SKU not found in EE" if the product was deleted upstream.
- **Mark Mapped (override flags)** (`mark_mapped_override` whitelist; FDE/SM only, confirm-required, audit Comment). Flips Created-Flagged → Mapped without fixing the source. Escape hatch for benign flags. Per-row only, never bulk.

## §8.1.x — Product images (NEW, commit `c79eaa5`, Option A URL-only)
EE `GetProductMaster` returns image URLs we previously dropped. Now: `product_image_url` → native `Item.image` (URL string; Attach Image accepts URLs and renders inline in list/SI/portal); `additional_images[]` → `Item.ecs_additional_image_urls` (Long Text JSON array, new custom field via patch `v0_1.add_ecs_item_image_fields` in `post_model_sync`, idempotent). URLs only — no download, no Frappe File records. Helper `_populate_image_fields` runs inline during the pull.
