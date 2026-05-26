# Test Script — §8d Item / Product Master

*Goes in `process/test_scripts/section_8d_item.md`. Same format as `section_8a_location.md` / `section_8b_channel.md` / `section_8c_tax.md`. This is the formal FDE test walkthrough for the Item master.*

> **Status:** §8d was **live-verified end-to-end against the Harmony sandbox** (disposable) during build — pull, push (CREATE/UPDATE/EAN/Bundle/Lifecycle/batch-sweep), flip, drift all confirmed against real EE. The EE contract conventions discovered live are captured in the appendix at the end of this script. **For a real client, push is mock-only** — do not live-write to a client's EE account.

**Prerequisites:** Location (§8a), Channel (§8b), and Tax (§8c) masters are configured for the test account — Item depends on all three. At least two companies enabled on the account (to exercise multi-company tax). Test EasyEcom credentials configured and Test Connection passing.

**A note on what's live vs mocked:** the *pull* side is fully testable against a real account (it's read-only). The *push* side (CreateMasterProduct / Update / ActivateDeactivate) writes to EasyEcom — per the project's hard constraint, **do not fire real push calls against EasyEcom during testing.** Push behaviour is verified by the test suite (mocked) and by inspecting manufactured payloads, not by live writes. Where a step below involves push, it's an inspect-the-payload / inspect-the-map-row check, not a live-write check, unless you are deliberately in a controlled go-live with appropriate authorisation.

---

## Section 1 — Pull foundation (onboarding phase)

### 1.1 Basic pull creates Items and map rows
**Do:** On EasyEcom Account, click **Discover → Products**.
**Confirm:** Normal products from EE appear as ERPNext Items, each with an EasyEcom Item Map row. Healthy ones are **Mapped** (green).
**Good:** Items exist, map rows link them, status Mapped.
**Failure looks like:** No items created (check Test Connection, check the account is in onboarding mode), or items created with no map rows (linkage broken — report).

### 1.2 Exact-SKU matching to an existing Item
**Do:** Before pulling, manually create an ERPNext Item whose `item_code` exactly matches a SKU you know is in the EE catalog. Then Discover → Products.
**Confirm:** The integration **auto-maps to your existing Item** (creates a map row pointing at it) rather than creating a duplicate.
**Good:** One Item, one map row, status Mapped — no duplicate.
**Failure looks like:** A second, duplicate Item created for that SKU (matching didn't fire — report), OR your existing item linked to the *wrong* SKU (a wrong-link — report immediately, this is the serious failure mode).

### 1.3 New SKU creates a fresh Item
**Do:** Pull a SKU that has no matching ERPNext item_code and no existing map row.
**Confirm:** A brand-new Item is created with a new map row.
**Good:** New Item, new map row, status Mapped (or Created-Flagged if tax/UOM needs attention).

### 1.4 Missing HSN → held (Flagged-Not-Created)
**Do:** Pull a product that has no HSN code (or temporarily ensure one in the catalog lacks it).
**Confirm:** The map row is **Flagged-Not-Created** (grey) and **no Item is created.** The flag reason names the missing HSN.
**Good:** No HSN-less Item exists; the product is held visibly.
**Failure looks like:** An Item created with a blank/placeholder HSN (the integration should NOT placeholder HSN — report), or the product silently dropped with no map row (should be visibly held — report).

### 1.5 Dirty UOM / unmapped tax → Created-Flagged
**Do:** Pull a product whose tax rule isn't yet configured in the Tax Rule Map, or whose unit-of-measure is dirty.
**Confirm:** The map row is **Created-Flagged** (orange) — the Item **does** exist (so downstream orders can use it), but it's flagged.
**Good:** Item exists, flagged orange, reason explains (unmapped tax rule / dirty UOM).
**Then:** Configure the missing tax rule in the Tax Rule Map (per the Tax test script), re-pull, and confirm the flag clears to Mapped.

### 1.6 Per-company tax stamping (the §8c resolver going live)
**Do:** With two companies enabled, pull a normal product with a mapped tax rule. Open the resulting Item's tax table.
**Confirm:** The item's tax table carries rows **for each enabled company** — both companies' Item Tax Template rows coexist.
**Good:** Both companies represented; each company's rows correct per its Tax Rule Map.
**Failure looks like:** Only one company's tax stamped (multi-company stamping broken — report), or stale rows from a prior pull left behind (per-company REPLACE should clear the company's old rows before writing — report if you see duplicates).
**This is the first live proof the Tax resolver works on real products — pay attention here.**

