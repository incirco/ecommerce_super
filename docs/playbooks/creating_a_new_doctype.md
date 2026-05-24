# Playbook: Creating a New DocType

**When to use this:** You're adding a new DocType (Frappe data model) to the parent app or a client app.

**Pre-flight check:** Is the DocType already specified in `SPEC.md` Appendix A or Section 30.2? If yes, follow this playbook. If no, **stop** — adding a new DocType to the parent app requires a SPEC.md change first; that's not a code change, that's a design change. Surface to the user.

---

## The 9-step workflow

### 1. Locate or create the spec

Open `SPEC.md`. Find the section that specifies this DocType. The schema you'll implement comes from there — you don't invent fieldnames or types. If the section says "EasyEcom Sync Record has fields company, direction, entity_type, ...", that's the contract.

If the spec section uses pseudo-schema notation (the convention is `fieldname    Fieldtype    Y/N    options/notes`), translate it to actual Frappe DocType JSON.

### 2. Create the directory

For a parent-app DocType:

```
apps/ecommerce_super/ecommerce_super/ecommerce_super/doctype/<doctype_snake_case>/
```

For a child-app DocType (rare):

```
apps/ecommerce_super_<client>/ecommerce_super_<client>/ecommerce_super_<client>/doctype/<doctype_snake_case>/
```

Use `snake_case` for the directory name. The DocType *display name* is "Title Case With Spaces" (e.g., "EasyEcom Sync Record"); the internal `name` field uses the same; the directory is the snake_case version (`easyecom_sync_record`).

### 3. Create the JSON schema file

File: `<doctype_snake_case>.json`

Minimum structure:

```json
{
 "actions": [],
 "creation": "2026-05-05 12:00:00.000000",
 "doctype": "DocType",
 "engine": "InnoDB",
 "field_order": [
  "company",
  "entity_type",
  ...
 ],
 "fields": [
  {
   "fieldname": "company",
   "fieldtype": "Link",
   "label": "Company",
   "options": "Company",
   "reqd": 1,
   "in_list_view": 1
  },
  ...
 ],
 "index_web_pages_for_search": 0,
 "links": [],
 "modified": "2026-05-05 12:00:00.000000",
 "modified_by": "Administrator",
 "module": "Ecommerce Super",
 "name": "EasyEcom Sync Record",
 "naming_rule": "By \"Naming Series\" field",
 "owner": "Administrator",
 "permissions": [
  {
   "role": "System Manager",
   "read": 1, "write": 1, "create": 1, "delete": 1, "submit": 0, "cancel": 0
  },
  ...
 ],
 "sort_field": "modified",
 "sort_order": "DESC",
 "states": [],
 "track_changes": 1
}
```

Convert the SPEC.md pseudo-schema row-by-row:

| SPEC.md notation | JSON equivalent |
| --- | --- |
| `Data` | `"fieldtype": "Data"` |
| `Long Text` | `"fieldtype": "Long Text"` |
| `Link → Company` | `"fieldtype": "Link", "options": "Company"` |
| `Select`, options A / B / C | `"fieldtype": "Select", "options": "A\nB\nC"` |
| `required` | `"reqd": 1` |
| `unique` | `"unique": 1` |
| `in_list_view` | `"in_list_view": 1` |
| `read_only` | `"read_only": 1` |
| `default 0` | `"default": "0"` (note: always a string) |
| `depends_on: "eval:..."` | `"depends_on": "eval:..."` |

### 4. Create the controller (Python)

File: `<doctype_snake_case>.py`

Skeleton:

```python
import frappe
from frappe.model.document import Document
from ecommerce_super.exceptions import EasyEcomError  # if relevant


class EasyEcomSyncRecord(Document):  # CamelCase matches the DocType display name
    def validate(self):
        """Called before save (insert and update). Raise on bad state."""
        self._check_invariants()

    def before_save(self):
        """Called before write. Use for last-second normalisations."""
        pass

    def on_update(self):
        """Called after save. Use for side effects (audit, signal, cache invalidation)."""
        self._write_audit_row()
        self._invalidate_caches()

    def _check_invariants(self):
        # Guard rails specific to this DocType
        if self.sync_status == "Marked As Already Synced" and not self.override_reason:
            frappe.throw("override_reason required when marking as already synced")

    def _write_audit_row(self):
        # See playbook: writing_an_audit_entry.md
        pass

    def _invalidate_caches(self):
        # If this DocType participates in cached lookups
        pass

    @frappe.whitelist()
    def retry_now(self):
        """Action menu method. Re-enqueue the sync."""
        # ... implementation per SPEC.md Section 18.6.1
        pass
```

**Don't catch bare `Exception`.** Use the typed exceptions from `ecommerce_super.exceptions`. See `SPEC.md` Section 30.5 / Appendix C.

### 5. Create the form script (JavaScript) — only if needed

File: `<doctype_snake_case>.js`

