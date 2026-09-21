# §12 B2C — What's Working, What Isn't

**Last verified:** 2026-09-20
**Scope:** Marketplace / D2C B2C flow (`flows/b2c_sales/`)

---

## TL;DR

The flow is **built, shipped, and running live** (Puresta, MMPL). Code, hooks, DocTypes, and the ops playbook all agree. The only thing lagging is **`SPEC.md` §12 itself** — it still describes a pre-June-2026 model that no longer matches reality. Ten patch notes are sitting in `spec_sections/SPEC_12_patch_notes.md` waiting to be folded into the spec by the methodology team.

**Bottom line: nothing is broken. SPEC is out of date.**

---

## What's Working (the actual flow)

```
Marketplace (Amazon / Flipkart / D2C) creates order
              │
              ▼
Order lands in EasyEcom (out of our scope)
              │
              ▼
Every 5 min: polling.reconcile_all_marketplace_accounts()
   • calls getAllOrders per Marketplace Account
   • per-Account cadence gating (ecs_polling_cadence_minutes)
   • idempotency check on ecs_easyecom_invoice_id
              │
              ▼
invoice_builder.build_si_from_ee_order()
   • Resolves pool Customer:
       - in-state pool  (buyer state = seller state)
       - out-of-state pool (buyer state ≠ seller state)
     → drives GST split correctly
   • Resolves items via EE SKU → ERPNext Item Map
   • Resolves posting_date from EE invoice_date (GST-legal)
   • Resolves currency + conversion_rate (multi-currency support)
   • Resolves gst_category (URP vs Registered Regular)
   • Resolves place_of_supply → drives IC's IGST vs CGST/SGST
   • Uses IC-native taxes_and_charges + per-item item_tax_template
   • INSERTS SI AS DRAFT — no .submit() anywhere in this file
              │
              ├─── upserts EasyEcom Marketplace Order Map
              │        + Shipment child row (per invoice_id)
              │
              └─── writes Sync Record

              [Draft SI now sits waiting]
              │
              ▼
Every hour: pending_manifest_sweeper.sweep_pending_manifest_sis()
   • Queries all Draft SIs whose Shipment.manifest_date IS NULL
   • For each: getAllOrders(invoice_id=...) → EE state
   • Branches:

     ┌─── EE says Manifested + Shipped
     │        └─ (FX gate: if foreign currency + confirmation=0 → HOLD Draft)
     │        └─ Submit SI (GL + stock ledger fire natively)
     │        └─ Set Shipment.manifest_date
     │
     ├─── EE says Manifested + Returned
     │        └─ Submit SI + create paired Credit Note
     │
     ├─── EE says Cancelled + GST category = Unregistered (URP)
     │        └─ Submit SI + immediately cancel it
     │        └─ Cancelled SI auto-excluded from GSTR-1 (IC filters docstatus=1)
     │
     ├─── EE says Cancelled + GST category = Registered Regular
     │        └─ Submit SI + create Credit Note (both appear in GSTR-1)
     │
     └─── Still transient (Confirmed / Manifest Scanned)
              └─ Leave in Draft; try again next tick
```

**Zero Sales Orders. Zero on_submit push. Zero webhook receivers.** The order originates on the marketplace; ERPNext is downstream books.

---

## What's Working — Verified File-by-File

| Piece | File / DocType | Status |
|---|---|---|
| 5-min poll driver | `flows/b2c_sales/polling.py` — `reconcile_all_marketplace_accounts` | ✅ live |
| SI builder (Draft-first) | `flows/b2c_sales/invoice_builder.py` — `build_si_from_ee_order` | ✅ live |
| Hourly sweeper | `flows/b2c_sales/pending_manifest_sweeper.py` — `sweep_pending_manifest_sis` | ✅ live |
| Poll cron | `hooks.py` `*/5 * * * *` | ✅ wired |
| Sweeper cron | `hooks.py` `0 */1 * * *` | ✅ wired |
| Order-grain bridge | `EasyEcom Marketplace Order Map` + `EasyEcom Order Map Shipment` child | ✅ present |
| FX-confirmation gate | Sweeper `_is_awaiting_fx_confirmation` | ✅ blocks submit until `conversion_rate_confirmed=1` |
| Pool Customer resolver | `invoice_builder._resolve_pool_customer` | ✅ dual in-state + out-of-state |
| Playbook | `docs/playbooks/b2c_marketplace_order_map_and_draft_first_flow.md` | ✅ current |

---

