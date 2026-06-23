# §11 B2B Sales (Phase 1) — Test Script

Repeatable script for the FDE to validate §11 Phase 1 on any
deployment. Mirrors `process/test_scripts/section_9_buying.md` and
`process/test_scripts/section_10_stock_transfer.md`.

Phase 1 scope is push (Async) + cancel + polling reconciliation. The
invoice / SRE / dispatch flow is Phase 2 and intentionally not in
this script.

---

## 0. Prerequisites (one-time per bench)

- [ ] `EasyEcom Account` configured, `Test Connection` passes (refer
      §3 primer if not).
- [ ] At least one `EasyEcom Location` Live + enabled, with
      `mapped_warehouse` set.
- [ ] `EasyEcom Account.ecs_b2b_module` set to `Old B2B` or
      `New B2B` per your EE tenant.
- [ ] At least one Customer pushed to EE successfully (§8e —
      Customer Map status `Mapped`, `ee_customer_id` populated).
- [ ] At least one Item pushed to EE successfully (§8d — Item Map
      status `Mapped` or `Created-Flagged`, with HSN code on the
      Item).
- [ ] India Compliance configured: Company has `gstin` set,
      Customer has `gstin`.

## 1. Precondition-gate test (negative paths)

For each of the six preconditions, force the failure and confirm
the SO submit refuses with the expected message.

### 1.1 Customer not synced

- Create a Customer that has NOT been pushed to EE.
- Create a Sales Order against it, all other prereqs met.
- Submit → expect `"Customer {customer} is not synced to EasyEcom..."`.

### 1.2 Item not synced

- Create an Item without pushing it to EE.
- Create an SO line referencing it.
- Submit → expect `"Item {item_code} is not synced..."`.

### 1.3 Warehouse not mapped

- Create an SO with `target_warehouse` that has no EasyEcom Location
  link.
- Submit → expect `"Warehouse {warehouse} is not mapped..."`.

### 1.4 Customer GSTIN missing

- Open the Customer, clear `gstin`, save.
- Submit an SO for that Customer.
- Submit → expect `"Customer {customer} has no GSTIN..."`.

### 1.5 HSN missing

- Open an Item, clear `gst_hsn_code`, save.
- Submit SO with that Item.
- Submit → expect `"Item {item_code} missing HSN code."`.

### 1.6 Zero price

- Create SO line with `rate = 0`, no Free-of-Charge flag.
- Submit → expect `"Item {item_code} has rate 0..."`.

✅ Passing means: every gate produces the right message AND no Map
row, no Queue Job, no API Call gets written when the gate fires.

## 2. Happy-path push (Async, New B2B)

- [ ] Create a Sales Order with all prerequisites satisfied.
- [ ] Submit.
- [ ] Confirm `EasyEcom B2B Order Map` row appears immediately at
      status `Queued`, keyed on the SO docname.
- [ ] Within ~15 seconds, confirm:
  - [ ] Map status flips to `Pushed`
  - [ ] `ee_order_id`, `ee_suborder_id`, `ee_invoice_id` populated
  - [ ] New row in `EasyEcom API Call` list with endpoint
        `/webhook/v2/createOrder` (or `/Wholesale/createOrder` for
        Old B2B), status `Success`, response_status_code 200
- [ ] Open the SO → branch chip top of form shows the B2B branch
      with the EE OrderID linked.
- [ ] Click `Trace B2B Push` button → walk through every gate
      pass, see Map / Sync Record / Queue Job / API Call all named.

✅ Passing means: the push round-trips cleanly and is traceable
end-to-end from the SO form.

## 3. Cancel from ERPNext

- [ ] Take a SO with a `Pushed` Map (from step 2).
- [ ] Cancel the SO (Frappe Cancel action).
- [ ] Confirm:
  - [ ] `on_cancel` hook fires `cancel_b2b_order_from_erpnext`
  - [ ] New EasyEcom API Call for the cancel endpoint
  - [ ] Map status → `Cancelled`
  - [ ] Sync Record landed for the cancel action

### 3a. Cancel a Shipped order (refusal path)

- [ ] Pick an SO whose Map has reached EE-side status Shipped
      (status_id 7) (or mock by setting Map's
      `last_observed_status_id = 7` if no shipped order available).
- [ ] Cancel the SO.
- [ ] Confirm: cancel is refused with a clear message; Map stays
      at `Pushed`; no cancel API call fires; an Integration
      Discrepancy of kind "B2B cancel refused — shipped state"
      lands.

