# Playbook: Adding a Field Mapping Rule

**When to use this:** You need to translate a new field between ERPNext and EasyEcom. Examples: a new Item attribute that EE wants in its payload, an EE response field we want to capture in ERPNext, a marketplace-specific field that needs conditional handling.

**Pre-flight check:** Read `SPEC.md` Section 20 (Path-based Field Mapping). The full engine spec is there. This playbook covers the "how do I add one rule" workflow.

---

## Decide first: parent app or client app?

| Question | If yes → | If no → |
| --- | --- | --- |
| Will any client need this rule? | Parent app | Client app |
| Is this for a specific client's odd payload variant? | Client app | Parent app |
| Is the field in EasyEcom's standard API surface? | Likely parent app | Likely client app |
| Has the methodology team approved this? | Use parent app fixture | Use client app fixture |

If unclear, ask the user. **Don't add client-specific rules to the parent app.**

---

## Workflow

### 1. Locate the right ruleset

Open `apps/ecommerce_super/ecommerce_super/fixtures/easyecom_field_mapping.json`. Field Mappings are organised per (entity_type, direction) ruleset. The library shipped is listed in `SPEC.md` Section 20.11.

Common rulesets:

- `EasyEcom-Item-Sync` (bidirectional)
- `EasyEcom-Customer-Sync` (bidirectional)
- `EasyEcom-Order-Pull` (pull-only — B2C marketplace orders)
- `EasyEcom-PO-Push` (push-only)
- `EasyEcom-GRN-Pull` (pull-only)

Find the ruleset your new rule belongs in. If the rule needs to participate in a child-table mapping (e.g., Item-UOM-Push for the UOM child table within Item), it goes in that child ruleset.

### 2. Choose the rule's shape

A Field Mapping Rule has these key fields (full schema in `SPEC.md` Section 20.3 / Appendix A.2.14):

```
erpnext_path:    "items[].cgst_amount"
easyecom_path:   "items[].tax_components[?type='CGST'].amount"
transform_push:  "identity"
transform_pull:  "identity"
condition:       "source_doc.customer_type == 'B2B'"  # optional
default_value:   ""                                    # if source absent
required_field:  0  # 1 if missing source must raise
notes:           "B2B-only; B2C uses pseudo-customer pool"
```

The path syntax is the JSONPath subset documented in `SPEC.md` Section 20.4. The transformer vocabulary is the closed set in Section 20.5.

### 3. Decide on the transformer

Closed vocabulary (full list in `SPEC.md` Section 20.5):

- `identity` — pass through, no change
- `bool_to_yn` / `yn_to_bool` — Python bool ↔ "Y"/"N" string
- `date_format` — date string reformat
- `currency_to_paise` / `paise_to_currency` — INR rupees ↔ paise
- `lookup_id` — Frappe doc name → EE-side ID
- `enum_map` — value-set translation (with `default`)
- `conditional_constant` — output a constant per condition
- `computed` — refer to a Computed Field
- `custom_python` — sandboxed expression (escape hatch; use sparingly)

Pick from the closed vocabulary first. **If your rule needs a transformer not in the vocabulary, this is a parent-app code change, not a fixture change** — pause and confirm with the user. The closed vocabulary is part of the methodology bet; expanding it requires methodology team review.

### 4. Add the rule to the fixture

Edit the appropriate ruleset entry. Example — adding a `gross_weight` field to `EasyEcom-Item-Sync` (bidirectional, identity translation):

```json
{
  "doctype": "EasyEcom Field Mapping",
  "mapping_name": "EasyEcom-Item-Sync",
  "entity_type": "Item",
  "direction": "Bidirectional",
  "active": 1,
  "missing_field_policy": "Permissive",
  "rules": [
    ...,
    {
      "erpnext_path": "weight_per_unit",
      "easyecom_path": "grossWeight",
      "transform_push": "identity",
      "transform_pull": "identity",
      "condition": "",
      "default_value": "",
      "required_field": 0,
      "notes": "Gross weight per unit; UOM determined by weight_uom field"
    }
  ]
}
```

Use 2-space indent. Sort rules within a ruleset by `erpnext_path` alphabetically — makes diffs readable.

### 5. Add a test

File: `apps/ecommerce_super/ecommerce_super/tests/unit/test_field_mapping_<entity>.py`

The test asserts that an input payload, run through the ruleset, produces the expected output.