### 1.7 Unsupported product types → not created
**Do:** Pull a catalog containing a variant parent, a child variant, or a kit (if the test catalog has them).
**Confirm:** Each is **Flagged-Not-Created** — no Item created.
**Good:** Variant parents / children / kits / unknown types all held, not turned into Items. Only normal products and combos create anything.

### 1.8 Cursor pagination and resume
**Do:** Against a catalog with more than one page (>200 products), run Discover → Products. If you can interrupt it, do; otherwise let it complete and run it again.
**Confirm:** The pull walks all pages (follows the next-page cursor), the cursor on the account advances after each page commits, and a re-run resumes from the cursor rather than restarting from the top.
**Good:** All products pulled across pages; re-run doesn't re-process everything.
**(If the test catalog is <200 products, note that pagination couldn't be exercised live — it's covered by the suite.)**

---

## Section 2 — Combos / Product Bundles

### 2.1 Combo → Product Bundle with resolved components
**Do:** Ensure the component SKUs of a combo are already pulled/mapped (they exist as Items). Then pull a combo product.
**Confirm:** A **Product Bundle** is created (not an Item), its components resolved to the right Items, and the Bundle has its **own** map row (linking the Product Bundle).
**Good:** Bundle exists, components correct, Bundle map row status Mapped.

### 2.2 Combo with an unresolved component → held
**Do:** Pull a combo where at least one component SKU has no map row yet.
**Confirm:** The **whole bundle is Flagged-Not-Created**, with a reason naming the missing component. No half-built Bundle.
**Good:** Bundle held, reason names the component. Pull/create the component, re-pull, confirm the Bundle then builds.

### 2.3 Combo with fewer than 2 sub-products → flagged
**Do:** Pull a combo that has only one sub-product (if the catalog has one).
**Confirm:** Flagged (EasyEcom requires ≥2 sub-products for a combo).

---

## Section 3 — Push (inspect, do NOT fire live writes)

*Reminder: do not make real push calls to EasyEcom during testing. These steps inspect the manufactured payload and the map-row outcome, verified via the suite / console with a mock, not live writes.*

### 3.1 Field manufacturing
**Confirm (via the suite / a mock dry-run):** For an ERPNext item being pushed, the manufactured payload carries the EE-mandatory fields ERPNext doesn't natively have — materialType (default Finished Good), itemType (0 for normal), Brand (fallback "Unbranded"), ModelNumber, Cost (fallback to valuation rate), TaxRate snapped to EE's allowed set {0,3,5,12,18,28}, dimensions.
**Good:** Payload is well-formed with all mandatory fields present.

### 3.2 Missing mandatory → flagged, not pushed
**Confirm:** An item missing a hard-mandatory field (e.g. zero/blank dimensions for a physical product, or no tax rate) is **Flagged-Not-Created with a reason** and produces **zero** EE calls — no broken payload is sent.
**Good:** Flagged, no call made.

### 3.3 product_id writeback
**Confirm (mocked create response):** After a successful create, the EE-returned product_id is written back to the map row and to the item's product_id field; status goes Mapped.
**Good:** Map row carries the returned id, status Mapped.

### 3.4 Batch sweep — which items, and non-blocking
**Do:** Click **Push → Push All Pending Items**.
**Confirm:** It returns **immediately** with a count of items enqueued (it does NOT hang the browser through sequential calls). The candidate set is: stock items, not disabled, with HSN set, not bundle wrappers, not already pushed.
**Good:** Instant response with an enqueued count; the right items selected.

### 3.5 Bundle push — dependency order
**Confirm (via suite / mock):** A bundle whose components haven't been pushed to EE yet (no product_id on a component) is **flagged**, not pushed broken — components must exist EE-side before the combo. The reason names the unpushed component.

---

## Section 4 — Lifecycle, flip, and drift

### 4.1 Lifecycle pull (active:0 → disabled)
**Do:** In the EE catalog, deactivate a product (or pull a catalog containing an inactive one). Pull.
**Confirm:** The corresponding ERPNext Item is set to **disabled** (onboarding phase — pull is authoritative for this).

