# FDE Primer — Master Sync (Section 8), growing per master

**Who this is for:** an FDE who has already read the foundation primer (`FDE_PRIMER_sections_1_to_7.md`). That one explained *what the product is*, the *nine principles*, and the *foundation* (connection, data model, mapping engine, idempotency, contract). This primer picks up where it left off: the **masters** — the reference data (locations, channels, items, customers, suppliers, tax) that every business flow depends on.

**Read the foundation primer first.** This one assumes you already understand books-vs-operations, idempotency, no-silent-failure, location→company resolution, and the Sync Record. If those phrases aren't familiar, go back.

**This primer grows.** Master Sync (Section 8) is built one master at a time. Each is added here as it ships. Today it covers the model (Part E) and **8a Location** (Part F). Channel, Tax, Item, Customer, Supplier will be appended as Parts G, H, … as they're built.

---

## Part E — The Master Sync model (read once, applies to every master)

### E.1 What a "master" is and why it comes first

A master is reference data a transaction points at: an Item, a Customer, a Supplier, a Warehouse/Location, a Tax Category, a sales Channel. Transactions (orders, receipts, returns — Sections 9+) *reference* masters. So if a master is missing or wrong, the transaction that needs it fails: a Sales Order for an Item that doesn't exist in EasyEcom can't push; a goods-receipt from EasyEcom naming a Vendor that isn't in ERPNext can't become a Purchase Receipt.

That's why masters are built and synced **before** the flows that use them. Getting masters right is most of what makes the later flows "just work."

### E.2 Each master has a direction of truth

The foundation primer's "books vs operations of record" (principle B.1) applies per master, and it isn't the same direction for all of them. Some masters are owned by ERPNext, some by EasyEcom, some are bidirectional with conflict rules. For each master this primer will state plainly: who owns it, what's pulled, what's pushed, and what happens on conflict. Don't assume — read the per-master part.

The one rule that never changes: when the two sides disagree and the integration can't safely reconcile, it raises a visible **Integration Discrepancy** rather than guessing (principle B.7).

### E.3 The build order (why it's not 8.1, 8.2, 8.3…)

The masters are built in **dependency order**, not spec-numbering order. The spec numbers (8.1 Item, 8.2 Customer, …) are stable for cross-referencing, but the build sequence is:

1. **Location (8a)** — the resolution substrate. Nothing resolves to a Company until locations are mapped, so this is first.
2. **Channel (8b)** — the marketplaces the client sells on.
3. **Tax Category (8c)** — must exist before Items, so Items get correct tax.
4. **Item (8d)** — the first hard, bidirectional master.
5. **Customer (8e)**.
6. **Supplier (8f)**.

Lookups (UOM, Brand, Item Group) are folded into whichever master first needs them. You'll test them in this order too.

### E.4 Two patterns you'll see repeatedly

8a established two patterns the later masters reuse, so learn them once:

