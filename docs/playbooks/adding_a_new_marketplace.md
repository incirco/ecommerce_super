# Playbook: Adding a New Marketplace

**When to use this:** A new marketplace comes onstream and the methodology team has approved it for the standard library. Examples: adding Tata CLiQ when it didn't ship in the v0.1 standard set, adding Ajio for fashion clients, adding a niche category-specific marketplace.

**Pre-flight check:** Has the methodology team signed off? Adding a marketplace is a methodology decision, not an engineering decision. The Account Role Map entries, GST disposition, and rate card require methodology team review before any code change.

---

## What "adding a marketplace" actually involves

Three layers, each independent and each required:

1. **Master data**: a `Marketplace` record (and its `Marketplace Channel` children) exist as fixtures
2. **Business rules**: Account Role Map entries map this marketplace's settlement events to the standard chart of accounts
3. **Per-tenant configuration**: each Frappe Company that uses this marketplace creates a `Marketplace Account` record

Layer 1 is parent-app fixtures. Layer 2 is parent-app fixtures (with possible client overrides). Layer 3 is per-deployment FDE work.

---

## The 8-step workflow

### 1. Confirm methodology sign-off

Before any code, the methodology team must have produced:

- The marketplace's fee schedule across all categories (commission, closing, shipping forward/reverse, storage, pick-pack, penalties)
- GST treatment for each fee category — is ITC claimable? Reverse charge applicable?
- Rate card entries (versioned, effective_from date)
- Any per-marketplace quirks documented (e.g., "Meesho doesn't charge commission on RTO; Flipkart does")
- Account Role Map entries with regex patterns or exact-match terms for each fee type

If any of these is missing, **stop**. Surface to the user that methodology sign-off is incomplete.

### 2. Add the Marketplace fixture

File: `apps/ecommerce_super/ecommerce_super/fixtures/marketplace.json`

```json
[
  ...,
  {
    "doctype": "Marketplace",
    "marketplace_name": "Tata CLiQ",
    "marketplace_code": "TATA_CLIQ",
    "country": "India",
    "is_active": 1
  }
]
```

`marketplace_code` is the stable identifier code uses; it's snake_case-uppercase, matches a Python identifier shape (no spaces, no special characters), and never changes once published.

### 3. Add Marketplace Channels

File: `apps/ecommerce_super/ecommerce_super/fixtures/marketplace_channel.json`

Channels are sub-types within a marketplace (FBA vs FBF on Amazon, B2B vs B2C splits, etc.). For a new marketplace, list the channels its sellers actually use:

```json
[
  ...,
  {
    "doctype": "Marketplace Channel",
    "marketplace_name": "Tata CLiQ",
    "channel_name": "B2C Standard",
    "channel_code": "B2C_STD",
    "is_active": 1
  },
  {
    "doctype": "Marketplace Channel",
    "marketplace_name": "Tata CLiQ",
    "channel_name": "Tata CLiQ Luxury",
    "channel_code": "LUXURY",
    "is_active": 1
  }
]
```

### 4. Add Account Role Map entries

The Account Role Map is the single most consequential part of adding a marketplace. Errors here produce silently mis-posted GL entries.

File: `apps/ecommerce_super/ecommerce_super/fixtures/account_role_map.json`

For each fee type the marketplace charges, add a mapping. Example:

```json
[
  ...,
  {
    "marketplace": "Tata CLiQ",
    "event_pattern": "^Marketplace Commission$",
    "matcher_type": "Regex",
    "target_account": "Marketplace Commission",
    "side": "Dr",
    "gst_treatment": "ITC Claimable",
    "rcm_applicable": 0,
    "notes": "Standard commission line on settlement file"
  },
  {
    "marketplace": "Tata CLiQ",
    "event_pattern": "Forward Shipping",
    "matcher_type": "Substring",
    "target_account": "Marketplace Shipping Fees",
    "side": "Dr",
    "gst_treatment": "ITC Claimable"
  },
  {
    "marketplace": "Tata CLiQ",
    "event_pattern": "Late Shipment Penalty",
    "matcher_type": "Substring",
    "target_account": "Marketplace Penalty Fees",
    "side": "Dr",
    "gst_treatment": "Not Claimable",
    "notes": "Penalty income to Tata CLiQ; ITC not eligible per BRD Section 3.2"
  }
]
```