Skeleton (only if the DocType has client-side behaviour):

```javascript
frappe.ui.form.on("EasyEcom Sync Record", {
    refresh(frm) {
        // Add action menu items per SPEC.md Section 18.6
        if (frm.doc.sync_status === "Failed") {
            frm.add_custom_button(__("Retry Now"), () => {
                frm.call("retry_now").then(r => {
                    if (r.message?.ok) {
                        frappe.show_alert({message: __("Re-enqueued"), indicator: "green"});
                        frm.reload_doc();
                    }
                });
            }, __("Actions"));
        }
    }
});
```

If the DocType has no client-side behaviour beyond standard Frappe form, skip this step.

### 6. Write the test file

File: `test_<doctype_snake_case>.py` (in the same directory as the DocType)

Minimum coverage:

```python
import frappe
from frappe.tests.utils import FrappeTestCase


class TestEasyEcomSyncRecord(FrappeTestCase):
    def test_insert_minimum(self):
        """Can insert with only required fields"""
        doc = frappe.get_doc({
            "doctype": "EasyEcom Sync Record",
            "company": "_Test Company",
            "direction": "Push to EE",
            "entity_type": "Item",
            "sync_status": "Pending",
            "correlation_id": "test-uuid-1",
        })
        doc.insert()
        self.assertEqual(doc.sync_status, "Pending")

    def test_validation_marked_as_synced_requires_reason(self):
        """Mark as Already Synced without reason should fail"""
        doc = frappe.get_doc({
            "doctype": "EasyEcom Sync Record",
            "company": "_Test Company",
            "direction": "Push to EE",
            "entity_type": "Item",
            "sync_status": "Marked As Already Synced",
            "correlation_id": "test-uuid-2",
        })
        with self.assertRaises(frappe.ValidationError):
            doc.insert()

    def test_retry_now_re_enqueues(self):
        """retry_now() resets state and creates a Queue Job"""
        # Insert a Failed Sync Record, call retry_now, assert state and side-effect
        pass

    # ... more tests
```

The test pyramid is in `SPEC.md` Section 15. **Don't ship a DocType without tests.**

### 7. Add to fixtures (only if shipped with seed data)

If this DocType ships with default rows (e.g., default Field Mappings, default SLA Budgets, default Error Translation entries), add to the parent app's `hooks.py`:

```python
fixtures = [
    # ... existing ...
    {"dt": "EasyEcom Sync Record"},  # ONLY if you're shipping seed rows
]
```

And create `apps/ecommerce_super/ecommerce_super/fixtures/easyecom_sync_record.json` with the seed rows.

**Do not ship operational data as fixtures.** Sync Records are operational data, not seed data — never add Sync Records to fixtures. (This example is illustrative; the right Sync Record fixture decision is "no, don't ship them.")

The fixture decision rule: if a row would naturally exist on every site at install time independent of any client's configuration, it's a fixture candidate. If it's something a deployment creates, it's not a fixture.

### 8. Run migrations to verify

```bash
bench --site <test_site> migrate
```

Expected output:
- New DocType created in the database
- New table `tabEasyEcom Sync Record` exists
- Fields match the JSON
- No migration errors

If migration errors:
- Read the error
- Most common cause: a Link field's `options` references a DocType that doesn't exist yet. Either ensure the target DocType is in the same app and migrates first, or use a Dynamic Link.
- Second most common: invalid Select options (Frappe is strict about newline-separated options; no commas, no extra whitespace).

### 9. Run the tests

```bash
bench --site <test_site> run-tests --module ecommerce_super.ecommerce_super.doctype.easyecom_sync_record.test_easyecom_sync_record
```

All tests must pass. Coverage of branching logic is mandatory.

---

## Common mistakes

- **Forgetting `in_list_view`** on enough fields that the list view is useful. The methodology has an opinion: status fields, key identifiers, and date fields should all be `in_list_view`. A list view that shows only the name column is insufficient.
- **Setting `read_only` on fields the user actually needs to edit** — distinguish fields the *system* fills (correlation_id, hashes, audit fields) from fields the *user* fills.
- **Forgetting permission rules.** If the DocType holds Company-scoped data, the permissions matrix in `SPEC.md` Appendix A.5 / Section 30.7 specifies the exact roles and rights. Never ship with `"role": "All", "read": 1` open access.
- **Creating both a child DocType and not setting `istable: 1`** in the JSON. Child DocTypes need this flag.
- **Inconsistent fieldname casing.** snake_case throughout. Always.

## When you're done

- Note in your chat response: "Created DocType X with N fields, T tests, M permissions. Migration runs clean. Tests pass."
- If you had to make a judgment call (e.g., "the spec said `Long Text` but the field is small enough that `Small Text` is more appropriate"), surface it.
- If the spec was ambiguous, surface what you assumed.

Don't say "implementation complete" until tests pass and migration runs.
