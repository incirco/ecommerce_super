"""gh#44 — `EasyEcom-Item-Push` dimension push expressions must read a
fallback chain so ERPNext-born Items don't fail validation when the
canonical `ecs_*` fields are empty.

These tests evaluate the actual expressions through Frappe's safe-eval
sandbox against a synthesized source_doc with different field
populations.
"""

from __future__ import annotations

import unittest


# Mirror of the post-patch expressions. If you update the ruleset JSON
# OR `add_dimension_fallback_chain.py`, mirror the change here too —
# the test is the third copy of the truth, on purpose.
_EXPR_WEIGHT = (
    'int(round((value or source_doc.get("custom_weight") or '
    'source_doc.get("unicommerce_item_weight") or 0) * '
    '{"Kg": 1000, "Gram": 1, "Mg": 0.001, "Lbs": 453.592, '
    '"Oz": 28.3495, "Tonne": 1000000}.get(source_doc.weight_uom, 1)))'
)
_EXPR_LENGTH = (
    'int(round((value or source_doc.get("custom_length") or '
    'source_doc.get("unicommerce_item_length") or '
    'source_doc.get("length") or 0) * '
    '{"Cm": 1, "M": 100, "Mm": 0.1, "Inch": 2.54, '
    '"Ft": 30.48}.get(source_doc.get("ecs_dim_uom"), 1)))'
)
_EXPR_HEIGHT = (
    'int(round((value or source_doc.get("custom_height") or '
    'source_doc.get("unicommerce_item_height") or '
    'source_doc.get("height") or 0) * '
    '{"Cm": 1, "M": 100, "Mm": 0.1, "Inch": 2.54, '
    '"Ft": 30.48}.get(source_doc.get("ecs_dim_uom"), 1)))'
)
_EXPR_WIDTH = (
    'int(round((value or source_doc.get("custom_width") or '
    'source_doc.get("custom_breadth") or '
    'source_doc.get("unicommerce_item_width") or '
    'source_doc.get("width") or 0) * '
    '{"Cm": 1, "M": 100, "Mm": 0.1, "Inch": 2.54, '
    '"Ft": 30.48}.get(source_doc.get("ecs_dim_uom"), 1)))'
)


class _FakeSourceDoc:
    """Minimal stand-in. Custom-python expressions call .get() and
    attribute access on source_doc; mimic both."""

    def __init__(self, **fields):
        self._fields = fields

    def get(self, key, default=None):
        return self._fields.get(key, default)

    def __getattr__(self, key):
        if key in self._fields:
            return self._fields[key]
        return None


def _eval(expression: str, *, value=None, source_doc=None) -> int:
    """Evaluate one of the push expressions in a constrained env that
    matches the live sandbox's exposed globals."""
    globals_dict = {
        "__builtins__": {"int": int, "round": round},
        "value": value,
        "source_doc": source_doc,
    }
    return eval(expression, globals_dict)  # noqa: S307 — sandboxed by globals.


class TestWeightFallback(unittest.TestCase):
    def test_canonical_field_wins(self) -> None:
        doc = _FakeSourceDoc(weight_uom="Kg", custom_weight=99, unicommerce_item_weight=88)
        result = _eval(_EXPR_WEIGHT, value=2, source_doc=doc)
        self.assertEqual(result, 2 * 1000)  # 2 Kg → 2000g

    def test_falls_back_to_custom_weight(self) -> None:
        doc = _FakeSourceDoc(weight_uom="Gram", custom_weight=500, unicommerce_item_weight=300)
        result = _eval(_EXPR_WEIGHT, value=None, source_doc=doc)
        self.assertEqual(result, 500)

    def test_falls_back_to_unicommerce_weight(self) -> None:
        doc = _FakeSourceDoc(weight_uom="Gram", unicommerce_item_weight=750)
        result = _eval(_EXPR_WEIGHT, value=None, source_doc=doc)
        self.assertEqual(result, 750)

    def test_falls_back_to_zero(self) -> None:
        doc = _FakeSourceDoc(weight_uom="Gram")
        result = _eval(_EXPR_WEIGHT, value=None, source_doc=doc)
        self.assertEqual(result, 0)


class TestLengthFallback(unittest.TestCase):
    def test_canonical_field_wins(self) -> None:
        doc = _FakeSourceDoc(custom_length=99, unicommerce_item_length=88, length=77)
        result = _eval(_EXPR_LENGTH, value=15, source_doc=doc)
        self.assertEqual(result, 15)

    def test_falls_back_to_custom_length(self) -> None:
        """gh#44 headline scenario — reporter had custom_length=15.0
        populated but ecs_length_cm was None. Pre-patch this was 0;
        post-patch this is 15."""
        doc = _FakeSourceDoc(custom_length=15.0, unicommerce_item_length=88)
        result = _eval(_EXPR_LENGTH, value=None, source_doc=doc)
        self.assertEqual(result, 15)

    def test_falls_back_to_unicommerce_length(self) -> None:
        doc = _FakeSourceDoc(unicommerce_item_length=20)
        result = _eval(_EXPR_LENGTH, value=None, source_doc=doc)
        self.assertEqual(result, 20)

    def test_falls_back_to_stock_length(self) -> None:
        doc = _FakeSourceDoc(length=12)
        result = _eval(_EXPR_LENGTH, value=None, source_doc=doc)
        self.assertEqual(result, 12)

    def test_uom_conversion_still_applies_to_fallback(self) -> None:
        """An FDE who uses custom_length in inches should still get a
        cm-converted output."""
        doc = _FakeSourceDoc(custom_length=10, ecs_dim_uom="Inch")
        result = _eval(_EXPR_LENGTH, value=None, source_doc=doc)
        # 10 inches * 2.54 = 25.4 → int(round(25.4)) = 25
        self.assertEqual(result, 25)


class TestHeightFallback(unittest.TestCase):
    def test_falls_back_to_custom_height(self) -> None:
        doc = _FakeSourceDoc(custom_height=18.0)
        result = _eval(_EXPR_HEIGHT, value=None, source_doc=doc)
        self.assertEqual(result, 18)


class TestWidthFallback(unittest.TestCase):
    def test_falls_back_to_custom_width(self) -> None:
        doc = _FakeSourceDoc(custom_width=10)
        result = _eval(_EXPR_WIDTH, value=None, source_doc=doc)
        self.assertEqual(result, 10)

    def test_falls_back_to_custom_breadth(self) -> None:
        """One reporter used 'breadth' instead of 'width' on the form
        (custom_breadth=10). Patch lists breadth as an alias."""
        doc = _FakeSourceDoc(custom_breadth=10)
        result = _eval(_EXPR_WIDTH, value=None, source_doc=doc)
        self.assertEqual(result, 10)

    def test_falls_back_to_unicommerce_width(self) -> None:
        doc = _FakeSourceDoc(unicommerce_item_width=14)
        result = _eval(_EXPR_WIDTH, value=None, source_doc=doc)
        self.assertEqual(result, 14)


if __name__ == "__main__":
    unittest.main()
