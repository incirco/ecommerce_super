"""Unit tests for the §5.4 JSONPath subset (path.py + utils/jsonpath.py)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError
from ecommerce_super.easyecom.field_mapping import path


class TestValidatePath(unittest.TestCase):
    def test_accepts_six_supported_syntaxes(self) -> None:
        for p in [
            "customer.gstin",  # dot
            "items[].sku",  # iteration
            "items[*].sku",  # wildcard
            "items[?type='CGST'].amount",  # filter
            "items[0].sku",  # index
            "..hsn_code",  # recursive descent
        ]:
            path.validate_path(p, rule_label="r")

    def test_rejects_root_selector(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            path.validate_path("$.customer", rule_label="r")

    def test_rejects_current_selector(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            path.validate_path("items[?@.x>1]", rule_label="r")

    def test_rejects_empty_path(self) -> None:
        with self.assertRaises(FieldMappingCompileError):
            path.validate_path("", rule_label="r")


class TestPathHasIteration(unittest.TestCase):
    def test_iteration_markers(self) -> None:
        self.assertTrue(path.path_has_iteration("items[].x"))
        self.assertTrue(path.path_has_iteration("items[*].x"))
        self.assertTrue(path.path_has_iteration("items[?t='X'].x"))
        self.assertTrue(path.path_has_iteration("..hsn_code"))

    def test_scalar_paths(self) -> None:
        self.assertFalse(path.path_has_iteration("customer.gstin"))
        self.assertFalse(path.path_has_iteration("items[0].x"))


class TestGetPath(unittest.TestCase):
    def test_dict_dot_access(self) -> None:
        self.assertEqual(
            path.get_path({"customer": {"gstin": "ABC"}}, "customer.gstin"),
            ["ABC"],
        )

    def test_object_attribute_access(self) -> None:
        obj = SimpleNamespace(customer=SimpleNamespace(gstin="ABC"))
        self.assertEqual(path.get_path(obj, "customer.gstin"), ["ABC"])

    def test_iteration(self) -> None:
        data = {"items": [{"sku": "A"}, {"sku": "B"}, {"sku": "C"}]}
        self.assertEqual(path.get_path(data, "items[].sku"), ["A", "B", "C"])

    def test_filter_predicate(self) -> None:
        data = {
            "taxes": [
                {"type": "CGST", "amount": 5},
                {"type": "SGST", "amount": 5},
                {"type": "CGST", "amount": 3},
            ]
        }
        self.assertEqual(
            path.get_path(data, "taxes[?type='CGST'].amount"),
            [5, 3],
        )

    def test_index_access(self) -> None:
        self.assertEqual(
            path.get_path({"items": ["a", "b", "c"]}, "items[1]"),
            ["b"],
        )

    def test_missing_returns_empty(self) -> None:
        self.assertEqual(path.get_path({"x": 1}, "y"), [])


class TestSetPath(unittest.TestCase):
    def test_dot_path(self) -> None:
        out: dict = {}
        path.set_path(out, "customer.gstin", "ABC")
        self.assertEqual(out, {"customer": {"gstin": "ABC"}})

    def test_index_creates_list(self) -> None:
        out: dict = {}
        path.set_path(out, "items[0].sku", "X")
        path.set_path(out, "items[1].sku", "Y")
        self.assertEqual(out, {"items": [{"sku": "X"}, {"sku": "Y"}]})

    def test_rejects_iteration_marker(self) -> None:
        with self.assertRaises(ValueError):
            path.set_path({}, "items[].sku", "X")


class TestSumPath(unittest.TestCase):
    def test_sum_numerics(self) -> None:
        # Smoke that the Py2-syntax bug we fixed in jsonpath.py doesn't recur.
        data = {"items": [{"amount": 5}, {"amount": 3.5}, {"amount": "ignored"}]}
        self.assertEqual(path.sum_path(data, "items[].amount"), 8.5)
