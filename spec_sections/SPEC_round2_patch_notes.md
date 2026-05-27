# Cross-cutting — Round 2 hardening (post-§8 closeout)

*Apply to SPEC.md across multiple sections. Single-writer rule: USER edits SPEC.md; this is the change list. Round-2 = hardening shipped after the §8 masters closeout, all already on `main` (commits `fb5465d` … `3c33c58`). Per-master items are folded into the per-section patch notes (`SPEC_8d/8e/8f_patch_notes.md`); this file holds the cross-cutting items.*

## §3.3.x — Operational levers: Go Live and Pause auto-push (NEW, commit `3c33c58`)

The three `auto_push_*_on_save` toggles on EasyEcom Account (Items / Customers / Suppliers, one per master) ship defaulted **OFF** for safety — a fresh deployment with credentials wired would otherwise push every existing entity the first time anyone saved one. Steady-state operation needs them ON. The transition is now a single-action ceremony, not three separate checkbox flips:

- **`easyecom.api.auto_push_controls.go_live_enable_auto_push(confirm=true)`** — whitelist endpoint, role-gated (FDE / System Manager / EasyEcom System Manager; Operator role refused). Defaults to enabling all three; the JS dialog lets the FDE deselect a master if a deployment is staging only some. Records an audit Comment on the Account doc capturing user, timestamp, transition details, and which masters were enabled.
- **`easyecom.api.auto_push_controls.pause_all_auto_push(reason, confirm=true)`** — whitelist endpoint, same role-gating. Operational kill-switch (planned maintenance, EE outage, freeze window). Reversible via the go-live endpoint. Audit Comment captures the FDE-supplied reason.

Both endpoints require `confirm=true`. Both write a Comment on the Account doc; the auto-push state is never silently mutated. Sync Records are not written (control-plane action, not entity-sync). Per-entity-Map auto-push remains available via the per-row checkbox; these endpoints are the account-wide convenience layer, not a replacement.

## §3.5.x — Threshold-validation refactor (NEW, commit shipped with `3c33c58` batch)

`EasyEcom Company Settings` carries three threshold fields: `allow_over_receipt_pct`, `allow_under_receipt_pct`, `tax_variance_tolerance_pct` (and future thresholds will follow the same pattern). Validation is now centralised:

- `_to_float()` helper coerces string/int/float input uniformly (previously inconsistent behaviour on string inputs vs numeric).
- `_validate_thresholds()` rewrite enforces 0–100 range with consistent error messages across all threshold fields.

No behaviour change in valid inputs. Previously-inconsistent error behaviour on invalid inputs is fixed.

## §7.7 / §14.2 — Foundational-call company-strip hook (NEW, commit `fb5465d`)

The §7.7 invariant "Foundational API Calls (token / location / test) must leave Company blank" was being violated by Frappe's default-fill on multi-Company sites. Root cause: Frappe v15/v16 auto-populates empty Link-to-Company fields from `frappe.defaults.get_user_default("Company")` during default-resolution, which runs **before** `validate`. Code in `auth.acquire_jwt()` / `client.log_api_call()` correctly passes `company=None`, but Frappe re-injects the user's default Company before validate runs, tripping the invariant. Single-Company dev sites with no user-default never tripped it; multi-Company sites tripped on every Test Connection click.

Fix: new `before_insert` hook on `EasyEcom API Call` that strips `company` when `is_foundational=1`. `before_insert` runs **after** Frappe's default-fill but **before** validate, closing the window. Regression guard: `test_token_call_survives_user_default_company`.

## §3.3.x — Discovery (Products / Customers / Suppliers) is async-by-default (NEW, commit `9280d58`)

Cross-cutting policy across §8.1 / §8.2 / §8.3 Discover desk actions: every Discover endpoint enqueues into the `long` queue (3600s timeout) via `frappe.enqueue` and returns immediately with the RQ job_id. The synchronous path tripped Frappe's 120s desk-whitelist budget on real-client catalogues (>2000 products, customers, or suppliers); the server pull continued in the worker but the browser disconnected, surfacing a misleading "(network or permission)" error to the FDE. Async-by-default is now the only pathway; the cursor + Account high-water fields advance page-by-page as the worker processes records. Progress visible via Account-form refresh + the entity Map list. Per-master detail in `SPEC_8d/8e/8f_patch_notes.md`.

## §3.3.x — Top-bar dropdowns include all three masters (NEW, commit `6d97179`)

Stage 6 oversight in §8e/§8f: the Account form's top-bar "Discover" and "Push" dropdowns only carried §8a/§8b/§8d entries; §8e Customer and §8f Supplier shipped their section-level Button fields but not their top-bar entries, forcing the FDE to scroll deep into the form. Now in the **Discover** dropdown: Locations / Channels / Products / Customers / Suppliers / States-Countries (shared §8e/§8f foundational refresh). In the **Push All Pending** dropdown (renamed from "Push" for clarity): Items / Customers / Suppliers. Each top-dropdown button delegates to the same in-section action handler via `frm.events.X_action(frm)` — same confirm dialog, same async enqueue, no behavioural divergence.

## Net effect on SPEC.md

Five subsections each gain one paragraph (§3.3.x ops levers, §3.3.x discover-async, §3.3.x top-bar dropdowns, §3.5.x thresholds, §7.7 company-strip). No new sections. No new DocTypes (changes are to existing DocTypes). No cross-reference renumbering. The encryption-guard hook (commit `a70a30b` #3) is folded inline into SPEC.md §3.7.2 (already applied); the per-master items (re-evaluate / Mark Mapped on Item, dup-name retry on Customer & Supplier, image fields on Item) are folded into their per-section patch notes.
