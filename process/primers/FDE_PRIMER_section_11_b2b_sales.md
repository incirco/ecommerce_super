# FDE Primer — §11 B2B Sales (Phase 1)

For Forward-Deployed Engineers operating §11 in production. Companion
to `SECTION_11_COMPLETION_CHECKLIST.md` (build state) and
`process/test_scripts/section_11_b2b_sales.md` (repeatable smoke).

Phase 1 covers: SO push (Async) → EasyEcom acknowledgement → cancel
from ERPNext side → polling reconciliation of EE-side state changes.

Phase 2 (invoice flow, dispatch, SRE mirror, multi-warehouse split,
sync push mode) is not in scope here.

---

## Part A — What §11 does, in one paragraph

A B2B Sales Order born in ERPNext fires through `on_submit` to
EasyEcom as a wholesale createOrder. EE acknowledges with an
`OrderID` / `SuborderID` / `InvoiceID` triple, which we capture on
the `EasyEcom B2B Order Map` row keyed to the SO. The order then
lives on EE's side through its fulfilment lifecycle; ERPNext polls
EE every 15 minutes per Account (cadence configurable) to read the
current state via `/orders/V2/getOrderDetails`. If the user cancels
the SO in ERPNext, the integration fires a cancel POST to EE. If
EE-side state changes (cancellation, invoice issuance), polling
detects them and raises Discrepancies for FDE attention.

## Part B — The data spine

- **`EasyEcom B2B Order Map`** — the per-SO mapping row. Carries
  the EE identifiers (`ee_order_id`, `ee_suborder_id`,
  `ee_invoice_id`), local state (`status`), and the polling
  watermark (`last_polled_at`).
- **`EasyEcom Queue Job`** of type `SO Push` — async enqueued by
  the SO `on_submit` hook. Worker calls EE's createOrder, captures
  the response, updates the Map.
- **`EasyEcom Account` settings** that matter:
  - `ecs_b2b_module` — `Old B2B` (legacy `/Wholesale/createOrder`)
    OR `New B2B` (`/webhook/v2/createOrder`). MMPL is on `New B2B`.
  - `ecs_polling_cadence_minutes` — default 15. Per-tick eligibility
    is `last_polled_at IS NULL OR last_polled_at <= NOW() - cadence`.

## Part C — Operator-visible surfaces

| Surface | Where | When you use it |
|---|---|---|
| **Branch chip on SO form** | top of SO form when target_warehouse picked | Pre-submit verification of which §11 path will fire |
| **EasyEcom Workspace → FDE Worklist** | desk workspace | Daily worklist — see Maps in `Queued`, `Drift`, etc. |
| **Trace B2B Push button on SO form** | top-right "EasyEcom" menu on submitted SOs | Read-only diagnostic — walks every §11 gate |
| **`EasyEcom B2B Order Map` list** | `/app/easyecom-b2b-order-map` | Find any SO's Map row by docname or filter by status |
| **`EasyEcom Integration Discrepancy` list** | `/app/easyecom-integration-discrepancy` | Polling-detected EE-side state changes that need FDE review |

## Part D — The §11.2 preconditions (the SO-submit gate)

`on_submit` fires `validate_pre_submit` which enforces six conditions.
If any fail, the SO submit is refused with the exact error text:

| Gate | Message the user sees |
|---|---|
| Customer synced to EE (Customer Map status=Mapped, `ee_customer_id` set) | "Customer {customer} is not synced to EasyEcom for company {company}." |
| All items synced (every SO line's Item has an EE Item Map row Mapped or Created-Flagged) | "Item {item_code} is not synced to EasyEcom." |
| target_warehouse mapped to a Live EE Location | "Warehouse {warehouse} is not mapped." |
| Customer.gstin populated | "Customer {customer} has no GSTIN. B2B GST invoice cannot be generated." |
| HSN on every item | "Item {item_code} missing HSN code." |
| Pricing complete (no zero rates unless explicitly free-of-charge) | "Item {item_code} has rate 0; mark explicitly as Free of Charge or set price." |

The gate fires before the Queue Job is enqueued — there's no
"Queued-then-flagged" race. Fix the cause, re-submit.

## Part E — The push lifecycle (the happy path)

1. User submits the Sales Order.
2. `validate_pre_submit` runs all six gates.
3. `enqueue_on_so_submit` writes a `B2B Order Map` row at status
   `Queued` and enqueues an `SO Push` Queue Job.
4. Worker fires `push_b2b_order_async` → routes to
   `build_old_b2b_payload` or `build_new_b2b_payload` per
   `Account.ecs_b2b_module`.
5. EE returns `{OrderID, SuborderID, InvoiceID}`. Worker stamps
   these on the Map row and flips status to `Pushed`.
6. The polling loop subsequently transitions status to
   `Invoice Pending` (when `invoice_number` appears on EE side) or
   `Cancelled` (when EE flips `order_status_id` to 9).

## Part F — Cancel from ERPNext

1. User cancels the Sales Order (Frappe Cancel action).
2. `cancel_b2b_order_from_erpnext` runs as an `on_cancel` hook.
3. If the Map's current EE state is `Shipped` (any of EE statuses
   {7, 10, …}) the cancel is **refused** with a clear message; the
   integration won't ask EE to cancel a shipped order.
4. Otherwise the cancel POST fires; Map status → `Cancelled`.