```python
import frappe
from frappe.tests.utils import FrappeTestCase
from ecommerce_super.field_mapping.runner import apply_ruleset


class TestEasyEcomItemSyncMapping(FrappeTestCase):
    def setUp(self):
        # Field Mapping fixtures load via FrappeTestCase
        self.ruleset = frappe.get_doc("EasyEcom Field Mapping", "EasyEcom-Item-Sync")

    def test_gross_weight_passes_through_on_push(self):
        item_doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": "TEST-ITEM-001",
            "item_name": "Test Item",
            "weight_per_unit": 0.5,
            "weight_uom": "Kg",
        })
        # Don't insert; we just translate
        payload = apply_ruleset(self.ruleset, item_doc, direction="push")
        self.assertEqual(payload["grossWeight"], 0.5)

    def test_gross_weight_passes_through_on_pull(self):
        ee_payload = {
            "itemCode": "TEST-ITEM-001",
            "itemName": "Test Item",
            "grossWeight": 0.5,
        }
        result = apply_ruleset(self.ruleset, ee_payload, direction="pull")
        self.assertEqual(result["weight_per_unit"], 0.5)

    def test_gross_weight_missing_uses_default(self):
        # If we set a default_value of 0 and the source field is absent,
        # the output should be 0
        ...
```

### 6. Run the tests

```bash
bench --site <test_site> run-tests --module ecommerce_super.tests.unit.test_field_mapping_item
```

All tests must pass. If a test fails, **don't disable the test** — the test is enforcing the spec. Either the rule is wrong (likely) or the spec needs updating (rare).

### 7. Verify the fixture loads cleanly

```bash
bench --site <test_site> clear-cache
bench --site <test_site> migrate
```

The fixture loader will recreate the Field Mapping records. Check:
- The rule appears in the desk: navigate to "EasyEcom Field Mapping" list, find your ruleset, confirm the new rule is in the rules child table
- "Show Computed Mapping" action in the desk works and shows your rule
- "Test Mapping" action with a sample payload produces the expected output

### 8. Update the change reason

Field Mapping rulesets capture `change_reason` on every save (per `SPEC.md` Section 20.2). When you save the fixture and migrate, the on_update hook will create a Field Mapping Version snapshot. The change reason should explain *why* this rule was added — e.g., "Added gross_weight to support FBA shipping rate calculations" — not *what* — "Added gross_weight rule".

### 9. Consider downstream impact

A new Field Mapping rule may affect:

- **Schema drift detection** (`SPEC.md` Section 22): if the field was previously dropped, the Mapping Coverage Snapshot will show a change. That's expected.
- **Recon engine**: if the field is consumed by recon (e.g., a tax field), the recon engine's tests must also pass with the new field. Check `apps/ecommerce_super/ecommerce_super/tests/integration/test_recon_*.py`.
- **Field Mapping Coverage report**: the coverage % for this ruleset will increase. That's a good thing.

---

## Common mistakes

### Adding a rule that's already implicitly handled

If the source and target paths are identical (`item_code` ↔ `item_code`) and the transform is identity, **you don't need a rule**. The Permissive missing_field_policy handles this implicitly. Adding redundant rules clutters the ruleset.

The rule is needed when:
- Path differs (`item_code` ↔ `itemCode`)
- A transform is needed (`has_batch_no` ↔ `batchRequired` via bool_to_yn)
- A condition gates the rule
- The field is required (`required_field: 1`)

### Conflating mapping rules with business logic

A Field Mapping rule translates a single field. It doesn't decide *whether* to sync. It doesn't post a Journal Entry. It doesn't raise a Discrepancy. If your rule's `condition` is "synthesizing complex business logic," you're trying to do too much in one rule.

The Field Mapping engine is a translation layer, not a workflow engine. Workflow lives in `flows/`.

### Forgetting bidirectional symmetry

If a rule is `direction: "Bidirectional"`, both `transform_push` and `transform_pull` need to be set, and they must round-trip cleanly. A push of value `X` via `transform_push`, then a pull via `transform_pull`, should produce `X` again. **Test this.**

If round-trip would lose information, the direction should not be Bidirectional. Use Push-only or Pull-only.

### Using `custom_python` when a closed transformer fits

`custom_python` is the escape hatch. It works, but it's slower (sandbox overhead), harder to audit, and harder to test in isolation. If your need fits one of the closed-vocabulary transformers, use it.

---

## When you're done

- The new rule is in the fixture, sorted alphabetically within the ruleset
- A test exists that asserts the rule's behaviour in both directions (if Bidirectional) or the relevant direction
- All tests pass
- The fixture loads cleanly (`bench migrate` works)
- The desk's "Show Computed Mapping" and "Test Mapping" actions confirm correctness
- A Field Mapping Version snapshot was created with a meaningful change_reason

In your chat response, name the ruleset and the rule, and confirm the round-trip test passed if applicable.