- **Discovery pull + FDE mapping.** Many masters are *pulled* from EasyEcom (discovery), then *mapped* by you in ERPNext. Discovery never guesses the mapping — it brings the data in and leaves the linking to you.
- **The FDE workflow.** A pulled record that needs your attention sits in an explicit, visible **workflow state** (not a hidden flag). Your worklist is "filter the list to the needs-attention state." You move records through the workflow with action buttons; the state is the source of truth for whether the record is live. (8a's Location workflow is the first; Channel and Item will have their own.)

---

## Part F — 8a: Location discovery & mapping

### F.1 What it is, in one breath

EasyEcom locations (warehouses/stores) are **born in EasyEcom and only ever pulled into ERPNext** — ERPNext never creates or pushes a location. You pull the list (discovery), then map each location to a Frappe **Company** and **Warehouse**, then take it live. That mapping is what makes location→company resolution (principle B.5) work for every downstream flow. **8a is the substrate the whole integration stands on** — until locations are mapped and live, nothing else resolves correctly.

### F.2 Why this is literally your day-one job

When you onboard a client, after Test Connection succeeds, **discovering and mapping locations is the first real configuration you do.** Everything else (channels, items, orders) assumes locations are mapped. So this workflow is not a corner of the system — it's your opening move on every deployment.

### F.3 The Location workflow — the four states

A discovered location moves through a visible workflow. This is the spine of your work:

| State | What it means | Your action |
| --- | --- | --- |
| **To Map** | Just pulled from EasyEcom; no Company/Warehouse assigned yet | This is your worklist. Assign a Company + Warehouse, then **Map** |
| **Mapped but not Live** | You've assigned Company/Warehouse and are reviewing; not yet syncing | Review, then **Go Live** |
| **Live** | Mapped and actively participating in operational flows | — (steady state) |
| **Skipped** | You've decided this location is out of scope (e.g. a master-only primary location, or a non-ERPNext location) | Deliberate; not an error |

The transitions: **Map** (To Map → Mapped but not Live; you must have set a Company first — the button won't let you advance without it), **Go Live** (Mapped but not Live → Live; this is what actually switches the location on), and **Mark Not Relevant** (→ Skipped). There are reverse transitions too (pause a Live location back to Mapped; reconsider a Skipped one) so nothing is a dead end. Only an FDE (or System Manager) can run these transitions.

**Your worklist = filter the EasyEcom Location list to `workflow_state = To Map`.** Those are the locations waiting on you.

### F.4 What discovery fills in for you (and what it deliberately doesn't)

When you run discovery, each location row comes in pre-populated from EasyEcom:
- **Address** — city, state, country, pincode, and the billing/pickup addresses. (State matters — it drives GST place-of-supply.)
- **`is_wms_location`** — set automatically from EasyEcom's `stockHandle` flag (a stock-handling location comes in as a WMS location). You can override it on the form.
- **EE identifiers** — the location key, name, EE company id.

What discovery deliberately leaves **blank for you**:
- **Company and Warehouse** — the mapping is your judgment, never guessed.
- **GSTIN** — EasyEcom doesn't supply it; you set it on operational locations (validated against India Compliance).
- **`is_primary`** — there's no "primary" signal in the EasyEcom data; you designate exactly one primary location.

One thing discovery never stores: EasyEcom returns a per-location **`api_token`** in the payload. It's irrelevant to us today and it's a credential, so it is **dropped on the way in and redacted in the logs** — you will never see it on a Location record. If you ever do, that's a bug.

### F.5 The company ↔ workflow-state rule (so a surprise isn't a bug)

There's a strict rule tying the Company field to the workflow state, and knowing it prevents confusion:
- In **To Map** and **Skipped**, the Company must be **empty** (and moving *to* Skipped auto-clears it for you).
- In **Mapped but not Live** and **Live**, the Company must be **set**.

So if you try to save a To-Map location with a Company filled in, the system rejects it — that's the rule working, not a defect. Company presence and workflow state are kept consistent by design.

### F.6 Re-pull behaviour (steady state)

Discovery runs again on a schedule (and you can trigger it manually). On a re-pull:
- A **new** location (one EasyEcom has that you haven't seen) is created fresh in **To Map** — it appears on your worklist, and you're notified.
- An **existing** location has its EasyEcom-supplied fields refreshed in place; its workflow state is left untouched (a re-pull never silently un-maps or re-advances a location you've already set up).
- An unmapped location is **normal** — it's a visible To Map / Skipped state, never an error. A deployment legitimately has locations you've chosen to skip.

### F.7 The Source-of-Truth Map (configured alongside locations)

While mapping locations you also configure the **Source-of-Truth Map** — one row per warehouse declaring inventory authority: which system owns the stock balance (`inventory_master`), who originates goods receipts (`pr_origination`), who originates stock adjustments (`adjustment_origination`), and whether B2B reservations mirror into ERPNext (`mirror_stock_reservations`). These are business facts about how the client operates — you set them at onboarding. The *code* that acts on them arrives with the buying/stock flows (Sections 9-11); you configure the map now so it's ready when those flows land.

### F.8 How to trigger a pull, and where things land

- **Trigger:** the **Discover Locations** button on the EasyEcom Account form (next to Test Connection). It also runs on a daily schedule.
- **Pulled locations:** the EasyEcom Location list (`/app/easyecom-location`) — new rows in **To Map**, color-coded.
- **The pull's log:** the EasyEcom API Call list (`/app/easyecom-api-call`), filter endpoint `/getAllLocation` — it's a foundational call (company blank, `is_foundational=1`), and the `api_token` shows as `***REDACTED***`.

### F.9 What 8a does NOT do (so you don't look for it)

- It does **not** push locations to EasyEcom (pull only).
- There is **no** custom mapping screen — you map on the standard Frappe Location form using the workflow buttons.
- The Section 9-11 *behaviour* that reads the Source-of-Truth authority fields isn't built yet — the fields are there to configure; the flows that act on them come later.

**Tested by:** `../test_scripts/section_8a_location.md`.

---

## Part G — 8b: Channel (Marketplace) discovery & classification

### G.1 What it is, in one breath

A "channel" is a marketplace or storefront the client sells through — Flipkart, meesho, Amazon, their own website, a B2B portal. EasyEcom knows which channels a client has integrated; 8b **pulls that list, dedupes it into one catalogue, and gives you a workflow to classify each channel** (is it B2C? B2B? an own storefront? a connector to ignore?). Like Location, channels are **pulled, never pushed** — they're born in EasyEcom.

### G.2 The one surprising thing: it polls every location

Here's the part that isn't obvious. EasyEcom's "which channels are live" answer is **per-location** — the API (`/current-channel-status`) tells you the channels for *one location*, authenticated with that location's token. So to build the complete channel catalogue, discovery **sweeps every location** you've discovered (8a) — not just the mapped/Live ones, *all* of them, including To Map and Skipped — because a channel might be live on a location you haven't mapped yet, and the catalogue must be complete.

Two consequences for you:
- **You must run Discover → Locations (8a) before Discover → Channels.** No locations, no channels to sweep. The system will tell you this if you try channels first (a friendly "discover locations first" guard).
- The same channel (say Flipkart) shows up on many locations. Discovery **dedupes by the channel's EasyEcom id** — you get **one** Flipkart row in the catalogue, not one per location. A channel is marked **active** if it's active on *any* location.

### G.3 The classification workflow — the four states

Just like Location, a discovered channel sits in a visible workflow. This is your work:

| State | What it means | Your action |
| --- | --- | --- |
| **Unclassified** | Just pulled; you haven't said what kind of channel it is | This is your worklist — filter the Marketplace list to this state |
| **Classified** | You've set its `channel_type` (B2C / B2B / Quick-Commerce / Own Storefront / POS-Offline / Connector-Ignore); reviewed, not yet active | Review, then Activate |
| **Active** | Classified and live — orders from this channel will run the right flow | — (steady state) |
| **Ignored** | Not a real sales channel (e.g. an accounting/connector artifact like an API integration that isn't a marketplace) | Deliberate; not an error |

Transitions: **Classify** (Unclassified → Classified; blocked until you set `channel_type` — the workflow won't let you advance an unclassified channel), **Activate** (Classified → Active), **Mark Not Relevant** (→ Ignored), plus reverse transitions. Role-gated to FDE, same as Location.

### G.4 Two independent ideas: "active" vs "classified"

Don't conflate these — they're separate axes:
- **`is_active`** comes from EasyEcom — it's *EasyEcom's* integration status (is this channel switched on at EE, on any location). You don't set it; the pull does.
- **The workflow state** is *your* classification lifecycle. You set it.

So a channel can be EE-Active but still **Unclassified** on your side — EasyEcom has it live, but you haven't yet told the system what kind of channel it is. That's normal on first discovery; classifying it is your job.

### G.5 Why classification matters downstream

`channel_type` isn't busywork — it decides how orders from that channel are processed later. A **B2C Marketplace** channel's orders run the B2C sales flow; a **B2B** channel's orders run the B2B flow (different invoicing, tax, settlement). A **Connector-Ignore** channel produces no sales documents at all. So classifying a channel correctly is what makes its orders flow correctly when the order sections (§11/§12) are built. Get this right at onboarding.

### G.6 How to trigger, and what's deferred

- **Trigger:** the **Discover → Channels** action on the EasyEcom Account form (grouped with Discover → Locations). Also runs on a daily schedule.
- **What's deferred:** the **Marketplace Account** (per-channel seller id, GSTIN, settlement template) is *not* part of 8b — it's a settlement/reconciliation concern, built when reconciliation is. And `reporting_parent` (an optional way to group, say, three Amazon channels under one "Amazon" rollup for reporting) is FDE-set and optional — leave it blank unless a client wants the grouping.

**Tested by:** `../test_scripts/section_8b_channel.md`.

---

## Part H — 8c: Tax (EasyEcom tax rule → Item Tax Template mapping)

### H.1 What it is, in one breath

EasyEcom attaches a **tax rule** to every product (a name like "GST", "5", "tax_28%"). 8c is where you tell ERPNext what each of those EasyEcom tax rules **means** — by mapping it to the right ERPNext Item Tax Template(s). Once mapped, when products sync (the Item master, next), each item automatically gets the correct GST set up. This is **configuration you do**, not a pull — EasyEcom has no tax API, so you read the rule from EasyEcom's Tax Master screen and set up the matching mapping here.

### H.2 The one thing to understand: EasyEcom tax rule names are meaningless labels

A rule called "5" might be 5%. A rule called "GST" might be **5% under ₹2500 and 18% above** (a price slab). A rule called "tax_28%" might be 28% — or might not. **The name tells you nothing reliable.** So you never guess from the name. You open EasyEcom's Tax Master (Masters >> Tax Master), see what the rule actually does (its rate, or its price slabs), and reproduce that in the mapping here. The name is just a label that links a product to its rule.

### H.3 The mapping: one document per (rule, company)

You create an **EasyEcom Tax Rule Map** document for each EasyEcom tax rule, **per company** (because Item Tax Templates are company-specific — `GST 18% - OTC` belongs to company OTC). In that document's **Taxes table** you add the ERPNext Item Tax Template row(s):

- **Flat rule** (e.g. "5" = always 5%): one row — pick the `GST 5%` template, leave Min/Max Net Rate blank.
- **Slab rule** (e.g. "GST" = 0–2500 → 5%, 2500+ → 18%): two rows —
  - `GST 5%` template, Min 0, Max 2500
  - `GST 18%` template, Min 2500, Max blank

You enter the bands exactly as EasyEcom's Tax Master shows them. ERPNext then applies the right band automatically at invoice time based on the sale price — **you don't compute anything; ERPNext does the slab math.**

### H.4 Test Resolve — verify before it goes live

This is the master where a typo (wrong template, wrong band) does the most damage, so there's a safety net. On any saved Tax Rule Map, click **Test Resolve** in the toolbar. Enter a sample tax rate (e.g. 0.18) and you'll see exactly what would happen: which template rows would be stamped onto an item, whether that rate reconciles (green) or mismatches your bands (red), and the cess that would apply. **Use it.** It runs the identical logic the real sync uses, so what you see is what products will get. Try a few rates against a slab map to confirm each lands in the right band before you trust it.

### H.5 The workflow, and what happens to unmapped rules

A Tax Rule Map sits in a simple workflow: **To Configure → Configured** (branch **Ignored**). You can't mark it Configured until its Taxes table has at least one row.

Here's the safety behaviour: when products start syncing (the Item master), if a product carries an EasyEcom tax rule you **haven't mapped yet**, the system **auto-creates a To Configure entry for it and alerts you** — it does *not* silently guess a tax. So your worklist is "filter Tax Rule Maps to To Configure" — those are the rules waiting for you to open EasyEcom's Tax Master, see what they do, and fill in the templates. An unmapped tax rule is never a silent wrong-tax; it's a visible task.

### H.6 CESS

Cess (the extra duty on things like tobacco, aerated drinks, some cosmetics) rides on the **product**, not the tax rule — so it's handled per-product automatically, outside this mapping. You don't configure cess in the Tax Rule Map.

**Tested by:** `../test_scripts/section_8c_tax.md`.

---

## Part I — Item / Product Master (§8.1)

*Append this to `process/primers/FDE_PRIMER_section_8_masters.md`, after Part H (Tax). Same FDE-facing voice as Parts F (Location), G (Channel), H (Tax). This is orientation — what the Item master does and how you operate it day to day — not a test script (that's `section_8d_item.md`).*

---

## What the Item master is

Items are the product/SKU master — the thing every order, GRN, and invoice ultimately points at. Of the six masters, Item is the largest, because unlike Location, Channel, and Tax (which are discovered-and-classified, or configured), **Items move in both directions and the direction of truth changes over the life of a client.**

The one idea that makes everything else make sense: **an Item can be born on either side.** A product might already exist in EasyEcom (the catalog team listed it on marketplaces) and need a financial counterpart in ERPNext. Or it might be born in ERPNext (a B2B SKU, a private-label product) and need to be pushed to EasyEcom so it can be sold and fulfilled. The Item master handles both, and it does so through **two phases** with an explicit switch between them.

## The two phases — and the flip

This is the most important thing to understand before you touch the Item buttons.

**Phase 1 — Onboarding (the default).** When you first connect a client, products exist on both sides and need reconciling. In this phase the Item master is *bidirectional and supervised*: you pull EasyEcom's products into ERPNext, you push ERPNext's products to EasyEcom, you review what gets flagged, and you fix mismatches by hand. EasyEcom and ERPNext are both legitimate sources during onboarding because neither side is yet authoritative.

**Phase 2 — ERPNext-mastered (steady state).** Once onboarding is done and the catalogs agree, you **flip** the account to ERPNext-mastered mode. From that point on, **ERPNext is the source of truth for items.** ERPNext→EasyEcom push is the authoritative direction; EasyEcom-side changes are no longer accepted automatically — they surface as **drift** for you to decide on (more below).

**The flip is one-way.** It is a deliberate, role-gated, explicit-confirm action on the EasyEcom Account form ("Flip → ERPNext-Mastered"). Once flipped, the account refuses to flip back. You flip a client *after* you've reconciled their catalog and you're confident ERPNext holds the correct master data. Don't flip early — onboarding's bidirectional supervision is there for a reason.

Why this matters operationally: in onboarding you're doing reconciliation work (pull, review flags, push, fix). After the flip, your job changes to *monitoring* — the steady state mostly runs itself (ERPNext changes push out), and the thing you watch for is drift.

## The Item Map — the spine of the whole thing

Every Item the integration knows about has a row in **EasyEcom Item Map**. This single registry is what links an EasyEcom SKU to its ERPNext counterpart, in either direction, regardless of which side the product was born on. It's the first place you look when you want to know "what is the state of this product?"

A map row links a SKU to **either an ERPNext Item or a Product Bundle** (combos map to Bundles — see below). Each row carries a **status** that tells you exactly where the product stands:

| Status | Colour | Meaning | What you do |
|---|---|---|---|
| **Mapped** | green | Linked and healthy. Item exists, tax stamped, nothing wrong. | Nothing — this is the happy state. |
| **Created-Flagged** | orange | The Item *was created* (downstream orders can use it), but something needs your attention — a dirty unit of measure, or an unmapped tax rule. | Review the flag reason, fix the underlying issue (e.g. map the tax rule), the flag clears on next sync. |
| **Flagged-Not-Created** | grey | The Item was **not** created because a hard requirement is missing — most commonly **no HSN code** (India Compliance won't allow an HSN-less Item), or an unsupported product type. | Fix the cause (add the HSN, etc.), then re-pull. The product stays held until then. |
| **Drift** | red | (Post-flip only.) EasyEcom changed something on a mapped item, but ERPNext is the source of truth now, so the change was **not** accepted. The map row records exactly what differs. | Decide: dismiss it (EE change is wrong/irrelevant) or re-assert ERPNext to EE. See the drift section. |
| **Disabled** | dark grey | The product is inactive. | Usually nothing; reactivate if needed. |

The map is also your worklist. The workspace surfaces counts — *Items in Drift*, *Items Created-Flagged*, *Items Flagged-Not-Created* — and each links into the filtered Item Map list. Those three numbers are your daily Item triage: anything in Drift, Created-Flagged, or Flagged-Not-Created is work waiting for you.

## How matching works (and why it's deliberately simple)

When a product comes in from EasyEcom, the integration decides what ERPNext Item it corresponds to using a deliberately simple rule:

1. **If a map row already exists for that SKU** → use it. Done.
2. **Else, if an ERPNext Item exists whose `item_code` exactly equals the SKU** (byte-for-byte) → auto-map to it and create the map row.
3. **Else** → create a brand-new Item and a map row.

There is **no fuzzy matching, no EAN/barcode matching, no name matching.** This is on purpose. The guiding principle is *"never wrongly link"* over *"never duplicate."* A duplicate Item is visible and easy to merge; a *wrong* link silently corrupts data (orders for product A flow against product B's books). So when in doubt, the integration creates a new Item rather than guessing at a match — and if that produces a duplicate, you fix it by hand. Don't expect the integration to be clever about matching; expect it to be safe.

## Combos → Product Bundles

EasyEcom "combo" products (a bundle of several SKUs sold as one) become **ERPNext Product Bundles**, not Items. The combo's component SKUs are resolved to their own Items via their own map rows. A few things to know:

- The Bundle gets its **own** map row (linking the Product Bundle, not the wrapper Item).
- If any component SKU **can't be resolved** (no map row for it yet), the **whole bundle is held** (Flagged-Not-Created) with a reason naming the missing component — the integration won't build a half-broken bundle. Fix by making sure the components exist/are mapped first, then re-pull.
- EasyEcom requires a combo to have **at least 2** sub-products; a combo with fewer is flagged.
- On push (ERPNext→EE), components must exist on the EasyEcom side **before** the combo that references them (dependency order). A bundle whose components haven't been pushed yet is flagged, not pushed broken.

## What does NOT get created

Some EasyEcom product types are deliberately **not** turned into ERPNext records — they're flagged-not-created and left for you to handle out of band:

- **Variant parents / child variants** — variants shouldn't exist in the EE↔ERPNext item flow; manufacturing/variant structure stays ERPNext-side.
- **Kits / BOMs** — manufacturing stays in ERPNext; we don't pull or push BOMs/kits.
- **Unknown future types** — if EasyEcom introduces a product type we don't recognise, it's flagged rather than guessed at (forward-safe).

Only **normal products** (→ Items) and **combos** (→ Product Bundles) create anything.

## Tax — where §8.5 goes live

This is where the Tax master (Part H) you configured actually gets used. After each Item is created or updated on pull, the integration stamps tax onto it — and because an item is shared across companies but tax is per-company, it does this **for every enabled company on the account**. Each company's tax rows coexist on the shared item's tax table; ERPNext picks the right one per transaction.

What this means for you: if an item comes in **Created-Flagged for an unmapped tax rule**, that's the Tax master telling you a `(tax rule, company)` pair isn't configured yet. Go to the Tax Rule Map, configure it (Part H), and the flag clears. The Item master and the Tax master work together here — a tax flag on an item is usually a tax-map gap, not an item problem.

## Drift — the post-flip safety net

After you flip to ERPNext-mastered, the pull stops *accepting* EasyEcom changes and starts *detecting* them. When a scheduled or manual pull finds that EasyEcom has changed a field on a mapped item (someone edited the name, the weight, the price on the EE side), the integration does **not** overwrite ERPNext — instead it sets the map row to **Drift** and records exactly which fields differ (ERPNext value vs EE value, one row per field, in a structured table on the map row).

Drift is **not an error** — it's a divergence you need to decide about. On an Item Map row in Drift status you get two actions:

- **Dismiss Drift (Mark Reviewed)** — the EE change is wrong or irrelevant; ignore it. Returns the row to Mapped. ERPNext is left untouched.
- **Push ERPNext → EE (Re-assert SoT)** — ERPNext is right; push the ERPNext values back to EasyEcom to correct the EE side.

There is deliberately **no "Accept EE Value" button** — accepting an EasyEcom value would mean letting EE overwrite ERPNext, which contradicts the whole point of being ERPNext-mastered. If EE genuinely holds the correct value, that's a sign the flip was premature or the data needs a manual look, not a routine accept.

Two conveniences worth knowing:
- A **quiet re-pull** (nothing changed) does **not** flap the status — your nightly pulls won't mark everything Drift over nothing.
- If you *intentionally* keep a field different between ERPNext and EE (say you renamed something in ERPNext on purpose and don't want to be re-flagged every night), you can add that field to the item's **drift exclusion list** so the detector skips it for that item.

## The buttons — where to click

Everything is on either the **EasyEcom Account** form or the **Item** form.

On **EasyEcom Account**:
- **Discover → Products** — pulls products from EasyEcom (cursor-paginated, resumable). Pre-flip it creates/updates items; post-flip the *same button* runs drift detection instead (it's mode-aware — you don't pick).
- **Push → Push All Pending Items** — the onboarding sweep; pushes every ERPNext item not yet on EE. Enqueues the work and returns immediately (it doesn't hang your browser on a big catalog).
- **Flip → ERPNext-Mastered** — the one-way phase switch. Deliberate, confirm-gated.
- **Auto-push Items to EasyEcom on save** (checkbox) — default **OFF**. When ON, every item save pushes to EE automatically. Turn this on only in steady state when you want hands-off push; leave it off during onboarding and testing.

On **Item** (and Product Bundle):
- **Push to EasyEcom** — push this one item (bundles auto-dispatch to the bundle path).
- **Sync Lifecycle to EasyEcom** — push this item's enabled/disabled state to EE (activate/deactivate).

On an **Item Map** row in Drift:
- **Dismiss Drift** / **Push ERPNext → EE** (the two drift actions above).

## The scheduler

Once a client is live, you don't have to click Discover Products every day — there's a daily scheduled pull (like Location and Channel have). Pre-flip it pulls deltas; post-flip it runs drift detection. The manual button is there for onboarding and for when you want an immediate pull.

## The typical FDE lifecycle with Items

1. **Connect** the account, configure the masters (Location, Channel, Tax first — Item depends on them).
2. **Discover → Products** — pull the EE catalog in. Review the map: fix Flagged-Not-Created (usually missing HSN), fix Created-Flagged (usually an unmapped tax rule via the Tax master).
3. **Push → Push All Pending Items** — push any ERPNext-only items to EE.
4. Iterate until the map is clean (everything Mapped, nothing flagged).
5. **Flip → ERPNext-Mastered** — once you're confident ERPNext is correct.
6. **Steady state** — ERPNext changes push out (optionally auto-push on save); the daily pull now watches for drift. Your job becomes: handle the occasional Drift row, keep the worklist counts at zero.

## One note for whoever comes next

The individual-push trigger (push-on-item-save) is **built but not auto-wired** — it only fires when the "Auto-push on save" checkbox is on. This is deliberate: it keeps item saves from firing EE traffic until a client is genuinely in controlled steady-state operation. When you take a client live and want hands-off push, that checkbox is the switch.

## Live-verified status and three things to know

§8d was **live-verified end-to-end against the Harmony sandbox** — every pull and push path (CREATE/UPDATE/EAN/Bundle/Lifecycle/batch-sweep), the flip, and drift all confirmed against real EasyEcom. Three operational realities worth carrying forward:

1. **Tax must exist before push.** §8c tax stamping happens on *pull*. An item you create in ERPNext and push to EE must have an Item Tax row first — push will *refuse* an item with no resolvable tax rate rather than send a broken payload. Add the tax row before pushing ERPNext-origin items.
2. **Weight and dimension units are per-client and FDE-editable.** Weight (Kg/Gram/etc → grams) and L/H/W (Cm/Inch/etc → cm) convert via Field Mapping rules you can edit in the desk — no code change. To let a client enter dimensions in inches, add an `ecs_dim_uom` field on Item; the rule already reads it.
3. **Never run `bench run-tests` against a live site.** It triggers test cleanup that once wiped the live Harmony account, Locations, and Company Settings (Items survived). Use `bench execute` for any live work. Cleanup is now prefix-guarded, but the rule holds regardless.

**Tested by:** `../test_scripts/section_8d_item.md`.

---

*As each further master ships (Item, Customer, Supplier), a new Part is appended here, and a matching test script is added.*
