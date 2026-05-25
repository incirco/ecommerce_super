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

*As each further master ships (Channel, Tax, Item, Customer, Supplier), a new Part is appended here, and a matching test script is added.*
