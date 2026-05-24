"""Integration tests for the Field Mapping executor (§5.9.2, §5.9.3)."""

from __future__ import annotations

from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.exceptions import (
    FieldMappingMissingRequiredError,
    SyncPreconditionError,
)
from ecommerce_super.easyecom.field_mapping import compiler
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor


def _cleanup(prefix: str) -> None:
    for n in frappe.get_all(
        "EasyEcom Field Mapping",
        filters={"mapping_name": ("like", f"{prefix}%")},
        pluck="name",
    ):
        for v in frappe.get_all(
            "EasyEcom Field Mapping Version",
            filters={"parent_mapping": n},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Field Mapping Version", v, force=True, ignore_permissions=True
            )
        frappe.delete_doc(
            "EasyEcom Field Mapping", n, force=True, ignore_permissions=True
        )
    frappe.db.commit()


def _make(
    name, rules, *, computed=None, direction="Push", entity="Item", preconditions=None
):
    doc = frappe.new_doc("EasyEcom Field Mapping")
    doc.mapping_name = name
    doc.entity_type = entity
    doc.direction = direction
    doc.active = 1
    doc.missing_field_policy = "Permissive"
    doc.change_reason = "test"
    if preconditions:
        doc.preconditions = preconditions
    for r in rules:
        doc.append("rules", r)
    for c in computed or []:
        doc.append("computed_fields", c)
    doc.insert(ignore_permissions=True)
    return doc


class TestPushAndPull(FrappeTestCase):
    PREFIX = "test-fm-exec-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_scalar_push(self) -> None:
        doc = _make(
            f"{self.PREFIX}scalar",
            rules=[
                {
                    "erpnext_path": "item_code",
                    "easyecom_path": "sku",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                },
                {
                    "erpnext_path": "qty",
                    "easyecom_path": "quantity",
                    "transform_push": "int_to_str",
                    "transform_pull": "str_to_int",
                },
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        out = ex.push(SimpleNamespace(item_code="ABC", qty=5))
        self.assertEqual(out, {"sku": "ABC", "quantity": "5"})

    def test_iteration_push(self) -> None:
        doc = _make(
            f"{self.PREFIX}iter",
            rules=[
                {
                    "erpnext_path": "items[].rate",
                    "easyecom_path": "items[].price_paise",
                    "transform_push": "currency_to_paise",
                    "transform_pull": "paise_to_currency",
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        out = ex.push(SimpleNamespace(items=[{"rate": 10.00}, {"rate": 12.34}]))
        self.assertEqual(out, {"items": [{"price_paise": 1000}, {"price_paise": 1234}]})

    def test_pull_roundtrip(self) -> None:
        doc = _make(
            f"{self.PREFIX}pull",
            rules=[
                {
                    "erpnext_path": "qty",
                    "easyecom_path": "quantity",
                    "transform_push": "int_to_str",
                    "transform_pull": "str_to_int",
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        out = ex.pull({"quantity": "42"})
        self.assertEqual(out, {"qty": 42})


class TestComputedFields(FrappeTestCase):
    PREFIX = "test-fm-computed-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_computed_resolves_before_rule_application(self) -> None:
        # Computed expressions may use ONLY the documented allow-list:
        # source_doc, source_payload, get_path, sum_path, filter_path.
        # No builtins (`int`, `len`, etc.) — the spec is strict.
        doc = _make(
            f"{self.PREFIX}order",
            rules=[
                {
                    "erpnext_path": "total",
                    "easyecom_path": "total_paise",
                    "transform_push": "computed",
                    "transform_pull": "identity",
                    "transform_args": {"name": "total_paise"},
                }
            ],
            computed=[
                {
                    "field_name": "total_paise",
                    "expression": "source_doc.base * 100",
                    "output_type": "Int",
                    "cache_per_record": 1,
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        out = ex.push(SimpleNamespace(base=10.5, total=None))
        self.assertEqual(out["total_paise"], 1050)


class TestConditions(FrappeTestCase):
    PREFIX = "test-fm-cond-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_condition_true_applies_rule(self) -> None:
        doc = _make(
            f"{self.PREFIX}true",
            rules=[
                {
                    "erpnext_path": "wholesale_price",
                    "easyecom_path": "wholesale_price",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                    "condition": "source_doc.customer_type == 'B2B'",
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        out = ex.push(SimpleNamespace(customer_type="B2B", wholesale_price=99.5))
        self.assertEqual(out, {"wholesale_price": 99.5})

    def test_condition_false_skips_rule(self) -> None:
        doc = _make(
            f"{self.PREFIX}false",
            rules=[
                {
                    "erpnext_path": "wholesale_price",
                    "easyecom_path": "wholesale_price",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                    "condition": "source_doc.customer_type == 'B2B'",
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        out = ex.push(SimpleNamespace(customer_type="D2C", wholesale_price=99.5))
        self.assertEqual(out, {})


class TestApplicationOrderAndPolicy(FrappeTestCase):
    PREFIX = "test-fm-order-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_required_raises_on_missing(self) -> None:
        doc = _make(
            f"{self.PREFIX}req",
            rules=[
                {
                    "erpnext_path": "item_code",
                    "easyecom_path": "sku",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                    "required": 1,
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        with self.assertRaises(FieldMappingMissingRequiredError):
            ex.push(SimpleNamespace(item_code=None))

    def test_preconditions_skip_ruleset(self) -> None:
        doc = _make(
            f"{self.PREFIX}precon",
            rules=[
                {
                    "erpnext_path": "x",
                    "easyecom_path": "x",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
            preconditions="source_doc.allow == True",
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        with self.assertRaises(SyncPreconditionError):
            ex.push(SimpleNamespace(allow=False, x=1))


class TestComposition(FrappeTestCase):
    PREFIX = "test-fm-composex-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_compose_per_row(self) -> None:
        child = _make(
            f"{self.PREFIX}child",
            rules=[
                {
                    "erpnext_path": "uom_name",
                    "easyecom_path": "uom",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
            entity="Item UOM",
        )
        parent = _make(
            f"{self.PREFIX}parent",
            rules=[
                {
                    "erpnext_path": "uoms",
                    "easyecom_path": "uoms",
                    "transform_push": "compose",
                    "transform_pull": "compose",
                    "transform_args": {"ruleset": child.name},
                }
            ],
        )
        compiler.invalidate_compiled_cache(parent.name)
        compiler.invalidate_compiled_cache(child.name)
        ex = FieldMappingExecutor(parent.name)
        out = ex.push(
            SimpleNamespace(
                uoms=[
                    SimpleNamespace(uom_name="Box"),
                    SimpleNamespace(uom_name="Carton"),
                ]
            )
        )
        self.assertEqual(out, {"uoms": [{"uom": "Box"}, {"uom": "Carton"}]})


class TestTestWithSample(FrappeTestCase):
    PREFIX = "test-fm-tws-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_test_with_sample_returns_trace(self) -> None:
        doc = _make(
            f"{self.PREFIX}trace",
            rules=[
                {
                    "erpnext_path": "x",
                    "easyecom_path": "x",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
        )
        compiler.invalidate_compiled_cache(doc.name)
        ex = FieldMappingExecutor(doc.name)
        res = ex.test_with_sample({"x": "ok"}, "pull")
        self.assertIn("output", res)
        self.assertIn("trace", res)
        self.assertIn("errors", res)
        self.assertEqual(res["output"], {"x": "ok"})
        self.assertEqual(len(res["trace"]), 1)
        self.assertEqual(res["errors"], [])