Cover at minimum:
- Order amount (the credit leg)
- Commission, shipping (forward & reverse), storage, pick-pack, closing fees
- All penalty types
- TCS and TDS deductions
- Refund amounts (return reversals)

The matcher_type vocabulary follows `SPEC.md` Section 27.2 — Substring, Regex, JSON Path, Compound. Use Substring when possible (fastest and most readable); Regex when patterns vary.

### 5. Add Rate Card entries

File: `apps/ecommerce_super/ecommerce_super/fixtures/rate_card_library.json`

Versioned rate card entries by (marketplace, channel, category):

```json
[
  ...,
  {
    "doctype": "Rate Card Entry",
    "marketplace": "Tata CLiQ",
    "channel": "B2C Standard",
    "category": "Apparel",
    "effective_from": "2026-05-01",
    "effective_to": null,
    "commission_rate_pct": 17.5,
    "closing_fee_inr": 25,
    "shipping_forward_inr": 49,
    "shipping_reverse_inr": 0,
    "storage_per_cu_ft_per_day_inr": 1.5,
    "pick_pack_fee_inr": 8,
    "tcs_rate_pct": 1.0,
    "tds_rate_pct": 1.0,
    "fee_gst_rate_pct": 18,
    "notes": "Initial rate card from methodology team review 2026-04-22"
  }
]
```

### 6. Add Error Translation entries (if marketplace returns distinct errors)

If this marketplace's payloads come through EasyEcom but include marketplace-specific error codes, add Error Translation entries (per `SPEC.md` Section 27):

```json
[
  ...,
  {
    "doctype": "EasyEcom Error Translation",
    "error_key": "TATA_CLIQ_INVALID_BRAND",
    "matcher_type": "Substring",
    "matcher_pattern": "Brand not registered with Tata CLiQ",
    "title": "Brand not registered with Tata CLiQ",
    "explanation": "Tata CLiQ requires brand registration before products can be listed. The Item's brand_name is not in their registered list.",
    "suggested_actions": [
      {"action_text": "Confirm brand registration with Tata CLiQ category manager"},
      {"action_text": "Update Item's brand_name to match exactly"}
    ],
    "confidence": "Confirmed"
  }
]
```

### 7. Add tests

File: `apps/ecommerce_super/ecommerce_super/tests/integration/test_marketplace_<marketplace_code>.py`

Minimum coverage:

```python
class TestTataCliqMarketplace(FrappeTestCase):
    def test_marketplace_master_loaded(self):
        mp = frappe.get_doc("Marketplace", "Tata CLiQ")
        self.assertEqual(mp.marketplace_code, "TATA_CLIQ")
        self.assertTrue(mp.is_active)

    def test_account_role_map_commission(self):
        # Given a settlement-file-shaped event, the resolver returns the right account
        from ecommerce_super.recon.resolver import resolve_account
        result = resolve_account(
            marketplace="Tata CLiQ",
            event_description="Marketplace Commission",
        )
        self.assertEqual(result["target_account"], "Marketplace Commission")
        self.assertEqual(result["gst_treatment"], "ITC Claimable")

    def test_rate_card_lookup_for_apparel(self):
        from ecommerce_super.recon.forecasting import get_rate_card
        rc = get_rate_card(
            marketplace="Tata CLiQ",
            channel="B2C Standard",
            category="Apparel",
            on_date=frappe.utils.getdate("2026-05-15"),
        )
        self.assertEqual(rc["commission_rate_pct"], 17.5)

    def test_unmapped_event_falls_to_other_charges(self):
        # An event not matching any rule should fall through to "Marketplace Other Charges"
        # AND raise an Unclassified Charge Discrepancy
        from ecommerce_super.recon.resolver import resolve_account
        result = resolve_account(
            marketplace="Tata CLiQ",
            event_description="Surprise Charge No Rule For This",
        )
        self.assertEqual(result["target_account"], "Marketplace Other Charges")
        self.assertTrue(result["raises_discrepancy"])
        self.assertEqual(result["discrepancy_type"], "Unclassified Charge")
```

