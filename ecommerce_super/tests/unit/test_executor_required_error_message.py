"""gh#13 — FieldMappingMissingRequiredError must name the source_path
and show the available source keys, not just the target field.

The original error ("Required rule N on 'X' produced no value at 'qty'")
made compose-call failures ambiguous — the FDE testing
EasyEcom-Order-Pull → EasyEcom-Order-Line-Pull couldn't tell whether
the mismatch was in their sample payload (wrong key — they used `qty`
when the ruleset's source path was `quantity`) or in the child ruleset
itself.

The enriched message includes both: source_path (what the rule was
LOOKING for) and available source keys (what was actually provided).
That collapses the three-way uncertainty in the issue to a single
glance.
"""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.field_mapping.executor import (
    _source_keys_hint,
)


class TestSourceKeysHint(unittest.TestCase):
    def test_dict_returns_bracketed_key_list(self) -> None:
        self.assertEqual(
            _source_keys_hint({"sku": "S1", "qty": 2}),
            "[sku, qty]",
        )

    def test_long_key_list_is_truncated(self) -> None:
        many = {f"k{i}": i for i in range(20)}
        hint = _source_keys_hint(many, limit=5)
        self.assertIn("[k0, k1, k2, k3, k4, …(15 more)]", hint)

    def test_none_source_explicitly_signalled(self) -> None:
        self.assertEqual(_source_keys_hint(None), "<no source provided>")

    def test_frappe_document_uses_as_dict(self) -> None:
        class _FakeDoc:
            def as_dict(self):
                return {"name": "X1", "item_code": "SKU-1"}
        self.assertEqual(
            _source_keys_hint(_FakeDoc()),
            "[name, item_code]",
        )

    def test_as_dict_failure_falls_back_to_type_name(self) -> None:
        class _BadDoc:
            def as_dict(self):
                raise RuntimeError("boom")
        result = _source_keys_hint(_BadDoc())
        self.assertEqual(result, "<_BadDoc>")

    def test_non_dict_non_doc_falls_back_to_type_name(self) -> None:
        self.assertEqual(_source_keys_hint(42), "<int>")
        self.assertEqual(_source_keys_hint("a string"), "<str>")
        self.assertEqual(_source_keys_hint([1, 2, 3]), "<list>")


if __name__ == "__main__":
    unittest.main()
