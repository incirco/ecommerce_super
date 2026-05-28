"""Unit tests for the §5.5 closed transformer vocabulary.

Each transformer is exercised: identity, type coercions, currency,
date/datetime, string, enum, conditional, computed, custom_python.
Args-contract validators are also tested.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from ecommerce_super.easyecom.exceptions import (
    FieldMappingCompileError,
    FieldMappingRuleError,
)
from ecommerce_super.easyecom.field_mapping import transformers as T


def _ctx(**kw) -> T.TransformContext:
    return T.TransformContext(direction=kw.pop("direction", "push"), **kw)


class TestRegistry(unittest.TestCase):
    def test_count_matches_spec_plus_compose(self) -> None:
        # 24 vocabulary entries + compose sentinel = 25. The SPEC §5.5 list
        # has been extended past the original 21: §9 Stage 1 added
        # lookup_field (the 3-arg generalisation of lookup_id /
        # reverse_lookup_id) to support Supplier-Map-mediated lookups.
        # Earlier extensions account for the remaining gap.
        self.assertEqual(len(T.TRANSFORMERS), 25)

    def test_unknown_transformer_raises(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            T.get_transformer("nonexistent")


class TestIdentityAndBoolean(unittest.TestCase):
    def test_identity(self) -> None:
        self.assertEqual(
            T.apply_transformer("identity", "x", args=None, context=_ctx()), "x"
        )
        self.assertIsNone(
            T.apply_transformer("identity", None, args=None, context=_ctx())
        )

    def test_bool_to_yn(self) -> None:
        self.assertEqual(
            T.apply_transformer("bool_to_yn", True, args=None, context=_ctx()), "Y"
        )
        self.assertEqual(
            T.apply_transformer("bool_to_yn", False, args=None, context=_ctx()), "N"
        )
        self.assertEqual(
            T.apply_transformer("bool_to_yn", 1, args=None, context=_ctx()), "Y"
        )
        self.assertEqual(
            T.apply_transformer("bool_to_yn", 0, args=None, context=_ctx()), "N"
        )

    def test_yn_to_bool(self) -> None:
        self.assertTrue(
            T.apply_transformer("yn_to_bool", "Y", args=None, context=_ctx())
        )
        self.assertFalse(
            T.apply_transformer("yn_to_bool", "N", args=None, context=_ctx())
        )
        self.assertIsNone(
            T.apply_transformer("yn_to_bool", "", args=None, context=_ctx())
        )


class TestStrings(unittest.TestCase):
    def test_str_lower(self) -> None:
        self.assertEqual(
            T.apply_transformer("str_lower", "ABC", args=None, context=_ctx()), "abc"
        )

    def test_str_upper(self) -> None:
        self.assertEqual(
            T.apply_transformer("str_upper", "abc", args=None, context=_ctx()), "ABC"
        )

    def test_str_strip(self) -> None:
        self.assertEqual(
            T.apply_transformer("str_strip", "  x  ", args=None, context=_ctx()), "x"
        )


class TestTypeCoercion(unittest.TestCase):
    def test_int_to_str(self) -> None:
        self.assertEqual(
            T.apply_transformer("int_to_str", 42, args=None, context=_ctx()), "42"
        )

    def test_str_to_int(self) -> None:
        self.assertEqual(
            T.apply_transformer("str_to_int", "42", args=None, context=_ctx()), 42
        )

    def test_str_to_int_invalid_raises(self) -> None:
        with self.assertRaises(FieldMappingRuleError):
            T.apply_transformer("str_to_int", "not-a-number", args=None, context=_ctx())

    def test_float_to_str(self) -> None:
        self.assertEqual(
            T.apply_transformer("float_to_str", 3.14, args=None, context=_ctx()), "3.14"
        )

    def test_str_to_float(self) -> None:
        self.assertEqual(
            T.apply_transformer("str_to_float", "3.14", args=None, context=_ctx()), 3.14
        )


class TestCurrency(unittest.TestCase):
    def test_rupees_to_paise(self) -> None:
        self.assertEqual(
            T.apply_transformer(
                "currency_to_paise", "123.45", args=None, context=_ctx()
            ),
            12345,
        )

    def test_rupees_to_paise_round_half_up(self) -> None:
        # 123.445 → 12344.5 → 12345 (banker's round-half-up)
        self.assertEqual(
            T.apply_transformer(
                "currency_to_paise", "123.445", args=None, context=_ctx()
            ),
            12345,
        )

    def test_paise_to_rupees(self) -> None:
        self.assertEqual(
            T.apply_transformer("paise_to_currency", 12345, args=None, context=_ctx()),
            Decimal("123.45"),
        )


class TestDateAndDatetime(unittest.TestCase):
    def test_date_format(self) -> None:
        self.assertEqual(
            T.apply_transformer(
                "date_format",
                "2024-12-31",
                args={"from": "YYYY-MM-DD", "to": "DD/MM/YYYY"},
                context=_ctx(),
            ),
            "31/12/2024",
        )

    def test_datetime_to_iso(self) -> None:
        self.assertEqual(
            T.apply_transformer(
                "datetime_to_iso",
                "2024-12-31 10:30:00",
                args=None,
                context=_ctx(),
            ),
            "2024-12-31T10:30:00",
        )

    def test_iso_to_datetime(self) -> None:
        from datetime import datetime as _dt

        result = T.apply_transformer(
            "iso_to_datetime", "2024-12-31T10:30:00", args=None, context=_ctx()
        )
        self.assertEqual(result, _dt(2024, 12, 31, 10, 30, 0))


class TestEnumAndConditional(unittest.TestCase):
    def test_enum_map_hit(self) -> None:
        self.assertEqual(
            T.apply_transformer(
                "enum_map",
                "paid",
                args={"map": {"paid": "PAID", "cod": "COD"}, "default": "OTHER"},
                context=_ctx(),
            ),
            "PAID",
        )

    def test_enum_map_default(self) -> None:
        self.assertEqual(
            T.apply_transformer(
                "enum_map",
                "missing-key",
                args={"map": {"paid": "PAID"}, "default": "OTHER"},
                context=_ctx(),
            ),
            "OTHER",
        )

    def test_enum_map_no_default_raises(self) -> None:
        with self.assertRaises(FieldMappingRuleError):
            T.apply_transformer(
                "enum_map",
                "missing",
                args={"map": {"a": "A"}},
                context=_ctx(),
            )


class TestComputedAndCustomPython(unittest.TestCase):
    def test_computed_reads_from_context(self) -> None:
        ctx = _ctx(computed_values={"total_with_tax": 999})
        self.assertEqual(
            T.apply_transformer(
                "computed", None, args={"name": "total_with_tax"}, context=ctx
            ),
            999,
        )

    def test_custom_python_value_times_two(self) -> None:
        self.assertEqual(
            T.apply_transformer(
                "custom_python",
                5,
                args={"expression": "value * 2"},
                context=_ctx(),
            ),
            10,
        )


class TestArgsContractValidator(unittest.TestCase):
    def test_date_format_missing_to_arg(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "date_format", {"from": "YYYY-MM-DD"}, rule_label="r"
            )

    def test_lookup_id_requires_two_args(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "lookup_id", {"doctype": "Item"}, rule_label="r"
            )

    def test_lookup_field_requires_three_args(self) -> None:
        """§9 Stage 1 — lookup_field needs doctype + filter_field +
        target_field. Missing any one fails compile."""
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "lookup_field",
                {"doctype": "EasyEcom Supplier Map", "filter_field": "erpnext_name"},
                rule_label="r",
            )
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "lookup_field",
                {"doctype": "EasyEcom Supplier Map", "target_field": "ee_vendor_id"},
                rule_label="r",
            )
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args("lookup_field", None, rule_label="r")

    def test_lookup_field_accepts_full_args(self) -> None:
        """Three-arg form passes validation."""
        T.validate_transformer_args(
            "lookup_field",
            {
                "doctype": "EasyEcom Supplier Map",
                "filter_field": "erpnext_name",
                "target_field": "ee_vendor_id",
            },
            rule_label="r",
        )

    def test_enum_map_map_must_be_dict(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "enum_map", {"map": "not-a-dict"}, rule_label="r"
            )

    def test_custom_python_validates_expression_at_compile(self) -> None:
        """SECURITY: a malicious expression in custom_python fails the
        ruleset compile, not run-time execution."""
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "custom_python",
                {"expression": "__import__('os').system('echo HACK')"},
                rule_label="r",
            )

    def test_conditional_constant_validates_each_when(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args(
                "conditional_constant",
                {
                    "conditions": [{"when": "frappe.X", "then": "X"}],
                    "default": "Y",
                },
                rule_label="r",
            )

    def test_unknown_transformer_rejected(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            T.validate_transformer_args("no-such-thing", None, rule_label="r")