### 8. Methodology team review of test fleet

After fixtures load on a sample FDE-fleet client, the methodology team validates:

- Real settlement files from this marketplace, processed through the recon engine, produce expected forecasts within tolerance
- The "Marketplace Other Charges" balance after a month of actual operation is below 0.5% of total fees for this marketplace (the methodology guardrail KPI from BRD Section 2.5)
- All eight Discrepancy types behave correctly when triggered

If validation passes, the marketplace moves from Pilot to Default per the methodology lifecycle (BRD Section 10).

---

## Per-client onboarding (after marketplace is in standard library)

When a specific client adds this marketplace to their deployment, the FDE:

1. Creates a `Marketplace Account` record per (Company, Marketplace, Channel) the client uses
2. Configures `seller_id` and `ee_company_id` from the client's EasyEcom marketplace integration
3. Selects the `default_pseudo_customer` and `default_marketplace_warehouse` per `SPEC.md` Section 8 (B2C sales flow setup)
4. Optionally configures `finance_email` for high-impact alert escalation
5. Reviews any per-client rate card overrides (rare) and adds them to the client app's `rate_card_overrides.json`

This is FDE work, not Claude Code work. But it's worth knowing about — Claude Code may be asked to scaffold the client app's overrides folder during initial deployment.

---

## Common mistakes

### Adding a marketplace without methodology sign-off

Symptoms: incorrect Account Role Map mapping → wrong GL postings → silently corrupted books-of-record. The cost of fixing this retroactively is huge.

If the user asks Claude Code to add a marketplace without methodology sign-off, push back: "I see methodology team sign-off is missing. Adding a marketplace without it risks bad GL postings. Should I proceed anyway, or wait for sign-off?"

### Treating "marketplace_name" as case-insensitive

Frappe is case-sensitive. "Tata CLiQ" and "Tata Cliq" are different DocType records. Pick the canonical form once (consult methodology team) and stick to it.

### Hardcoding rate card values into Python

Rate cards are versioned data, not code. They live in fixtures. The recon engine reads them via `get_rate_card()` (which looks up the version effective at order time). Never write `if marketplace == 'Tata CLiQ': commission_rate = 17.5`.

### Adding event_pattern entries that overlap

If two patterns match the same settlement event description, the methodology has ambiguity. Order matters in `account_role_map.json` (first match wins), but this is fragile. Better: make patterns mutually exclusive. If two patterns must overlap, set explicit `priority` field.

### Forgetting to add the Marketplace Other Charges guardrail check

Every settlement event the recon engine sees should match a rule. If it falls through to "Marketplace Other Charges," that's a Discrepancy. The flow code that resolves accounts must always invoke the Discrepancy raiser when falling through. **Never silently route to "Marketplace Other Charges" without a Discrepancy.**

---

## When you're done

- Marketplace, Marketplace Channel, Marketplace Account fixtures present
- Account Role Map entries cover all standard fee types for this marketplace
- Rate Card Library entries published with effective_from date
- Error Translation entries added if marketplace has distinct error patterns
- Tests at integration level confirm fixture loading, account resolution, rate card lookup, and unmapped-event behaviour
- Methodology team has signed off on the values (separate from your code review)

In your chat response, name the marketplace, the channels, and confirm the methodology team's sign-off was in place before you started.
