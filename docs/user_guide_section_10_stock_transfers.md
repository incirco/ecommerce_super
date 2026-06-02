# Stock Transfers — User Guide

A guide for the ERPNext user who needs to move stock between warehouses. Plain language. Four scenarios. Textual step-by-step flow for each.

If you are an FDE or technical integrator, read `FDE_PRIMER_section_10_stock_transfer.md` instead — this guide deliberately leaves out the technical machinery.

---

## How the flow tables work

Each scenario shows a step-by-step table. Read top to bottom. The middle column tells you who acts at that step:

| Tag | Meaning |
| --- | --- |
| **YOU** | Your action (the ERP user) |
| **SYSTEM** | Integration automation (the §10 substrate) |
| **EASYECOM** | Happens in EasyEcom |
| **DECIDE** | Decision point (a branch) |
| **DONE** | Transfer complete |

---

## What this covers

You have stock in one warehouse. You want it in another. The integration helps when at least one of the warehouses is connected to EasyEcom. This guide explains:

1. The four scenarios you might be in.
2. What you do, and what the system does automatically.
3. What documents get created (and who submits them).
4. What can go wrong and how you'll know.

**One rule above all others:** the integration creates *Draft* documents. *You* submit them. The system never auto-submits a Sales Invoice, a Purchase Invoice, or a Debit Note on your behalf. This is deliberate — financial documents are your decision, not the integration's.

---

## The four scenarios at a glance

The two questions that determine your scenario:

- **Is the source warehouse connected to EasyEcom?**
- **Is the destination warehouse connected to EasyEcom?**

The four combinations:

| Source | Destination | What you call it | What happens |
| --- | --- | --- | --- |
| Not EE | Not EE | Plain stock movement | Pure ERPNext. The integration is silent. |
| EE | EE | Internal warehouse transfer | Integration pushes the transfer to EE. EE manages dispatch and receipt. |
| Not EE | EE | Inward to EE warehouse | Integration creates a purchase order on the EE side so EE can receive goods. |
| EE | Not EE | Dispatch from EE to outside | Integration tells EE about a B2B order; the receiving side is purely your ERPNext task. |

The next four sections cover each in detail.

---

## Scenario 1: Plain stock movement (neither warehouse is EE-connected)

You're moving stock between two warehouses, neither of which is connected to EasyEcom.

### Flow

| Step | Who | What |
| --- | --- | --- |
| 1 | YOU | Create a Delivery Note in ERPNext from source to destination |
| 2 | YOU | Submit the Delivery Note |
| 3 | SYSTEM | Stock moves source → destination in ERPNext (standard ERPNext mechanics) |
| 4 | EASYECOM | (nothing happens — neither warehouse is in EasyEcom) *The integration is silent. No Transfer Map, no Sync Record.* |
| 5 | YOU | When goods physically arrive at destination, record the receipt yourself (Stock Entry or similar) |
| 6 | DONE | Transfer complete — pure ERPNext flow |

**Who submits what:** you submit the DN. You record the receipt. That's it.

---

## Scenario 2: Internal warehouse transfer (both warehouses are EE-connected)

The most common §10 scenario. Both source and destination are EE-managed warehouses. Goods will travel between them.

Two cases inside this scenario:
- **Same GSTIN** (intra-Company, or same legal entity even across Companies): pure stock movement, no GST consequence.
- **Different GSTIN** (inter-Company across legal entities, or inter-state in a multi-state Company): legal supply, GST applies.

### Scenario 2A — Same GSTIN

No Sales Invoice or Purchase Invoice needed — same legal entity, no inter-Company GST consequences. Just stock movement, with EasyEcom managing the physical flow.

#### Flow

| Step | Who | What |
| --- | --- | --- |
| 1 | YOU | Create an Internal-Customer Delivery Note (source EE warehouse → destination EE warehouse) |
| 2 | YOU | Submit the DN |
| 3 | SYSTEM | Creates a Transfer Map record for tracking |
| 4 | SYSTEM | Pushes a Stock Transfer Order (STN) to EasyEcom |
| 5 | SYSTEM | Stock parks in your Goods-in-Transit (GIT) warehouse |
| 6 | EASYECOM | Receives goods at destination warehouse (mark GRN-Complete in EasyEcom) |
| 7 | SYSTEM | Auto-creates and submits Internal Purchase Receipt (IPR). Stock moves GIT → destination warehouse. |
| 8 | DONE | Transfer complete — Transfer Map status: Fully-Received |

**What you do after:** nothing. The same-GSTIN flow needs no further action from you. The transfer is complete.

### Scenario 2B — Different GSTIN

Same as 2A, but the system now also produces GST documents because the transfer crosses a GSTIN boundary. This is where things get more involved.