### 4.2 Lifecycle push (inspect)
**Confirm (via suite / mock):** Disabling an ERPNext item produces an ActivateDeactivateProduct payload with status 0; re-enabling produces status 1. (Inspect, don't fire live.)

### 4.3 The flip
**Do:** On EasyEcom Account, click **Flip → ERPNext-Mastered** and confirm.
**Confirm:** The account's mode changes to ERPNext-mastered. Try to flip again → it **refuses** (one-way). Confirm a non-FDE user can't perform the flip (role-gated).
**Good:** Mode flipped, re-flip refused, role-gated.

### 4.4 Post-flip: changed field → Drift, NOT overwrite
**Do:** After flipping, change a field on a mapped product **on the EasyEcom side** (e.g. rename it). Then click Discover → Products (which is now drift-detection).
**Confirm:** The ERPNext Item is **NOT** changed. The map row goes **Drift** (red), and the structured drift table records the differing field(s) — ERPNext value vs EE value, one row per field.
**Good:** ERPNext untouched, Drift status, drift table populated with the specific differences.
**Failure looks like:** The ERPNext item got overwritten with the EE value (drift detection failed — this is the critical failure; ERPNext must be authoritative post-flip — report).

### 4.5 Post-flip: new EE product → Drift, not created
**Do:** Add a brand-new product on the EE side after flipping. Pull.
**Confirm:** **No** ERPNext Item is created; a Drift map row records the EE-origin new product.

### 4.6 Quiet re-pull doesn't flap
**Do:** With nothing changed on the EE side, run the (drift-detection) pull twice.
**Confirm:** Mapped items **stay Mapped** — they don't flip to Drift on a no-change pull.
**Good:** No spurious drift; status stable across quiet pulls.

### 4.7 Drift resolution actions
**Do:** On a Drift map row, try each action.
**Confirm:**
- **Dismiss Drift** → status returns to Mapped, drift table cleared, ERPNext item untouched.
- **Push ERPNext → EE** → (inspect, don't fire live) re-asserts ERPNext values to EE via the push path.
- There is **no "Accept EE Value"** action (correct — accepting EE would contradict ERPNext-is-SoT).
**Good:** Both actions behave as described; no accept-EE option exists.

### 4.8 Field-level drift exclusion
**Do:** Add a field to an item's drift exclusion list, then change that field on the EE side and re-pull.
**Confirm:** The excluded field does **not** trigger Drift for that item.
**Good:** Excluded field ignored by the detector.

---

## Section 5 — Operational surface / sync records

### 5.1 Sync Records written
**Do:** After any item operation (pull, push, lifecycle, drift), open the Sync Record list.
**Confirm:** Each operation wrote a Sync Record — entity-centric (one per item × direction), with the right status: successful ops → Success; genuine failures → Failed; drift → **Discrepancy** (NOT Failed — drift is divergence, not failure).
**Good:** Items appear in the sync history with correct statuses; a drift run shows Discrepancy, not Failed.
**Failure looks like:** No Sync Records for item operations (the §18 surface would be blind to items — report), or drift recorded as Failed (wrong — it should be Discrepancy — report).

### 5.2 Workspace worklist counts
**Confirm:** The workspace shows live counts — Items in Drift / Created-Flagged / Flagged-Not-Created — and each links into the filtered Item Map list.
**Good:** Counts match the actual map-row states; clicking a count opens the right worklist.

### 5.3 Item Map list view triage
**Confirm:** The Item Map list shows status colours (Drift red, Created-Flagged orange, Mapped green, FNC grey, Disabled dark grey), useful columns, and sidebar filters for the common worklists.
**Good:** You can triage at a glance — the list tells you what needs work without opening rows.

### 5.4 Single-Account constraint (audit #11)
**Do:** With one EasyEcom Account already enabled, try to enable a second one and save.
**Confirm:** The save **fails with a clear error naming the existing enabled account** — the DocType-level guard enforces one enabled Account per deployment (§8.1 assumes one Account).
**Good:** Second enabled account refused at save.

### 5.5 Drift exclusion (audit #10)
**Do:** (Post-flip.) On an item, add a field to its drift **Excluded Fields** child table (e.g. `item_name`, reason "intentional ERPNext rename"). Change that field on the EE side, re-pull.
**Confirm:** The excluded field's change does **not** appear in the drift table and does **not** flag the row for that field.
**Good:** Excluded field ignored by the detector; intentional ERPNext-side differences don't generate recurring drift noise.

---

## What "passing" means

§8d passes when: the pull creates/holds/flags products correctly per type and content; multi-company tax stamps live; combos become Bundles with proper component resolution and held-on-broken behaviour; the flip switches the account to ERPNext-mastered one-way; post-flip drift detects-but-never-overwrites and the resolution actions work; push payloads are well-formed and missing-mandatory flags rather than sends a broken payload (verified mocked, not live); and every operation surfaces as a Sync Record with the right status (drift → Discrepancy). The single most important checks are **1.6 (live per-company tax)** and **4.4 (drift never overwrites ERPNext)** — those are where real damage would hide.

---

## Appendix — EE contract conventions (discovered live on Harmony)

These are real EasyEcom behaviours observed during live bring-up. They're already handled in code; this is reference for the next person and for §8e/§8f.

**Identifier semantics — the cp_id vs product_id trap:**
- `GetProductMaster` returns one record per **(SKU, location)** when `includeLocations=1`; the pull reads from the **primary location only** and dedupes on SKU identity.
- Read-side standalone ids: `product_id` (snake) + `cp_id` (snake). On combo sub-products, ids appear as `product_id` (snake) + `cpId` (**camel!**) + `combo_cp_id` (snake).
- **`UpdateMasterProduct` `productId`** (camelcase int) = the value EE returned as **`cp_id`** on read — NOT the master product_id.
- **`ActivateDeactivateProduct` `product_id`** (snake int) = the same **cp_id** value.
- **`CreateMasterProduct`** returns `{"code":200,"data":{"product_id":<int>}}` — that value semantically **IS the cp_id** for subsequent calls. Writeback stores it to **both** `ee_product_id` and `ee_cp_id`.

**Field-naming inconsistencies:**
- Product name → **`ModelName`** (NOT `ItemName` — that's silently ignored).
- Bundle components → **`subProduct`** (singular) on CREATE, **`subProducts`** (plural) on UPDATE. EE is intentionally inconsistent; the build canonicalizes on plural for snapshots and renames at the CREATE wire boundary only.
- Bundle component qty → **integer only** ("Quantity cannot be a decimal value").
- Weight → **integer grams**; Length/Height/Width → **integer cm**; TaxRate → **integer in {0,3,5,12,18,28}**.
- EANUPC → sometimes literal string `"NA"`; junk must be filtered. EAN push uses a `barcode_type='EAN'` filter.

**Response quirks:**
- Empty page → `{"data":"No Data Found"}` (string, not list) when the cursor walks past the end.
- **HTTP 200 with body `code:400`** → business-level error wrapped in a 2xx envelope; the classifier inspects the body code, not just HTTP status.
- `nextUrl` for `/Products/GetProductMaster` is returned as a **relative path** (handled).
- `"Product Already Exists"` → EE server-side dedup; returns the existing product_id; treat as success.
- `"Same SKU creation is already in progress"` → race protection; another request is in flight; wait and re-check the map row.

## Appendix — UOM-aware dimensions (FDE-editable)

Weight and Length/Height/Width conversions are done via `custom_python` rules in Field Mapping, **editable from the desk** (no code deploy):
- Weight: source `weight_uom` (Kg/Gram/Mg/Lbs/Oz/Tonne) → integer grams. `0.5 kg` → `500`. No UOM → treated as grams (back-compat).
- Dimensions: optional `ecs_dim_uom` (Cm/M/Mm/Inch/Ft) → integer cm. No UOM → passthrough cm. `Inch` → ×2.54.
- The lookup tables live in the rule's `transform_args.expression`. To enable non-cm dimension input for a client, add a custom `ecs_dim_uom` field on Item; the rule already reads `source_doc.get("ecs_dim_uom")`.
- The Field Mapping sandbox allow-list gained `int`, `float`, `round` to support these (closed a compile-vs-runtime mismatch; safe_eval already permitted them at runtime).

## Appendix — FDE handoff items (non-bugs)

1. **§8c tax stamping is pull-direction only by design.** ERPNext-side-created items need an Item Tax row added manually before push — push correctly *refuses* items with no resolvable TaxRate rather than sending a broken payload.
2. **Never run `bench run-tests` against a live site.** It triggers the test-factory cleanup, which wiped the live Harmony account/Locations/Company Settings once during build (Items survived). Cleanup is now prefix-guarded (`test_cleanup_safety.py`), but always use `bench execute` for live work.
3. **Diagnostic helpers** (`_diag.py`) used during smokes are untracked and self-marked safe-to-delete; commit or delete at discretion.
