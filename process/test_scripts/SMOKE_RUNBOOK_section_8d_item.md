# §8d Item — Smoke Test Runbook (UI walkthrough)

Quick "click through it and confirm it behaves" runbook — distinct from the thorough `section_8d_item.md` test script.

> **Status (8d build):** §8d was **live-verified end-to-end against the Harmony EasyEcom sandbox** during build — pull *and* push (CREATE/UPDATE/EAN/Bundle/Lifecycle/batch-sweep), flip, and drift all confirmed against real EE. Harmony is a disposable sandbox, so live writes were acceptable there. **For a real client account, push verification is mock-only** per the standing rule — never live writes to a real client's EE. This runbook is the repeatable procedure for that case: Phase B is read-only; push stays mock/inspect-only.
>
> **⚠️ Never run `bench run-tests` against a live site.** During build it triggered the test-factory cleanup and wiped the live Harmony account, Locations, and Company Settings (Items survived). Use `bench execute` for all live work (fresh process, no test-factory). Cleanup is now prefix-guarded (`test_cleanup_safety.py`), but the rule stands regardless.

Split into two phases so you're not blocked on credentials:

- **Phase A — No credentials (do this NOW).** Confirms the UI/surface exists, buttons are wired, gates hold, the §17 workspace layer renders honestly. Shaking out the *mechanism and surface*, not real data.
- **Phase B — Needs test credentials (do when creds land).** The live read-only pull that proves the foundation against real EasyEcom data.

**Safety rule throughout:** the **"Auto-push Items to EasyEcom on save" checkbox stays OFF**. Pull is read-only and safe; you do NOT want a standing auto-push firing EE writes while you poke around. Push, if exercised at all, is mock-only — never a live write to EasyEcom during testing.

**Two checks matter most** (do these or nothing): **B4** (per-company tax stamps live on a real product) and **B9** (post-flip drift never overwrites ERPNext).

> **Prerequisite:** the §17 workspace layer (Top Strip, worklist row, KPI tiles, charts) ships in the workspace packet. Run A5 only after that packet has landed and been approved; if it hasn't, A5's full layout won't be present yet (only the nav hub + Item count cards).

---

## Phase A — No credentials (do now)

All doable on your local bench with no EE account. Goal: every stage has a working UI trigger, gates hold, the workspace renders honestly, nothing broken at the surface. The 498-green suite proves the logic; this proves the surface.

### A0 — Confirm the triggers exist (don't-guess step)
```bash
grep -rn "Discover.*Products\|Push All Pending\|Push to EasyEcom\|Sync Lifecycle\|Flip.*ERPNext\|auto_push_on_save\|Dismiss Drift" apps/ecommerce_super/ | grep -i "\.js\|whitelist"
```
Expected: Discover Products / Push All Pending / Flip / Auto-push checkbox on **EasyEcom Account**; Push to EasyEcom / Sync Lifecycle on **Item**; Dismiss Drift / Push-to-EE on **EasyEcom Item Map** (Drift rows).

### A1 — Account form buttons
**Do:** Open an **EasyEcom Account** record.
**Confirm:** Discover -> Products, Push -> Push All Pending Items, Flip -> ERPNext-Mastered, and the **Auto-push on save** checkbox (default **unchecked**) all present.

### A1.5 — Workspace sidebar integrity (recent bug class — check it)
**Do:** Look at the left **Workspace Sidebar** under Control Panel.
**Confirm:** Exactly **5 sections**: Setup / Masters / FDE Worklists / Operations / Runtime Logs. Each **FDE Worklist** item is **clickable** and lands on a filtered list with `?status=...` or `?workflow_state=...` in the URL.
**Why:** the Sidebar is a separate v16 DocType that has drifted from the workspace Card Breaks before (mismatched sections; a URL-placement bug made worklist items unclickable). Confirm it matches and the links work.

### A2 — Item form buttons
**Do:** Open any **Item**.
**Confirm:** EasyEcom group shows **Push to EasyEcom** and **Sync Lifecycle to EasyEcom**.