#### Phase 1 — At DN submit

| Step | Who | What |
| --- | --- | --- |
| 1 | YOU | Create an Internal-Customer DN (source EE → destination EE, different GSTINs) |
| 2 | YOU | Submit the DN |
| 3 | SYSTEM | Creates a Transfer Map record |
| 4 | SYSTEM | Auto-drafts a Sales Invoice (full dispatched quantity) — stays in DRAFT. *You'll submit this when ready — see Phase 2.* |
| 5 | SYSTEM | Pushes Stock Transfer Order (STN) to EasyEcom |
| 6 | SYSTEM | Stock parks in your Goods-in-Transit (GIT) warehouse |

#### Phase 2 — When EasyEcom reports receipt

This phase depends on whether you've submitted the SI yet.

**Case A — You have already submitted the SI:**

| Step | Who | What |
| --- | --- | --- |
| 7 | EASYECOM | Receives goods at destination (mark GRN-Complete in EasyEcom) |
| 8 | SYSTEM | Auto-submits the Internal Purchase Receipt (IPR). Stock moves GIT → destination warehouse. |
| 9 | SYSTEM | Auto-drafts an Internal Purchase Invoice (IPI) sized to the SI's full dispatched quantity (for full Input Tax Credit) |
| 10 | DECIDE | Did EasyEcom receive fewer units than dispatched? |
| 11 | SYSTEM | If YES: auto-drafts a Debit Note (gap-sized lines) to reverse proportional ITC |

**Case B — You haven't submitted the SI yet:**

| Step | Who | What |
| --- | --- | --- |
| 7 | EASYECOM | Receives goods at destination (mark GRN-Complete in EasyEcom) |
| 8 | SYSTEM | Creates the IPR but holds it in DRAFT (cannot auto-submit until SI is submitted). *ToDo appears on the IPR for you.* |
| 9 | YOU | Find the drafted SI → review it → submit it (when ready) |
| 10 | SYSTEM | Automatically retries: submits the held IPR. Stock moves GIT → destination. |
| 11 | SYSTEM | Auto-drafts the IPI and (if applicable) Debit Note — same as Case A from step 9 onwards |

#### Phase 3 — Closing out the GST documents

| Step | Who | What |
| --- | --- | --- |
| 12 | YOU | Find the auto-drafted IPI → review it → submit it |
| 13 | YOU | If a Debit Note was drafted: review it → submit it (this acknowledges the loss and reverses the proportional ITC) |
| 14 | DONE | Transfer complete — Transfer Map status: Fully-Received (or DN-Submitted-Locked if a DN was submitted) |

**What you do after, in order:** submit SI → submit IPI → submit DN (if a gap exists). Always in that order. The integration handles the rest.

#### Multi-GRN: when goods arrive in batches

Sometimes EasyEcom records receipt in multiple batches over several days (you dispatched 100; EE records 60 on day 1, 40 on day 3).

What the integration does:
- A separate IPR per EE receipt event.
- The Debit Note draft adjusts **automatically** each time more goods are received. If a draft DN of "missing 40 units" was created after the first batch, when the second batch arrives and closes the gap, the draft DN is auto-cancelled. An audit Comment is added to the Transfer Map recording what happened.
- If a gap remains after the final receipt, the draft DN sits there waiting for you to review and submit.

If you submit the draft Debit Note while a gap still exists, you're accepting that the missing units are genuinely lost. **If goods then arrive late (after you've submitted the DN), the integration won't auto-process them** — you'll get a flag on the FDE worklist saying "late goods after submitted DN — needs reconciliation."

---

## Scenario 3: Inward to EE warehouse (source NOT EE, destination IS EE)

You're bringing stock into an EE-managed warehouse from somewhere outside EE — maybe a 3PL, maybe a non-EE branch, maybe a contract manufacturer.

### Flow

Almost identical to Scenario 2B (different-GSTIN flow), with one key difference: the integration creates a **Purchase Order on the EasyEcom side** (instead of a Stock Transfer Order) so EE knows goods are arriving from outside its universe.