## Part G — Polling reconciliation

- Scheduler tick: cron `*/5` (every 5 minutes).
- Per-Account eligibility filter: `last_polled_at IS NULL OR
  last_polled_at <= NOW() - ecs_polling_cadence_minutes`.
- For each eligible Map: fire `getOrderDetails` keyed on
  `reference_code = <SO.name>`, run `derive_local_status_from_ee_rows`,
  apply the decision.

Decision table (locked):

| Decision | Trigger | Action |
|---|---|---|
| `orphan` | No `businessorder` rows returned | Raise Discrepancy "B2B Map orphaned at EE" |
| `transition_to "Cancelled"` | All rows status=9 AND all qty cancelled | Flip Map status; raise Discrepancy "B2B order cancelled by EE — polling-detected" |
| `partial_cancel` | Some qty cancelled but not all | Raise Discrepancy "B2B order partial cancellation detected"; no local state flip (Phase 2 territory) |
| `transition_to "Invoice Pending"` | Any row has `invoice_number` | Flip Map status |
| `no_change` | Latest row in {1,2,3,4,5,6,7,30} | Update `last_polled_at` only |
| `unknown` | `order_status_id` outside known enum | Raise Discrepancy "B2B unknown order_status_id" |

## Part H — FDE Worklist (the daily view)

The EasyEcom Workspace surfaces three §11 cards (added in Stage 3):

- **B2B Maps in Drift** — Map rows with status `Drift`. Click through
  to inspect `flag_reason`.
- **B2B Discrepancies (Open)** — Integration Discrepancies of kind
  starting with "B2B …". Click through, read context, take action.
- **B2B Queue Jobs (Failed / Retrying)** — `SO Push` Queue Jobs
  with non-Success state. Click → inspect error → Retry from the
  Queue Job form.

## Part I — Common errors and recovery

### "Customer {customer} is not synced to EasyEcom"

Push the Customer first via `Customer form → EasyEcom → Push to
EasyEcom` (§8e). Wait for Customer Map status `Mapped` + non-empty
`ee_customer_id`. Re-submit the SO.

### "Item {item_code} is not synced to EasyEcom"

Push the Item via `Item form → EasyEcom → Push to EasyEcom` (§8d).
Wait for Item Map status `Mapped` or `Created-Flagged`. Re-submit.

### "Warehouse {warehouse} is not mapped"

The target_warehouse must be linked to a Live + enabled EasyEcom
Location (via `mapped_warehouse`). If it shows up but label is
empty: hit
`/api/method/ecommerce_super.easyecom.flows.warehouse_label_sync.backfill_all`.
If no Location maps to it: configure the Location in EE Masters →
Locations first.

### Queue Job stuck in Failed

Open the `EasyEcom Queue Job` row. Read `translated_error`. Common
causes:

- EE returned a payload-validation error → fix the SO field that
  triggered it, retry.
- Token expired (HTTP 401) → check `EasyEcom Account` credentials,
  Test Connection.
- EE-side rate limit → wait and retry (the job auto-retries with
  back-off, but FDE can force-retry from the form).

### B2B Map status stuck at `Queued`

The Queue Job was enqueued but the worker hasn't picked it up. Check
the bench's worker pool is running. If the job state is `Failed`
but Map is still `Queued`, this is a bug — report.

### "B2B Map orphaned at EE" Discrepancy

EE has no record matching the SO's `reference_code`. Either the push
silently failed (check API Call logs), or someone deleted the order
EE-side. FDE decision: dismiss + manual re-push, or accept the
orphan and cancel the local SO.

### "B2B order cancelled by EE — polling-detected"

Someone cancelled the order from EasyEcom's UI. ERPNext now knows.
FDE action: investigate cause (refund? customer complaint?). If
ERPNext should follow: cancel the local SO. If not: dismiss the
Discrepancy with a reason captured.

### "B2B unknown order_status_id"

EE returned a status_id outside the documented enum {1,2,3,4,5,6,
7,9,10,30}. Either EE added a new state we don't know about, or
malformed data. Capture the value (in the Discrepancy detail),
escalate to design-lead.

## Part J — When in doubt

- The **Trace B2B Push button** on the SO form is the fastest
  diagnostic — read-only, walks every gate, lists every downstream
  artifact.
- The Map row's `flag_reason` is always the most precise message.
- For deep diagnosis, share the SO name + Map docname + flag_reason
  with engineering.

## Part K — Phase 2 (out of scope today)

These will be built when Phase 2 lands. Plan around their current
absence:

- **No automatic Stock Reservation Entry** when EE reserves
  inventory for the order. ERPNext available-to-promise won't
  reflect EE-side commitments. Phase 2.
- **No automatic Sales Invoice + e-waybill** when EE requests
  one via webhook. Phase 1 keeps the SI as an FDE-driven manual step.
- **No automatic SI status flip to Delivered** on EE dispatch. Phase 2.
- **No multi-warehouse SO split** into separate EE orders. If an SO
  has lines spanning multiple linked warehouses, the Phase 1 push
  uses the SO's `target_warehouse` header field only; lines in other
  warehouses are silently included in one EE order (which EE may or
  may not accept). Use single-warehouse SOs in Phase 1.
- **No sync push mode** — all pushes are async. SO submit is
  immediate; EE rejection surfaces via the FDE Worklist, not as a
  submit-blocking error.
