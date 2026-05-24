"""Integration tests for the Field Mapping compiler (§5.9.1, §5.8).

Covers DB-backed concerns: ruleset doc creation, composition resolution,
cycle/max-depth detection, cross-table validation (computed reference),
cache invalidation. Also verifies the §5.11 library fixtures all compile.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.exceptions import FieldMappingCompileError
from ecommerce_super.easyecom.field_mapping import compiler


def _cleanup_test_fms(prefix: str) -> None:
    for n in frappe.get_all(
        "EasyEcom Field Mapping",
        filters={"mapping_name": ("like", f"{prefix}%")},
        pluck="name",
    ):
        # Snapshots are append-only; force-delete them too for test reset.
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


def _make(name, rules, *, computed=None, direction="Push", entity="Item"):
    doc = frappe.new_doc("EasyEcom Field Mapping")
    doc.mapping_name = name
    doc.entity_type = entity
    doc.direction = direction
    doc.active = 1
    doc.missing_field_policy = "Permissive"
    doc.change_reason = "test"
    for r in rules:
        doc.append("rules", r)
    for c in computed or []:
        doc.append("computed_fields", c)
    doc.insert(ignore_permissions=True)
    return doc


class TestCompilerBasic(FrappeTestCase):
    PREFIX = "test-fm-compiler-"

    def setUp(self) -> None:
        _cleanup_test_fms(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup_test_fms(self.PREFIX)

    def test_simple_ruleset_compiles(self) -> None:
        doc = _make(
            f"{self.PREFIX}simple",
            rules=[
                {
                    "erpnext_path": "item_code",
                    "easyecom_path": "sku",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
        )
        c = compiler.compile_ruleset(doc.name)
        self.assertEqual(c.mapping_name, doc.name)
        self.assertEqual(len(c.rules), 1)
        self.assertEqual(c.rules[0].erpnext_path, "item_code")

    def test_cache_invalidation_on_save(self) -> None:
        doc = _make(
            f"{self.PREFIX}cache",
            rules=[
                {
                    "erpnext_path": "x",
                    "easyecom_path": "x",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
        )
        c1 = compiler.compile_ruleset(doc.name)
        # Save triggers controller's on_update → invalidate_compiled_cache.
        doc.change_reason = "trigger cache bust"
        doc.save(ignore_permissions=True)
        c2 = compiler.compile_ruleset(doc.name)
        # New compile, new version.
        self.assertGreater(c2.version, c1.version)

    def test_invalidate_compiled_cache_function(self) -> None:
        doc = _make(
            f"{self.PREFIX}inval",
            rules=[
                {
                    "erpnext_path": "x",
                    "easyecom_path": "x",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
        )
        compiler.compile_ruleset(doc.name)
        compiler.invalidate_compiled_cache(doc.name)
        # Recompile works.
        compiler.compile_ruleset(doc.name)


class TestCompilerCrossTable(FrappeTestCase):
    PREFIX = "test-fm-xtable-"

    def setUp(self) -> None:
        _cleanup_test_fms(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup_test_fms(self.PREFIX)

    def test_computed_reference_must_exist(self) -> None:
        """A rule whose transform is 'computed' must reference a
        computed_field declared on the same ruleset (cross-table check)."""
        with self.assertRaises(Exception):
            _make(
                f"{self.PREFIX}badref",
                rules=[
                    {
                        "erpnext_path": "x",
                        "easyecom_path": "x",
                        "transform_push": "computed",
                        "transform_pull": "identity",
                        "transform_args": {"name": "missing"},
                    }
                ],
                computed=[],
            )

    def test_valid_computed_reference(self) -> None:
        doc = _make(
            f"{self.PREFIX}goodref",
            rules=[
                {
                    "erpnext_path": "x",
                    "easyecom_path": "x",
                    "transform_push": "computed",
                    "transform_pull": "identity",
                    "transform_args": {"name": "total"},
                }
            ],
            computed=[
                {
                    "field_name": "total",
                    "expression": "1 + 2",
                    "output_type": "Int",
                    "cache_per_record": 1,
                }
            ],
        )
        c = compiler.compile_ruleset(doc.name)
        self.assertIn("total", c.computed_fields)


class TestCompilerComposition(FrappeTestCase):
    PREFIX = "test-fm-compose-"

    def setUp(self) -> None:
        _cleanup_test_fms(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup_test_fms(self.PREFIX)

    def test_compose_target_must_exist(self) -> None:
        with self.assertRaises(Exception):
            _make(
                f"{self.PREFIX}orphan",
                rules=[
                    {
                        "erpnext_path": "uoms",
                        "easyecom_path": "uoms",
                        "transform_push": "compose",
                        "transform_pull": "compose",
                        "transform_args": {"ruleset": "no-such-child"},
                    }
                ],
            )

    def test_valid_compose(self) -> None:
        child = _make(
            f"{self.PREFIX}child",
            rules=[
                {
                    "erpnext_path": "name",
                    "easyecom_path": "name",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
        )
        parent = _make(
            f"{self.PREFIX}parent",
            rules=[
                {
                    "erpnext_path": "rows",
                    "easyecom_path": "rows",
                    "transform_push": "compose",
                    "transform_pull": "compose",
                    "transform_args": {"ruleset": child.name},
                }
            ],
        )
        c = compiler.compile_ruleset(parent.name)
        self.assertIn(child.name, c.composed_ruleset_names)

    def test_cycle_detected(self) -> None:
        """A → B → A should be caught by the compiler."""
        a_name = f"{self.PREFIX}cycle-a"
        b_name = f"{self.PREFIX}cycle-b"
        # Create A first as a single-rule placeholder (no compose),
        # then B references A, then update A to reference B → cycle.
        _make(
            a_name,
            rules=[
                {
                    "erpnext_path": "x",
                    "easyecom_path": "x",
                    "transform_push": "identity",
                    "transform_pull": "identity",
                }
            ],
        )
        _make(
            b_name,
            rules=[
                {
                    "erpnext_path": "rows",
                    "easyecom_path": "rows",
                    "transform_push": "compose",
                    "transform_pull": "compose",
                    "transform_args": {"ruleset": a_name},
                }
            ],
        )
        # Now make A reference B — creating a cycle.
        a = frappe.get_doc("EasyEcom Field Mapping", a_name)
        a.rules = []
        a.append(
            "rules",
            {
                "erpnext_path": "rows",
                "easyecom_path": "rows",
                "transform_push": "compose",
                "transform_pull": "compose",
                "transform_args": {"ruleset": b_name},
            },
        )
        a.change_reason = "introduce cycle"
        a.save(ignore_permissions=True)
        # The compile (used at run time, not save) should detect the cycle.
        compiler.invalidate_compiled_cache(a_name)
        compiler.invalidate_compiled_cache(b_name)
        with self.assertRaises(FieldMappingCompileError):
            compiler.compile_ruleset(a_name)


class TestCompilerLibrary(FrappeTestCase):
    """Every shipped §5.11 fixture must compile clean. This is the
    'fixtures-pass-compile' acceptance check."""

    def test_every_shipped_library_ruleset_compiles(self) -> None:
        for n in frappe.get_all(
            "EasyEcom Field Mapping",
            filters={"mapping_name": ("like", "EasyEcom-%")},
            pluck="name",
        ):
            try:
                compiler.compile_ruleset(n)
            except Exception as e:  # noqa: BLE001
                self.fail(f"Library ruleset {n!r} failed to compile: {e}")