### A3 — Auto-push gate holds (key safety check)
**Do:** With Auto-push **OFF** (default), create/save a few Items.
**Confirm:** No **new Item Push queue jobs** appear in the EasyEcom Queue Job list for those saves, and no EE-call errors. *(Other unrelated Frappe scheduler jobs may exist in the list — fine; what matters is no new Item Push jobs from your saves.)*
**Leave the checkbox OFF.** Turning it on is a go-live action, not a test action.

### A4 — Item Map list view triage
**Do:** Open the **EasyEcom Item Map** list.
**Confirm:** Status colours (Drift=red, Created-Flagged=orange, Mapped=green, FNC=grey, Disabled=dark grey); useful columns; sidebar preset filters (all Drift / all Created-Flagged / all FNC). *(Empty list fine if never pulled.)*

### A5 — Operational workspace surface (§17 layer — run after the workspace packet lands)
**Do:** Open the EasyEcom workspace.
**Confirm, in order:**
- **Top Strip:** Environment (Sandbox/Production, colour-coded) / Connection status / Pause-Resume All Syncs — OR a single "No Account configured" empty-state tile if no Account enabled.
- **FDE Worklist row (6 cards):** Locations - To Map, Channels - Unclassified, Tax Rules - To Configure, Items in Drift, Items Created-Flagged, Items Flagged-Not-Created. All clickable -> filtered lists.
- **KPI Tiles row (3 LIVE):** Open Sync Records (Failed), API Calls (last hour), Queue Job Depth.
- **Pending Tiles row (4 placeholders):** Partial Jobs (24h), Webhook Events (1h), Cursor Lag (Order/GRN), Open Integration Discrepancies. Confirm they render as **labelled, dashed-border paragraph blocks** with "pending §X" text — **NOT** number cards showing 0. (Honesty rule: a "0" meaning "feeder not built" is forbidden — must be visually unmistakable from a live zero.)
- **Charts row:** API Call Volume (7d) line chart, Sync Record Status donut.
**Good:** All layers present; pending tiles unmistakable from live zeros.

### A6 — Flip is gated and one-way (⚠️ throwaway account only)
**Do:** On a **disposable/test** Account, click **Flip -> ERPNext-Mastered**.
**Confirm:** Explicit confirmation required; mode shows ERPNext-mastered; second flip **refuses** (one-way); non-FDE user can't perform it (role-gated).
**⚠️ Can't be undone — throwaway account only.** Skip until you have one if needed (suite covers it).

### A7 — Drift resolution buttons
**Do:** Manually set an Item Map row to Drift.
**Confirm:** Row shows **Dismiss Drift** and **Push ERPNext -> EE**, and **no "Accept EE Value"** button. Dismiss returns it to Mapped without touching the ERPNext item.

### A8 — Single-Account constraint (audit #11)
**Do:** With one Account enabled, try to enable a second EasyEcom Account and save.
**Confirm:** Save **fails with a clear error naming the existing enabled account.** DocType-level guard holds.

**End of Phase A.** Surface, gates, workspace honesty, flip safety, drift actions, single-account guard verified — everything that doesn't need real data.

---

## Phase B — Needs test credentials (live read-only pull)

**Phase B is ORDERED: B0 -> B9, and B9 (flip+drift) MUST be LAST.** The flip is one-way — once you flip you can no longer exercise B1-B8 in onboarding mode. Do all onboarding-phase checks first; flip only at the very end.

Everything here is **read-only on the EE side** (all `GetProductMaster` pulls) — safe live. Push stays mock-only.

### B0 — Connect
**Do:** Set API Endpoint / X-API-Key / Email / Password / Default Location, save. Click **Test Connection**.
**Confirm:** JWT acquired; account in **onboarding** mode (not flipped).
**Stop if Test Connection fails.**

