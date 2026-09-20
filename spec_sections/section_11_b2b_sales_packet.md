# 11 — B2B Sales Flow (§11) — Build Packet

*Operational flow #3, post-§10 Stock Transfers. Build phase-by-phase; Phase 1 (push side + local reservation + ERPNext-initiated cancellation) is fully grounded against EE's documented createOrder API and the client's confirmed business model. Phase 2 (Generate Invoice receiver, Branch X/Y invoice flow, EE-initiated cancellation, returns) is deferred pending further EE clarification. The pre-build SPEC.md §11 is **superseded** by this packet — §11.4 (Stock Reservation mirror via assumed inventory.reserved webhook) is structurally invalid because EE does not expose reservation state. §11.5's two-branch invoice model is also reframed: EE owns invoice generation; ERPNext mirrors. Branch X (EE's default GSP) vs Branch Y (ERPNext-as-GSP) replaces the original Branch A vs Branch B distinction.*

> Single-writer rule. No real EE writes during dev/test except Harmony (Old B2B) + Puresta Test location (New B2B). SPEC.md is rewritten via §11 patch-notes at closeout, not before.

## The principle (locked, design-defining)

**The integration does not modify any GL postings.** Same discipline as §9 and §10. ERPNext + India Compliance own all financial documents; the integration orchestrates *which* documents to create *when* with *which* references. Stock movement and tax accounting are ERPNext-native.

## The §11 invariant (write into the controller as a docstring)

> §11 push fires when SO.set_warehouse maps to an EE-mapped Location. All line items must use set_warehouse — mixed-warehouse SOs are refused at on_submit, not silently split. EE owns the order's downstream lifecycle (picking, packing, invoice generation, dispatch); ERPNext receives invoice details via the Generate Invoice webhook and mirrors as Sales Invoice. Stock reservation is local to ERPNext only — EE does not expose reservation state. Phase 2 work is deferred until EE confirms Generate Invoice payload + Custom GSP contract + Cancel Order webhook + Mark Return mechanism.

## Role separation (locked)