| Step | Who | What |
| --- | --- | --- |
| 1 | YOU | Create an Internal-Customer DN (source non-EE warehouse → destination EE warehouse) |
| 2 | YOU | Submit the DN |
| 3 | SYSTEM | Creates a Transfer Map record |
| 4 | SYSTEM | If GSTINs differ: auto-drafts a Sales Invoice (DRAFT) |
| 5 | SYSTEM | Pushes a Purchase Order to EasyEcom (NOT an STN — because source is outside EE). *This is the key difference from Scenario 2B.* |
| 6 | SYSTEM | Stock parks in your Goods-in-Transit (GIT) warehouse |
| 7 | EASYECOM | Receives goods at destination (mark GRN-Complete in EE) |
| 8 | SYSTEM | Auto-submits the IPR (if SI is submitted) or holds it (if SI isn't submitted yet — auto-retry on SI submit) |
| 9 | YOU | Submit the auto-drafted SI when ready |
| 10 | SYSTEM | If different-GSTIN: auto-drafts IPI; auto-drafts Debit Note on any gap |
| 11 | YOU | Submit the IPI; submit the Debit Note (if drafted) |
| 12 | DONE | Transfer complete |

#### Prerequisite (FDE setup, not your concern)

The source side needs to be representable as an EE vendor. **If your FDE hasn't set this up, your DN will go into Drift state and they'll fix it.** You'll see a flag; you don't have to do anything about it. The FDE handles drift.

---

## Scenario 4: Dispatch from EE to outside (source IS EE, destination NOT EE)

You're sending stock out of an EE-managed warehouse to somewhere outside EE — a B2B customer's facility, a non-EE branch, a 3PL that doesn't run EE.

### Flow

| Step | Who | What |
| --- | --- | --- |
| 1 | YOU | Create an Internal-Customer DN (source EE warehouse → destination non-EE warehouse) |
| 2 | YOU | Submit the DN |
| 3 | SYSTEM | Creates a Transfer Map record |
| 4 | SYSTEM | If GSTINs differ: auto-drafts a Sales Invoice (DRAFT) |
| 5 | SYSTEM | Pushes a B2B order to EasyEcom (`orderType = businessorder`). *EE knows it's dispatching goods outside its universe.* |
| 6 | EASYECOM | Dispatches goods from the EE warehouse |
| 7 | EASYECOM | (Goods leave EasyEcom's universe — no EE-side receipt event) |
| 8 | YOU | If a SI was drafted: submit it when ready |
| 9 | YOU | When goods physically arrive at the non-EE destination, **RECORD THE RECEIPT YOURSELF** in ERPNext (Purchase Receipt or Stock Entry — standard ERPNext). *The integration cannot auto-create an IPR — destination is outside EE.* |
| 10 | DONE | Transfer complete on the destination side (your manual receipt) + GST documents settled (if applicable) |

#### What's different from other scenarios

**The integration does NOT create an IPR or IPI on the destination side.** Stock has left EasyEcom's universe — there's no EE event to trigger inbound processing. You record the receipt manually in ERPNext using standard ERPNext UX.

**Important:** the §10 automation ends when goods leave EE's universe. Everything downstream from step 7 is your responsibility via standard ERPNext.

---

## What you'll see in ERPNext for any §10 transfer

### The Transfer Map

A new record type called `EasyEcom Transfer Map` is created for every §10 transfer. Open it to see:

- The originating Delivery Note.
- The auto-drafted Sales Invoice (if any).
- The list of Internal Purchase Receipts created so far.
- The auto-drafted Internal Purchase Invoice.
- The auto-drafted Debit Note (if any).
- The current Goods-in-Transit balance for this transfer.
- The status, showing where the transfer is in its lifecycle.

### Status meanings

| Status | Meaning |
| --- | --- |
| Mapped | Just created, pre-push |
| SI-Pending | SI drafted, waiting for you to submit |
| EE-Pushed | Pushed to EasyEcom, awaiting receipt |
| Partial-Received | Some goods have arrived; more expected |
| Fully-Received | All dispatched units received; transfer cleanly closed |
| DN-Submitted-Locked | Debit Note was submitted (loss acknowledged); transfer closed |
| Drift | Something is wrong — see flag_reason; FDE will handle |
| Disabled | Manually disabled |

### Warehouse autocomplete shows EE-mapping

On the DN form, when you click into a warehouse field, EE-mapped warehouses appear at the top of the dropdown with their EE label (e.g. `EE: Delhi B2B WH (#4521)`). Non-EE warehouses appear below with no label. Helps you pick the right warehouse without checking elsewhere.

### Branch prediction chip

Once you've picked your source and destination warehouses on a §10-marked DN, a chip appears on the form telling you which §10 scenario you're in: `§10 branch: STN`, `§10 branch: B2B`, etc. This way you know what's about to happen before you submit.

### ToDos and Comments

The integration uses standard ERPNext ToDos and Comments to nudge you. Examples:
- "Submit the SI on Transfer Map ECS-XFER-DN-26-00040 — IPR is waiting" → on the IPR.
- "GIT aged past 30 days on this transfer — submit DN or investigate" → on the originating DN.
- "Late goods arrived after submitted DN — needs reconciliation" → Integration Discrepancy raised; FDE will engage.

---

## What can go wrong

### Drift

If something's misconfigured (missing Internal Customer pair, unmapped item, missing target warehouse address, etc.), your Transfer Map goes into `Drift` status with a `flag_reason` explaining what's missing. **This is not your problem to fix** — the FDE handles drift cases. You'll see the flag, and the FDE will see it on their worklist.

### Aged goods-in-transit

If dispatched units don't get fully received within `lost_in_transit_threshold_days` (default 30), you get a ToDo on the originating DN. Decide whether to submit the auto-drafted Debit Note (accept the loss) or investigate further (call the warehouse, check for missed GRNs, etc.).

### Cancelling a DN after it's been pushed to EE

You can't (currently). If you try to cancel a DN whose Transfer Map is in EE-Pushed status or beyond, you'll get a clear error message. The integration doesn't yet support cancel — the EE cancel endpoint hasn't been wired. Contact your integration team if this happens; they'll handle the EE-side cancellation manually.

If the DN is still in pre-push state (Mapped, Drift, SI-Pending without EE having been called), it cancels cleanly.

### Submit order matters

For different-GSTIN flows: **SI must be submitted before the IPR can submit, and IPR must be submitted before the IPI auto-drafts.** If you try to submit out of order (e.g. try to submit the IPI before the IPR is in), ERPNext will refuse — that's a financial-precondition rule. Submit in order: SI → wait for IPR to auto-submit → IPI → DN (if a gap exists).

---

## Quick reference: what gets created in each scenario

| Scenario | DN | SI | EE push | IPR | IPI / DN |
| --- | --- | --- | --- | --- | --- |
| 1 — neither EE | You | — | — | — | — |
| 2A — both EE, same GSTIN | You | — | STN | Auto-submitted | — |
| 2B — both EE, different GSTIN | You | Auto-drafted | STN | Auto-submitted (after SI) | IPI auto-drafted; DN auto-drafted on gap |
| 3 — source non-EE, dest EE, same GSTIN | You | — | PO | Auto-submitted | — |
| 3 — source non-EE, dest EE, different GSTIN | You | Auto-drafted | PO | Auto-submitted (after SI) | IPI auto-drafted; DN auto-drafted on gap |
| 4 — source EE, dest non-EE, same GSTIN | You | — | B2B | — (you record manually) | — |
| 4 — source EE, dest non-EE, different GSTIN | You | Auto-drafted | B2B | — (you record manually) | — |

**Pattern:** if the destination is outside EE (Scenario 4), the destination-side receipt is your manual work in ERPNext. Everywhere else, the integration handles the receipt-side documents.

---

## Frequently asked questions

**Why doesn't the integration submit the SI for me?** Because submitting a Sales Invoice has GST and accounting consequences. You decide when it's right to submit — the integration creates the draft, you confirm.

**The Transfer Map says SI-Pending but I already submitted the SI.** Refresh the page. Or check whether you submitted the right SI — the Transfer Map's `sales_invoice` field links to the exact one. If you've submitted the right one and the status hasn't advanced, contact your FDE.

**Goods arrived but the IPR is in Draft.** Check the IPR's ToDo and Comments. The most common cause: the SI hasn't been submitted yet. Submit the SI; the IPR will auto-submit within seconds.

**The Debit Note draft has the wrong quantity.** The DN is sized to the gap (dispatched − received). If you've had multiple GRNs and the cumulative received changed, the draft DN updates automatically. If the qty still looks wrong, check the cumulative receipt summary on the Transfer Map form.

**The auto-cancelled DN disappeared. Where's the audit trail?** Look at the Transfer Map's Comments. The integration writes a Comment on the Transfer Map ("Auto-cancelled draft Debit Note X — cumulative receipt closed the gap on GRN Y") before deleting the DN. The Transfer Map survives; the Comment survives with it.

**What's the difference between Goods-in-Transit and the destination warehouse?** GIT is a buffer warehouse owned by the destination Company. Stock parks there when the DN is submitted (because the DN says "goods are leaving the source"). Stock moves GIT → destination warehouse only when the IPR is submitted (because the IPR says "goods have actually arrived"). The gap between these two events is your real-world transit window.

**Can I do partial transfers — send 60 units this week, 40 next week — from one DN?** No, each DN represents one dispatch event. If you need to dispatch in batches, create separate DNs. Multi-GRN handling is about EE recording *receipt* in batches, not about ERPNext-side dispatch batching.

---

*For technical detail (EE endpoints, payload contracts, status enum, doc_event hooks), see `FDE_PRIMER_section_10_stock_transfer.md`. For step-by-step verification testing, see `process/test_scripts/section_10_stock_transfer.md`.*