### B1 — First pull + Sync Records
**Do:** Click **Discover -> Products**.
**Confirm:** Products land as Items + map rows; healthy ones Mapped. Workspace counts show real numbers.
**Then open the Sync Record list:** one row per pulled product, **direction=Pull, status=Success**; failed pull -> **status=Failed**. *(Audit #1 — §8d writes Sync Records at every op point.)*

### B2 — Matching (spot-check for WRONG links)
**Confirm:** 2-3 map rows link the genuinely correct product per SKU. New SKUs created fresh; SKUs matching an existing item_code auto-mapped.
**Critical:** nothing *wrongly* linked. A duplicate is fine/fixable; a wrong link is silent corruption.

### B3 — Content gating
**Confirm:** No-HSN -> **Flagged-Not-Created** (grey), **no Item** (no placeholder HSN). Unmapped-tax / dirty-UOM -> **Created-Flagged** (orange), Item **exists**.
**Then:** configure one Created-Flagged item's tax rule, re-pull, confirm clears to Mapped.

### B4 — Per-company tax (THE big one — first live proof of the §8c resolver)
**Do:** With two companies enabled, open a Mapped item's tax table.
**Confirm:** Tax rows for **both** enabled companies coexist, each correct per its Tax Rule Map.
**This is where tax-on-real-products is proven end-to-end. Look closely.**

### B5 — Real-payload dirt survived
**Confirm:** No crash / no silent drops on the known dirt — dirty accounting_unit ("111"/"333"/"PCS"), garbage EANUPC, product_type strings, the "product shelf life" key with a space.

### B6 — Combos -> Bundles
**Confirm:** A combo became a **Product Bundle** with components resolved via their map rows + its own Bundle map row. Unresolved component -> **whole bundle held** (FNC), reason names the missing component. <2 sub-products -> flagged.
**Ordering note:** if a combo arrives in the **same page** before its components' map rows exist, it FNCs as *"sub_product not yet mapped"* — **expected.** Re-pull and it resolves to Mapped once components were created in the first pass. A bundle FNC'd for "components not yet mapped" on first pull is not a failure; re-pull before investigating.

### B7 — Unsupported types held
**Confirm:** variant_parent / child_product / kit / unknown -> **Flagged-Not-Created**, no Item. Only normal + combo create anything.

### B8 — Pagination + resume (if catalog >200)
**Confirm:** Pull walks all pages via the next-page cursor; cursor advances per page; re-run resumes rather than restarting. *(If <200 products, note pagination wasn't live-exercised — suite covers it.)*

### B9 — Flip + drift (⚠️ DO THIS LAST — flip is one-way)
**Only after B1-B8 are clean.** Click **Flip -> ERPNext-Mastered**. Then change a field on a product **on the EE side** and re-pull (now drift-detection).
**Confirm:**
- ERPNext Item **NOT** overwritten. Map row -> **Drift** (red); structured drift table records the differing fields (ERPNext value vs EE value, one row per field).
- The drift run's **Sync Record lands status=Discrepancy, NOT Failed** — divergence is not a sync failure (§7.3 / option B). Check the Sync Record list.
- A new EE product post-flip -> Drift row, no Item created.
- A quiet re-pull (no change) -> stays Mapped, doesn't flap to Drift.
- On a Drift row: **Dismiss** returns it to Mapped (ERPNext untouched); **no "Accept EE Value"** button.
- **Drift exclusion (audit #10):** on a Drift row, add a field (e.g. item_name, reason "intentional ERPNext rename") to the **Excluded Fields** child table; re-pull; confirm that field's change is **NOT** in the drift table and the row no longer flags for it.
**Critical:** ERPNext is **never** overwritten by an EE change post-flip. If it is, that's the one serious failure — report it.

### B10 — API call log hygiene
**Confirm:** Pull calls logged as API Call rows with X-API-Key and Bearer token **redacted**.

---

## What you do NOT do in the smoke

- **Do NOT** turn on Auto-push-on-save.
- **Do NOT** click Push All Pending / Push to EasyEcom / Sync Lifecycle as **live writes** to a real EE account. Those write to EasyEcom. Push is verified mocked (suite) or by inspecting manufactured payloads — never live writes during testing.
- **Do NOT** click the drift "Push ERPNext -> EE" button as a live test — that's a live EE write; mock only.
- No cron smoke (`scheduled_discover_products` at 0 5 * * *) — can't sensibly smoke a cron; suite covers it.

## The two load-bearing checks
1. **B4** — per-company tax stamps live on a real product (§8c resolver's first real exercise).
2. **B9** — post-flip drift never overwrites ERPNext (the SoT guarantee).
Everything else matters; these two are where real damage hides.