## What's Not Working — SPEC.md Drift

`SPEC.md §12` still reflects the pre-June-2026 model. The **code shipped past it**; the spec never caught up. This is the only real risk — a future engineer reading SPEC would build wrong code.

**Six documented patch notes** live in `spec_sections/SPEC_12_patch_notes.md` (from PR #108 closeout, 2026-06-29) that need folding:

| # | Patch | SPEC.md still says | Code actually does |
|---|---|---|---|
| 1 | Marketplace Account scoped into §12 | §8.6.2 "deferred to recon" | Built and used by §12 |
| 2 | Path 2 tax model | §12.5: "ERPNext-derived tax wins" | EE-supplied wins; ERPNext is cross-check |
| 3 | Polling-only, webhooks deferred | §12.3.2: "webhook `manifested` = canonical trigger" | Poll-only; no webhook receiver |
| 4 | No IRN minting from §12 | §12.6: "trigger IRN generation immediately after SI submit" | Zero IRN calls; marketplace owns IRN |
| 5 | Marketplace Order Map (as of Aug 2026 rework) | §12.8: describes an old dropped model | Map + Shipment child fully restored (PR #272) |
| 6 | Two pseudo-customers per Account | §12.2: "singular per-marketplace pseudo-customer" | Dual in-state + out-of-state pools |

**Four additional EE-API-grounding notes** (7–10) also unfolded — `suborders` scanning, marketplace_id guard, address flattening, `total_amount` field.

---

## What's Deferred (Phase 2, on purpose)

Not bugs — planned. Do not attempt in v1.

| Item | Reason |
|---|---|
| Webhooks (`manifested`, `ready_to_dispatch`) | Polling handles all real-world cases; webhook adds infra without measurable gain |
| Auto-Credit-Note for partial marketplace cancellations | §11 mirrors are still conservative; §12 keeps parity |
| ERPNext-initiated cancel | Marketplace-initiated cancel is >99% of B2C cancels; reverse path deferred |
| Per-buyer Customer master (vs pool) | Customer sprawl unsolved; pool works for GST + recon |
| IRN minting from §12 | Marketplace / EE owns IRN — no duplicate risk |

---

## Known Gaps / Open Follow-ups

**None affecting live operations.** Housekeeping:

1. **Fold `SPEC_12_patch_notes.md` (patches 1–10) into SPEC.md §12.** Blocks the §102 B2C backfill build cleanly. Methodology-team work; not a code task.
2. **Delete `drafts/spec_sections/section_12_b2c_packet.draft.md`** — obsolete SO-push model, misconceived, superseded by shipped code. Confused a prior audit (this one).
3. **Issue #97** (B2C SO Push tracking) can probably close — the shipped flow supersedes the original scope. Confirm with a comment on the issue before closing.
4. **§8.6.2 Marketplace Account note** — we amended this on 2026-09-20 (PR #297) to say Marketplace Account is a shared master in `ecommerce_super`. That amendment is consistent with patch note #1 above. Cross-reference when folding patch note #1.

---

## Where the Truth Lives (source-of-truth map)

When statements conflict, resolution order:

1. **Live code** (`flows/b2c_sales/*.py`) — authoritative for actual behavior
2. **Playbook** (`docs/playbooks/b2c_marketplace_order_map_and_draft_first_flow.md`) — authoritative for ops workflow
3. **Patch notes** (`spec_sections/SPEC_12_patch_notes.md`) — authoritative for design decisions past SPEC.md
4. **SPEC.md §12** — authoritative *once patches are folded*; today, stale on the 10 points above

---

## One-Screen Cheat Sheet

**Q: How does a Puresta Amazon order become an ERPNext SI?**
A: Poll every 5 min → invoice_builder makes Draft SI + Map + Shipment → hourly sweeper submits when EE says Manifested+Shipped (or the appropriate cancel/return branch).

**Q: Where's the Sales Order?**
A: There isn't one. Never was. B2C doesn't use SO in this app.

**Q: Where's the webhook receiver?**
A: There isn't one. Polling handles everything today. Webhook is Phase 2.

**Q: What if the SPEC.md says X and the code does Y?**
A: The code wins. SPEC.md §12 is behind by 10 patch notes. Check `spec_sections/SPEC_12_patch_notes.md` first.

**Q: What breaks if I don't fold the patch notes?**
A: Nothing in prod. The next build (§102 backfill) will read SPEC.md and get confused about tax model, pseudo-customer count, and IRN. Fold before then.