✅ Passing means: ERPNext can request a cancel only when EE is in
a cancellable state.

## 4. Polling reconciliation tick

### 4.1 Manual trigger (rather than waiting for the cron)

- [ ] From bench shell, fire the polling tick manually:
      `bench --site <site> execute
      ecommerce_super.easyecom.flows.b2b_sales.polling.run_polling_tick`
- [ ] Confirm in `EasyEcom API Call` list: one `/orders/V2/getOrderDetails`
      call per eligible Map.
- [ ] Confirm each Map row's `last_polled_at` is updated.

### 4.2 Invoice-pending detection

- [ ] In Harmony's UI, generate an invoice for one of the test
      orders (so `invoice_number` field gets populated on EE side).
- [ ] Re-fire the polling tick.
- [ ] Confirm the corresponding Map row's status flips to
      `Invoice Pending`.

### 4.3 EE-side cancel detection (post-merge live verification)

- [ ] In Harmony's UI, cancel a test B2B order (set
      `order_status_id = 9`, fully cancel all suborders).
- [ ] Re-fire the polling tick.
- [ ] Confirm:
  - [ ] Map status flips to `Cancelled`
  - [ ] Discrepancy of kind "B2B order cancelled by EE —
        polling-detected" raised against the Map row

### 4.4 Orphan probe

- [ ] Create a Map row with a `reference_code` that EE doesn't know.
      (Easiest: enqueue an SO Push but force EE to reject by passing
      bad data; the Map row gets created at `Queued` then `Drift`.)
- [ ] Fire polling tick.
- [ ] Confirm Discrepancy of kind "B2B Map orphaned at EE" raised.

### 4.5 Partial cancel detection

- [ ] In Harmony, partially cancel an order (cancel some line qty
      but not all).
- [ ] Fire polling tick.
- [ ] Confirm Discrepancy of kind "B2B order partial cancellation
      detected" raised. Map status does NOT flip (Phase 2 territory).

### 4.6 Unknown status_id

- Can only be verified if EE introduces a new status. Skip in
  normal smoke; covered by the unit test
  `TestUnknownStatus.test_status_id_outside_enum_is_unknown`.

✅ Passing 4.1 – 4.5 means: polling correctly maps every state
EE returns and surfaces drift for human attention.

## 5. FDE Worklist surfaces

- [ ] EasyEcom Workspace → confirm the three §11 cards render:
  - [ ] "B2B Maps in Drift"
  - [ ] "B2B Discrepancies (Open)"
  - [ ] "B2B Queue Jobs (Failed / Retrying)"
- [ ] Each card click-through opens a filtered list.
- [ ] After the live smoke (steps 2-4), each card shows correct counts.

✅ Passing means: an FDE doing a daily worklist review can spot
every §11 incident from the workspace.

## 6. Idempotency

- [ ] Re-submit the same SO (clone + submit) — confirm a fresh Map
      row + fresh EE order_id are produced (no clobbering).
- [ ] Re-cancel an already-cancelled SO — confirm idempotent (no
      duplicate cancel call, no extra Discrepancy).
- [ ] Run the polling tick twice in a row with no EE-side change —
      confirm second run is a no-op (`last_polled_at` updated, no
      new Discrepancies, no status flip).

✅ Passing means: re-runs are safe.

## 7. Module-discriminator test (Old B2B vs New B2B)

- [ ] If your tenant supports both, flip `Account.ecs_b2b_module`
      between `Old B2B` and `New B2B` and re-run step 2 in each
      configuration.
- [ ] Confirm the EE API Call's endpoint differs accordingly:
      `/Wholesale/createOrder` for Old B2B, `/webhook/v2/createOrder`
      for New B2B.

✅ Passing means: Account-level discrimination works.

## 8. Test-script regression

After every §11 patch (any commit touching `flows/b2b_sales/`),
re-run sections 1, 2, 3, 4 at minimum. Sections 5-7 quarterly.

---

## What passing means

§11 Phase 1 is operationally healthy on this deployment when:

- ✅ All six precondition gates fire with the right message (Section 1)
- ✅ A clean SO produces Map + EE OrderID round-trip (Section 2)
- ✅ Cancel from ERPNext works and the Shipped-state refusal triggers (Section 3)
- ✅ Polling tick correctly classifies every state EE returns (Section 4)
- ✅ FDE Worklist surfaces every §11 incident (Section 5)
- ✅ Idempotency holds (Section 6)
- ✅ Module-discriminator picks the right endpoint (Section 7)