- **FDE:** integration setup. Configures EasyEcom Account `ecs_b2b_module` (Old vs New B2B), configures `ecs_eway_origination` (EasyEcom vs ERPNext), wires credentials, runs precheck, triages **integration-health Discrepancies** (push failures, webhook delivery gaps, polling-detected divergences).
- **ERP User:** business flow. Creates Sales Orders, links Payment Entries (or doesn't, for credit-terms customers), submits SO. **All operational pendings are ERP User's responsibility.** Submits Sales Invoices once the integration auto-drafts them from EE's Generate Invoice webhook (Phase 2).
- **Surface separation:** FDE Worklist (§17 workspace cards) carries integration-health items only — pushed-but-no-invoice SOs, polling-detected reservation drift, failed Custom GSP responses (Branch Y), Generate Invoice mirror variances. Operational pendings live in ERPNext-native UX.

## The model (settled, EE-coherent)

**Gate 0 (lifecycle-wide):** integration acts iff `SO.set_warehouse` maps to an EE-mapped Location via §8a Source-of-Truth Map. set_warehouse empty or non-EE-mapped → silently inert (pure ERPNext, integration not involved). Same Gate-0 pattern as §9 and §10.

**All ERPNext SOs are B2B by definition** in the client's business model. §12 (B2C / D2C / Marketplace) orders originate in EasyEcom and pull into ERPNext as Sales Invoices directly — they never become Sales Orders in the §11 sense. So in ERPNext, every SO is a §11 candidate when its set_warehouse maps to EE.

**No marketplace layer in §11.** Unlike §12, there is no per-channel concept. Each SO is customer-keyed. The customer is the entire context.

**Two B2B module variants on EE side:**
- **Old B2B** — original EE B2B implementation. Synchronous response: `createOrder` returns SuborderID + OrderID + InvoiceID immediately.
- **New B2B** — newer EE B2B implementation. Asynchronous queue: `createOrder` returns `"Successfully Queued"` with empty data. The identifier correlation mechanism is currently `[OPEN: pending EE clarification]`.

Per EE Account, only one of the two modules is enabled. The integration must know which (via `ecs_b2b_module` configuration) to construct the right payload and handle the right response shape.

**EE owns invoice generation.** ERPNext receives the invoice via the Generate Invoice webhook and mirrors as Sales Invoice. There is no "ERPNext-originated invoice" mode (the original SPEC's Branch A is not a real EE capability).

**Two e-way origination modes (configured at EE Account level via `ecs_eway_origination`):**
- **Branch X — EasyEcom (default).** EE uses its own default GSP to generate IRN and e-way internally. ERPNext receives final invoice values (including IRN + e-way) via the Generate Invoice webhook and mirrors. No upload from ERPNext to EE.
- **Branch Y — ERPNext.** EE generates invoice values, then calls ERPNext's exposed Custom GSP endpoint *synchronously* to obtain IRN + e-way. ERPNext generates these via India Compliance and responds. EE proceeds with dispatch using the returned values. The Generate Invoice webhook fires afterward with the complete invoice state.

**Stock reservation is local-only.** ERPNext-side Stock Reservation Entry (ERPNext v16) fires on SO submission if Stock Settings has reservation enabled. **No coupling to EE-side reservation state** because EE does not expose reservation visibility (no `inventory.reserved` webhook, no pull endpoint returning reservation state; EE has confirmed this is "in discussion, may come in future"). The reconciliation channel is the Get All Orders polling fallback, which detects EE-side cancellations and releases the local reservation.

**Lifecycle visible to ERPNext is only two events plus polling fallback:**
- `Generate Invoice` webhook — EE fires when invoice is generated for a B2B order (= dispatch is imminent)
- `Cancel Order` webhook — EE fires when an order is cancelled (EE-initiated)
- `Get All Orders` pull — polling fallback to catch missed events

No webhooks for: picking started/completed, packing, partial dispatch, delivery confirmation, customer-initiated cancellation. EE only emits Create Order and Generate Invoice events.

## Phase 1 vs Phase 2 scope

**Phase 1 (this packet, fully grounded):**
- Push side — Old B2B and New B2B payload construction, async push, response handling
- Local stock reservation handling
- ERPNext-initiated cancellation (call to `/orders/cancelOrder` before invoice generation)
- Get All Orders polling fallback for divergence detection
- FDE Worklist cards for pushed-but-pending orders, push failures, polling-detected divergences
- Gating logic (set_warehouse EE-mapped → §11 fires)
- Configuration on EE Account (`ecs_b2b_module`, `ecs_eway_origination`)
- Custom Field on SO for §11 metadata (ecs_b2b_so_map link)

**Phase 2 (deferred pending EE clarification):**
- Generate Invoice webhook receiver → Sales Invoice mirror (Branch X)
- Custom GSP endpoint exposure → IRN + e-way generation (Branch Y)
- EE-initiated Cancel Order webhook receiver
- "Mark Return" mechanism + Credit Note handling
- Variance check on mirrored SI vs SO
- Webhook idempotency mechanism

Phase 2 requires the following EE answers before design can be locked:
- Generate Invoice webhook payload sample + signature (same for Old/New?)
- Custom GSP endpoint contract (request, response, timeout, auth, idempotency)
- Cancel Order webhook payload
- "Mark Return" mechanism (webhook / pull / manual)
- Credit note ownership (EE-issued / ERPNext-issued / mirrored)
- Webhook idempotency and retry policy
- Rate limits

## Gate 0 and preconditions

### Gate 0 (silent inert)

```
def is_section_11_gated(so):
    if not so.set_warehouse:
        return False  # No source warehouse → no §11.
    ee_location = lookup_ee_location_for_warehouse(so.set_warehouse)
    return ee_location is not None  # EE-mapped → §11 fires.
```

If Gate 0 is False, the integration does nothing on `on_submit`. The SO stays purely ERPNext-native.

### Preconditions (refuse with clear error)

When Gate 0 is True, every condition below must hold. Any failure refuses the SO submit with a specific error message:

| Precondition | Refusal message |
| --- | --- |
| All line items' `warehouse == set_warehouse` | "Mixed warehouses not supported for §11 push. All line items must use set_warehouse." |
| EE Account has `ecs_b2b_module` configured | "EasyEcom Account is missing the B2B module configuration (Old B2B / New B2B). Configure before pushing." |
| Customer has `ecs_easyecom_customer_id` for this Company (§8e synced) | "Customer {customer} is not synced to EasyEcom for company {company}. Sync the customer before submitting." |
| Every line item has `ecs_easyecom_product_sku_code` (§8d synced) | "Item {item_code} is not synced to EasyEcom. Sync the item before submitting." |
| Customer has GSTIN (`tax_id` populated) — *Old B2B only; New B2B falls back to `"URP"`* | "Customer {customer} has no GSTIN. Old B2B requires GSTIN; no URP fallback." |
| Every item has HSN code | "Item {item_code} missing HSN code." |
| Every line has non-zero rate (or explicit free-of-charge flag) | "Item {item_code} has rate 0; mark explicitly as Free of Charge or set price." |
| Customer has a primary Billing Address Dynamic-Linked | "Customer {customer} has no Billing Address. B2B GST invoice cannot be generated without billing address." |
| SO has a Shipping Address Dynamic-Linked (or customer has Shipping Address) | "Sales Order {so} has no shipping address. Set shipping address or configure on customer." |

All refusals fire at `on_submit` via the standard ERPNext validation throw. The user cannot submit a §11-gated SO until preconditions pass.

## Push side (Phase 1, fully grounded)

### Push endpoint

Both Old B2B and New B2B use the same endpoint:

```
POST https://api.easyecom.io/webhook/v2/createOrder
Content-Type: application/json
Authorization: Bearer <token>
```

The `orderType: "businessorder"` field is constant for both. The module discriminator is **server-side configuration on the EE Account**, not in the request. Different payload shapes correspond to which module that EE Account is configured for.

### Payment derivation (shared logic, called by both payload builders)

```python
def derive_payment_fields(so):
    """
    Derive paymentMode, paymentTransactionNumber, collectableAmount, shippingMethod
    from SO's linked Payment Entries.

    Three scenarios:
    (I)   Full prepaid — PE total >= SO grand_total
    (II)  Partial prepaid — 0 < PE total < SO grand_total
    (III) Pure credit terms — no PE linked (collected via invoice + credit terms)

    EE's data model only has Prepaid (5) and COD (2). Scenarios (II) and (III)
    both bucket as COD on EE's side with collectableAmount carrying the
    "amount still owed" value. In the client's operational reality, there is
    no actual cash collection on delivery — collectableAmount represents
    deferred-invoice amount.
    """
    pe_references = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Order",
            "reference_name": so.name,
            "docstatus": 1,  # submitted PEs only
        },
        fields=["parent", "allocated_amount"],
    )

    advance_amount = sum(p.allocated_amount for p in pe_references)
    pe_names = [p.parent for p in pe_references]

    if advance_amount == 0:
        # Scenario III: pure credit terms.
        return {
            "paymentMode": 2,                     # COD bucket per EE's model
            "paymentTransactionNumber": "",
            "collectableAmount": so.grand_total,
            "shippingMethod": 1,                  # Standard COD
        }
    elif advance_amount >= so.grand_total:
        # Scenario I: full prepaid.
        pe = frappe.get_doc("Payment Entry", pe_names[-1])
        return {
            "paymentMode": 5,                     # Prepaid
            "paymentTransactionNumber": pe.reference_no or pe.name,
            "collectableAmount": 0,
            "shippingMethod": 3,                  # Standard Prepaid
        }
    else:
        # Scenario II: partial prepaid → bucketized as COD.
        pe = frappe.get_doc("Payment Entry", pe_names[-1])
        return {
            "paymentMode": 2,
            "paymentTransactionNumber": pe.reference_no or pe.name,
            "collectableAmount": so.grand_total - advance_amount,
            "shippingMethod": 1,
        }
```

### Date formatting

EE's createOrder uses two different timezone conventions:

| Field | Format | Timezone |
| --- | --- | --- |
| `orderDate` | `"YYYY-MM-DD HH:MM:SS"` | UTC |
| `expDeliveryDate` | `"YYYY-MM-DD HH:MM:SS"` | IST (Asia/Kolkata) |

The integration uses the ERPNext SO's `transaction_date + transaction_time` for orderDate (formatted as UTC) and `delivery_date` (formatted as IST midnight, since SO doesn't store a delivery time of day).

### Customer block builder

Shared between Old and New B2B:

```python
def build_customer_block(so, include_lat_long=False):
    """Construct the customer block for createOrder payload."""
    customer = frappe.get_doc("Customer", so.customer)
    billing_addr = frappe.get_doc("Address", customer.customer_primary_address)
    shipping_addr_name = so.shipping_address_name or customer.customer_primary_address
    shipping_addr = frappe.get_doc("Address", shipping_addr_name)

    block = {
        "customerId": customer.ecs_easyecom_customer_id,
        "billing": flatten_address(billing_addr, customer.customer_name, customer.mobile_no, customer.email_id),
        "shipping": flatten_address(shipping_addr, customer.customer_name, customer.mobile_no, customer.email_id),
    }

    if include_lat_long and shipping_addr.geo_location:
        # Only Old B2B sample shows latitude/longitude in shipping block.
        # ERPNext Address doesn't have geo_location by default — if added via Custom Field, populate; else skip.
        lat, lng = parse_geo(shipping_addr.geo_location)
        if lat and lng:
            block["shipping"]["latitude"] = lat
            block["shipping"]["longitude"] = lng

    return block


def flatten_address(addr, name, contact, email):
    return {
        "name": name,
        "addressLine1": addr.address_line1 or "",
        "addressLine2": addr.address_line2 or "",
        "postalCode": addr.pincode or "",
        "city": addr.city or "",
        "state": addr.state or "",
        "country": addr.country or "India",
        "contact": contact or "",
        "email": email or "",
    }
```

### Item line builder (Old B2B)

```python
def build_old_b2b_item(so, so_item):
    """One item entry for Old B2B payload."""
    item = frappe.get_doc("Item", so_item.item_code)
    return {
        "OrderItemId": f"{so.name}-line-{so_item.idx}",
        "Sku": item.ecs_easyecom_product_sku_code,  # §8d-synced primary identifier
        "productName": so_item.item_name,
        "Quantity": str(so_item.qty),                # Old B2B uses string
        "Price": so_item.rate,                       # decimals supported per FAQ 5
        "itemDiscount": so_item.discount_amount,     # per total quantity per FAQ 6
    }
```

### Item line builder (New B2B)

```python
def build_new_b2b_item(so, so_item):
    """One item entry for New B2B payload."""
    item = frappe.get_doc("Item", so_item.item_code)
    return {
        "OrderItemId": f"{so.name}-line-{so_item.idx}",  # mandatory per docs
        "Sku": item.ecs_easyecom_product_sku_code,
        "Quantity": int(so_item.qty),                # New B2B uses integer
        "Price": so_item.rate,
        "itemDiscount": so_item.discount_amount,
    }
```

### Old B2B payload builder

```python
def build_old_b2b_payload(so, ee_account):
    payment = derive_payment_fields(so)
    customer = frappe.get_doc("Customer", so.customer)
    # Old B2B requires GSTIN strictly; URP fallback is New B2B only.
    if not customer.tax_id:
        raise PreconditionError(
            "Customer GSTIN is missing. Old B2B requires GSTIN; URP fallback is "
            "only available for New B2B."
        )

    payload = {
        "orderType": "businessorder",
        "orderNumber": so.name,
        "orderDate": format_utc(so.transaction_date, so.transaction_time),
        "expDeliveryDate": format_ist_date(so.delivery_date),
        "is_market_shipped": 0,                       # Self-shipped (hardcoded per client model)
        "remarks1": so.terms or "",
        "remarks2": "",
        "shippingCost": get_shipping_charge(so),
        "discount": so.discount_amount,               # header-level
        "walletDiscount": 0,                          # B2C concepts; 0 for B2B
        "promoCodeDiscount": 0,
        "prepaidDiscount": 0,
        "paymentMode": payment["paymentMode"],
        "paymentGateway": "",                         # [OPEN: only required if EE rejects empty;
                                                      # confirm in build]
        "shippingMethod": payment["shippingMethod"],
        "paymentTransactionNumber": payment["paymentTransactionNumber"],
        "collectableAmount": payment["collectableAmount"],
        "packageWeight": 0,
        "packageHeight": 0,
        "packageWidth": 0,
        "packageLength": 0,
        "taxIdentificationNumber": customer.tax_id,   # GSTIN
        "items": [build_old_b2b_item(so, it) for it in so.items],
        "customer": [build_customer_block(so, include_lat_long=True)],
    }
    return payload
```

### New B2B payload builder

```python
def build_new_b2b_payload(so, ee_account):
    payment = derive_payment_fields(so)
    customer = frappe.get_doc("Customer", so.customer)
    gstin = customer.tax_id or "URP"                  # URP fallback for unregistered customers (New B2B only)

    payload = {
        "orderType": "businessorder",
        "orderNumber": so.name,
        "orderDate": format_utc(so.transaction_date, so.transaction_time),
        "is_market_shipped": 0,                       # Self-shipped (hardcoded)
        "remarks1": so.terms or "",
        "is_pricing_master": False,                   # ERPNext owns pricing
        "items": [build_new_b2b_item(so, it) for it in so.items],
        "paymentMode": payment["paymentMode"],
        "paymentTransactionNumber": payment["paymentTransactionNumber"],
        "collectableAmount": payment["collectableAmount"],
        "shippingMethod": payment["shippingMethod"],
        "shippingCost": get_shipping_charge(so),
        "discount": so.discount_amount,               # header-level discount supported in New B2B too
        "taxIdentificationNumber": gstin,
        "customer": [build_customer_block(so, include_lat_long=False)],
        "queue": 1,                                   # always queue for New B2B
    }
    return payload
```

### Push dispatcher and response handling

```python
def push_b2b_order(so):
    """§11 Phase 1 push side. Called from SO on_submit hook."""
    # Gate 0
    if not is_section_11_gated(so):
        return

    ee_account = lookup_ee_account_for_warehouse(so.set_warehouse)
    
    # Preconditions
    validate_preconditions(so, ee_account)

    # Build payload by module
    if ee_account.ecs_b2b_module == "Old B2B":
        payload = build_old_b2b_payload(so, ee_account)
    elif ee_account.ecs_b2b_module == "New B2B":
        payload = build_new_b2b_payload(so, ee_account)
    else:
        raise IntegrationError(
            "ecs_b2b_module not configured on EasyEcom Account. "
            "Cannot determine push payload format."
        )

    # POST to EE (async — SO submission completes regardless)
    response = post_to_ee(
        endpoint="/webhook/v2/createOrder",
        payload=payload,
        ee_account=ee_account,
        log_correlation_id=so.name,
    )

    # Handle response by module
    if ee_account.ecs_b2b_module == "Old B2B":
        handle_old_b2b_response(so, ee_account, response)
    elif ee_account.ecs_b2b_module == "New B2B":
        handle_new_b2b_response(so, ee_account, response)


def handle_old_b2b_response(so, ee_account, response):
    """Old B2B returns SuborderID + OrderID + InvoiceID synchronously."""
    if response.get("code") != 200:
        raise PushFailedError(response.get("message", "Unknown EE error"))

    data = response["data"]
    create_b2b_order_map(so, ee_account, {
        "module": "Old B2B",
        "suborder_id": data["SuborderID"],
        "order_id": data["OrderID"],
        "invoice_id": data["InvoiceID"],
        "pushed_at": now(),
        "status": "Pushed",
    })


def handle_new_b2b_response(so, ee_account, response):
    """New B2B returns 'Successfully Queued' with empty data — IDs assigned later."""
    if response.get("code") != 200:
        raise PushFailedError(response.get("message", "Unknown EE error"))

    create_b2b_order_map(so, ee_account, {
        "module": "New B2B",
        "suborder_id": None,                          # populated later
        "order_id": None,
        "invoice_id": None,
        "pushed_at": now(),
        "status": "Queued",
    })

    # [OPEN: New B2B identifier correlation. After EE confirms how identifiers
    # are delivered post-queue (webhook? polling? other?), this handler is
    # extended to schedule the correlation update.]
```

### EE Account configuration (new fields)

| Field | Type | Mandatory | Description |
| --- | --- | --- | --- |
| `ecs_b2b_module` | Select | Yes (if B2B used) | `"Old B2B"` or `"New B2B"`. Determines push payload shape. |
| `ecs_eway_origination` | Select | Yes (if B2B used) | `"EasyEcom"` (default) or `"ERPNext"`. Determines Phase 2 invoice flow branch X vs Y. |

Hardcoded constants (not exposed as fields, kept in code):
- `is_market_shipped = 0` (B2B is always self-shipped in client's business model)
- `is_pricing_master = False` (ERPNext is always the source of pricing truth)

### Sales Order configuration (new fields)

| Field | Type | Description |
| --- | --- | --- |
| `ecs_b2b_so_map` | Link to "EasyEcom B2B Order Map" | Per-SO mapping to EE-side identifiers. Populated post-push. Read-only. |

### EasyEcom B2B Order Map (new DocType)

Child doctype attached to Sales Order. One row per SO; tracks EE-side identifiers.

| Field | Type | Description |
| --- | --- | --- |
| `parent` | Link to Sales Order | Standard child-doctype parent |
| `easyecom_account` | Link to EasyEcom Account | Which EE Account this push went to |
| `module` | Select | `"Old B2B"` or `"New B2B"` |
| `ee_suborder_id` | Data | SuborderID returned by EE (or pending for New B2B) |
| `ee_order_id` | Data | OrderID returned by EE (or pending for New B2B) |
| `ee_invoice_id` | Data | InvoiceID returned by EE (or pending for New B2B) |
| `status` | Select | `"Pushed"` / `"Queued"` / `"Invoice Pending"` / `"Invoice Generated"` / `"Cancelled"` |
| `pushed_at` | Datetime | When the push completed |
| `cancelled_at` | Datetime | When the cancellation was acknowledged by EE (null otherwise) |
| `last_polled_at` | Datetime | Last Get All Orders poll that touched this record (for reconciliation) |
| `payload_hash` | Data | Hash of the request payload for audit |

## Local stock reservation (Phase 1)

ERPNext v16's Stock Reservation Entry (SRE) mechanism is used as-is. On SO submission, ERPNext-native logic creates SREs against the SO per Stock Settings configuration. The integration does not interfere.

**Operational gap noted in design:** ERPNext-side reservation can lag EE-side state. If EE rejects the order asynchronously (out of stock, customer credit issue, etc.), ERPNext's reservation remains until the polling fallback reconciles. Acceptable trade-off given EE doesn't expose reservation state.

**Architectural note for Phase 2 evolution:** when EE introduces reservation visibility API (currently "in discussion" per EE tech), the reservation mirror layer should plug in cleanly without restructuring §11. Keep reservation logic isolated; don't bake assumptions about reservation being purely local into deep parts of the architecture.

## ERPNext-initiated cancellation (Phase 1)

Allowed only **before invoice generation** per EE's confirmation. After Generate Invoice fires, cancellation must go through EE's own cancellation flow.

### Endpoint and payload (grounded against EE documentation)

```
POST {{BaseURL}}/orders/cancelOrder
Headers:
  x-api-key: <mandatory>
  Authorization: Bearer <Jwt_Token>
Body:
  { "reference_code": "<SO name>" }

Response:
  { "code": 200, "message": "Successfully Cancelled the Order with reference_code <SO name>", "data": [] }
```

EE accepts cancellation via one of three identifiers (`reference_code`, `invoice_id`, or `suborder_num`); the integration always uses `reference_code` = SO name (= the `orderNumber` we sent at createOrder). Three reasons:

1. **Works for both Old and New B2B uniformly.** No conditional logic.
2. **Available immediately at SO submission.** No dependency on EE's response identifiers.
3. **Avoids the New B2B identifier-correlation gap.** Even before EE confirms how New B2B identifiers are delivered, cancellation by reference_code works because we own the reference.

### Implementation

```python
def cancel_b2b_order_from_erpnext(so):
    """Cancel an §11-pushed B2B order from ERPNext side.

    Allowed only while ecs_b2b_so_map.status is 'Pushed' or 'Queued'.
    Refused if status is 'Invoice Generated' or 'Cancelled'.
    """
    so_map = get_b2b_order_map(so)
    if so_map.status in ("Invoice Generated", "Cancelled"):
        raise CancellationError(
            "Cannot cancel B2B order after invoice generation. "
            "Use EE's cancellation flow or contact EE support."
        )

    ee_account = so_map.easyecom_account_doc
    response = post_to_ee(
        endpoint="/orders/cancelOrder",
        payload={
            "reference_code": so.name,  # = orderNumber sent at createOrder
        },
        ee_account=ee_account,
    )

    if response.get("code") != 200:
        raise CancellationFailedError(response.get("message", "Unknown EE error"))

    # Update map
    so_map.status = "Cancelled"
    so_map.cancelled_at = now()
    so_map.save()

    # Release ERPNext-side reservation via standard ERPNext mechanism
    release_stock_reservation(so)

    # SO cancellation: leave to user discretion (accounting implications).
    # The integration marks the §11 push as cancelled; user decides whether
    # to cancel the SO entirely or keep it for credit-terms reconciliation.
```

## Polling fallback — Get All Orders (Phase 1)

Periodic reconciliation between ERPNext's view of pushed B2B orders and EE's actual state. Catches:

- Orders rejected/cancelled by EE that we missed Cancel Order webhook delivery for (Phase 2 webhook may not always arrive)
- Orders that have progressed to invoice generation that we missed Generate Invoice webhook delivery for (Phase 2 webhook may not always arrive)
- New B2B's identifier correlation, if EE confirms polling is the mechanism for that

### Polling cadence

`[OPEN: rate limits — pending EE clarification]`

Default proposal: every 5 minutes per EE Account, configurable. Hits Get All Orders, paginates if needed (per General FAQ 1, paginated responses use Next page URL as cursor).

### Reconciliation logic

```python
def reconcile_b2b_orders(ee_account):
    """Periodic polling reconciliation against EE."""
    pending_maps = frappe.get_all(
        "EasyEcom B2B Order Map",
        filters={
            "easyecom_account": ee_account.name,
            "status": ["in", ("Pushed", "Queued", "Invoice Pending")],
        },
        pluck="name",
    )
    if not pending_maps:
        return

    # Fetch current EE state for these orders
    # Get All Orders endpoint — filter by orderNumber range or by reference
    response = get_from_ee(
        endpoint="/orders/getAllOrders",  # [OPEN: exact endpoint path — pending EE]
        params={"orderNumbers": [...]},   # [OPEN: filter format — pending EE]
        ee_account=ee_account,
    )

    for ee_order in response["data"]:
        local_map = match_local_map_by_order_number(ee_order["reference_code"])
        if not local_map:
            continue

        # Reconcile state divergences
        if local_map.module == "New B2B" and not local_map.ee_order_id:
            # New B2B identifier correlation: fill in IDs from polling
            update_map_identifiers(local_map, ee_order)

        if local_map.status == "Pushed" and ee_order["status"] == "Cancelled":
            # We missed Cancel Order webhook
            handle_polling_detected_cancellation(local_map, ee_order)

        if local_map.status in ("Pushed", "Queued") and ee_order["status"] == "Invoice Generated":
            # We missed Generate Invoice webhook (Phase 2 concern)
            local_map.status = "Invoice Pending"  # downgrade to "needs SI mirror"
            local_map.save()
            # Phase 2: trigger SI mirror replay


def handle_polling_detected_cancellation(local_map, ee_order):
    """EE cancelled the order, we missed the webhook."""
    local_map.status = "Cancelled"
    local_map.save()

    so = frappe.get_doc("Sales Order", local_map.parent)
    release_stock_reservation(so)

    # Raise Integration Discrepancy for FDE visibility
    create_discrepancy(
        kind="B2B order cancelled by EE — polling-detected",
        severity="Warning",
        so_name=so.name,
        ee_order_id=local_map.ee_order_id,
        flag_reason="Cancel Order webhook delivery missed; reconciled via polling.",
    )
```

## FDE Worklist surfaces (Phase 1)

New §17 workspace cards:

| Card | Query | Action |
| --- | --- | --- |
| "B2B orders awaiting invoice generation" | Maps with status `"Pushed"` or `"Queued"` for > N hours | Visibility into stuck orders; FDE investigates if too old |
| "B2B order push failures" | Recent Integration Discrepancies with kind containing `"B2B"` | Triage push errors; usually missing master sync |
| "B2B polling-detected cancellations" | Recent Discrepancies kind `"B2B order cancelled by EE — polling-detected"` | Acknowledge after verifying with EE |
| "New B2B orders missing IDs" | Maps where `module="New B2B"` and `ee_order_id IS NULL` for > N hours | If consistent, indicates identifier correlation is failing |

## Branch chip UX (Phase 1)

Following the §10 pattern (DN form branch-prediction chip), the SO form gets a chip predicting which §11 branch will fire:

- **No branch** — set_warehouse not EE-mapped or empty. SO is purely ERPNext.
- **§11 → Old B2B** (color: blue) — set_warehouse EE-mapped, EE Account configured for Old B2B.
- **§11 → New B2B** (color: green) — set_warehouse EE-mapped, EE Account configured for New B2B.

Plus the explanation text in the SO form under set_warehouse: "When submitted, this SO will push to EE as a Business Order. Module: {ecs_b2b_module}. E-way origination: {ecs_eway_origination}."

## Failure modes (Phase 1)

| Failure | Recovery |
| --- | --- |
| Gate 0 not met (set_warehouse empty or non-EE-mapped) | Silent inert — SO is purely ERPNext |
| ecs_b2b_module not configured | REFUSED at on_submit with clear error |
| Mixed-warehouse SO submitted | REFUSED at on_submit |
| Customer/item not synced to EE | REFUSED at on_submit (precondition) |
| GSTIN missing on customer (Old B2B) | REFUSED at on_submit |
| createOrder HTTP error (network/auth) | Push job retries with back-off; persistent failures alert FDE |
| createOrder returns non-200 code | Push job stops; Integration Discrepancy raised; FDE triages |
| createOrder accepted but order rejected later (async, both modules) | Detected via Get All Orders polling; FDE worklist surfaces; reservation released |
| ERPNext-side reservation diverges from EE state | Polling reconciles; SRE released if EE cancelled |
| Cancellation attempted after invoice generation | REFUSED with clear error to user |
| `/orders/cancelOrder` HTTP error | Retry with back-off; persistent failures alert FDE |

## Phase 2 design sketch (NOT for build — pending EE clarification)

This section captures the design intent for Phase 2 so it's preserved across the gap. **Do not build from this section until EE answers the listed `[OPEN]` items.**

### Phase 2 dependencies on EE answers

1. **Generate Invoice webhook payload sample.** Without this, the SI mirror receiver design is speculative.
2. **Generate Invoice webhook signature — same for Old vs New?** Determines whether one handler or two.
3. **Custom GSP endpoint contract** — what URL ERPNext exposes, what request payload EE sends, what response shape EE expects, what timeout, how EE authenticates, whether EE includes a request ID for idempotency.
4. **Cancel Order webhook payload.** Determines the EE-initiated cancel handler signature.
5. **Mark Return mechanism.** Webhook? Pull? Manual? Determines whether §11 has a return-handler at all.
6. **Credit note ownership.** Determines what doctype gets created on return.
7. **Webhook idempotency mechanism.** Determines whether we need to build our own deduplication or use EE's tokens.

### Phase 2 architecture sketch

**Generate Invoice receiver (Branch X — EE-generated e-way):**

```
EE webhook arrives → handler:
  1. Resolve EE OrderID to local SO via ecs_b2b_so_map
  2. Mirror as Sales Invoice (Draft):
     - Lines from EE payload
     - Tax breakdown from EE payload
     - IRN from EE payload
     - e-way bill from EE payload
  3. Variance check: SI total vs SO total, tolerance %
     - Within tolerance → ready for user submit
     - Outside tolerance → Integration Discrepancy raised
  4. Stock movement on SI submit releases SRE
```

**Custom GSP receiver (Branch Y — ERPNext-generated e-way):**

```
EE calls ERPNext's Custom GSP endpoint (sync) → handler:
  1. Receive invoice payload from EE
  2. Idempotency cache check by EE's request ID
     (if seen, return cached response without re-generating IRN)
  3. Generate IRN via India Compliance
  4. Generate e-way bill via India Compliance
  5. Return IRN + e-way to EE in sync response
  6. Cache the response by request ID for retry idempotency

(Separately, Generate Invoice webhook fires after; SI mirror logic per Branch X)
```

**EE-initiated cancellation receiver:**

```
Cancel Order webhook arrives → handler:
  1. Resolve EE OrderID to local SO
  2. Release ERPNext-side reservation
  3. Update ecs_b2b_so_map.status = "Cancelled"
  4. Notify user (Comment on SO, ToDo)
  5. SO cancellation: leave to user discretion (accounting implications)
```

**Returns handler:**

```
[OPEN: depends entirely on EE's "Mark Return" mechanism]
```

### Phase 2 critical design notes

**Branch Y latency considerations:** EE's Custom GSP call is synchronous; EE waits for our response within some timeout. India Compliance → GSTN can take 2-5 seconds (and can spike). If EE's timeout is tight:
- **Risk:** EE retries before we respond → duplicate GSTN calls → duplicate IRNs → duplicate fees → invalid invoices
- **Mitigation:** Idempotency cache by EE-provided request ID (or by invoice ID if no request ID exists). Cache returns the same IRN without re-calling GSTN.

This is the single biggest implementation risk in Phase 2.

**Variance check tolerance:** When EE generates the invoice, line totals and tax may differ from SO due to rounding, GSTN tax recomputation, or item substitution. The variance threshold determines when ERPNext flags an Integration Discrepancy. Original spec said >1%. With EE-led invoice now being the only mode, this matters more.

`[OPEN: variance tolerance % — design decision pending; lean toward 1% with per-line and per-tax breakdown so FDE can see *what* varied, not just *that* it varied]`

## Carry-forwards from this packet to build phase

**Phase 1 build can proceed against this packet with:**
- All payload fields grounded against EE's documented createOrder API
- All preconditions and refusals locked
- All configuration fields specified
- All UX surfaces designed

**Phase 1 carry-forwards (must resolve during build):**
- Old B2B `paymentGateway` — confirm whether empty string is acceptable, or whether the field must be omitted entirely. Test against Harmony during Stage 1 build.
- Latitude/longitude — confirm Old B2B accepts shipping block without lat/long (sample shows them present but docs mark them optional). Test against Harmony during Stage 1 build.
- Get All Orders exact endpoint path and filter format. Test against Harmony / Puresta during Stage 2 build.

**Phase 1 carry-forwards (deferred to operational):**
- mmpl16 deploy verification once Phase 1 ships
- First real client SO push verification on Harmony / Puresta Test location
- Polling cadence tuning based on observed rate limits

**Phase 2 carry-forwards (EE clarification — not for Phase 1 build):**
- Generate Invoice webhook payload sample + signature
- Custom GSP endpoint contract
- Cancel Order webhook payload
- Mark Return mechanism + credit note ownership
- Webhook idempotency
- Rate limits

## Discipline lessons applied from §9 and §10

- **Ground decisions in real EE payloads before locking design.** §11's pre-build spec assumed an `inventory.reserved` webhook that doesn't exist. Same class of failure as §10's pre-build "4-flavour Stock Entry" model. The EE clarification round in this packet is the §11 equivalent of §10's Stage 0 discovery work.
- **Stage-by-stage delivery.** Phase 1 ships and is live-verified before Phase 2 design is locked. Phase 2 design has explicit gating on EE clarification; if EE answers within Phase 1's build window, Phase 2 starts immediately; if not, Phase 1 ships standalone.
- **Refuse rather than silently transform.** Mixed-warehouse SOs, missing GSTIN (Old B2B), unsynced customer or items — all refused at on_submit with clear errors. No silent splits, no silent fallbacks. Same discipline as §10's "set_warehouse empty / mixed → refuse."
- **Trace diagnostic from day one.** Phase 1 ships a `trace_b2b_so` diagnostic following the §10 `trace_dn` and §8d `trace_item` pattern. Whitelisted endpoint that walks every gate + downstream artefact for a single SO, returns a verdict (failed-gate / Pushed / Queued / Cancelled / etc.).
- **Test discipline.** Phase 1 tests include end-to-end state propagation checks — not just "payload built correctly" but "after push, ecs_b2b_so_map row exists with correct status." Lesson from §10 where unit tests mocked SI back-link inputs and missed the production bug.
- **End-to-end live verification on Harmony (Old B2B) + Puresta Test (New B2B) before closeout.** Same as §10's Live Integration Smoke; corrective commits expected before claiming Phase 1 complete.
