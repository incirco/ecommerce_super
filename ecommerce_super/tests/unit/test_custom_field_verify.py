"""gh#48 — Custom Field verifier + auto-rescue contract tests.

The verifier must:
  - return distinguishable outcomes for each (row-exists, column-exists)
    quadrant
  - degrade defensively when Frappe internals raise
  - support the ensure_custom_field rescue path without disturbing
    healthy existing fields

These tests use mocks so they can run without a live Frappe schema.
The audit-against-real-DB scenarios are exercised via the
ci-test.local provisioning script (see gh#48 reproduction).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.install.custom_field_verify import (
    EXPECTED_FIELDS,
    ensure_custom_field,
    run_audit,
    verify_custom_field,
)


def _has_column_factory(*, column_present: bool):
    """Build a `has_column` side_effect that:
      - Returns True for the table-existence probe `has_column(dt, "name")`.
      - Returns `column_present` for any other fieldname probe.
    Mirrors the verifier's two-probe pattern: first probe checks the
    table by querying `name` (always present on existing DocType
    tables); second probe checks the actual fieldname.
    """
    def _impl(dt: str, fieldname: str) -> bool:
        if fieldname == "name":
            return True
        return column_present
    return _impl


def _has_column_sequence(seq):
    """`has_column` side_effect where the `name` probe is always True and
    non-`name` probes consume `seq` in order. Lets a test scripts the
    'column missing → rescue → column present' sequence without having
    to know how many table-existence probes happen in between."""
    iterator = iter(seq)

    def _impl(dt: str, fieldname: str) -> bool:
        if fieldname == "name":
            return True
        return next(iterator)
    return _impl


class TestVerifyCustomField(unittest.TestCase):
    """Outcomes of each (row, column) presence combination."""

    def test_ok_when_row_and_column_both_present(self) -> None:
        with (
            patch.object(frappe.db, "exists", return_value="W-ee_label"),
            patch.object(
                frappe.db, "has_column",
                side_effect=_has_column_factory(column_present=True),
            ),
        ):
            self.assertEqual(
                verify_custom_field("Warehouse", "ecs_ee_location_label"),
                "ok",
            )

    def test_missing_column_when_row_present_column_absent(self) -> None:
        """The gh#26 / gh#48 headline scenario — create_custom_fields
        recorded the row but didn't materialize the column."""
        with (
            patch.object(frappe.db, "exists", return_value="W-ee_label"),
            patch.object(
                frappe.db, "has_column",
                side_effect=_has_column_factory(column_present=False),
            ),
        ):
            self.assertEqual(
                verify_custom_field("Warehouse", "ecs_ee_location_label"),
                "missing_column",
            )

    def test_missing_row_when_column_present_row_absent(self) -> None:
        with (
            patch.object(frappe.db, "exists", return_value=None),
            patch.object(
                frappe.db, "has_column",
                side_effect=_has_column_factory(column_present=True),
            ),
        ):
            self.assertEqual(
                verify_custom_field("Warehouse", "ecs_ee_location_label"),
                "missing_row",
            )

    def test_missing_row_and_column_when_neither_present(self) -> None:
        with (
            patch.object(frappe.db, "exists", return_value=None),
            patch.object(
                frappe.db, "has_column",
                side_effect=_has_column_factory(column_present=False),
            ),
        ):
            self.assertEqual(
                verify_custom_field("Warehouse", "ecs_ee_location_label"),
                "missing_row_and_column",
            )

    def test_doctype_missing_when_parent_table_absent(self) -> None:
        # has_column on `name` returns False → table-existence probe fails.
        with patch.object(
            frappe.db, "has_column",
            side_effect=lambda dt, fname: False,
        ):
            self.assertEqual(
                verify_custom_field("MadeUpDocType", "fake_field"),
                "doctype_missing",
            )

    def test_defensive_against_frappe_internal_raise(self) -> None:
        """If `has_column` raises on the FIELDNAME probe (not the table
        probe), verifier must return a pessimistic outcome rather than
        propagating the error."""
        def _raise_on_fieldname(dt: str, fname: str) -> bool:
            if fname == "name":
                return True
            raise Exception("boom")

        with (
            patch.object(frappe.db, "exists", return_value="W-ee_label"),
            patch.object(
                frappe.db, "has_column", side_effect=_raise_on_fieldname
            ),
        ):
            # row exists, column lookup raised → treat column as missing.
            self.assertEqual(
                verify_custom_field("Warehouse", "ecs_ee_location_label"),
                "missing_column",
            )

    def test_defensive_against_table_probe_raise(self) -> None:
        """If the table-existence probe (`has_column(dt, "name")`) itself
        raises, verifier returns 'doctype_missing' rather than crashing."""
        with patch.object(
            frappe.db, "has_column", side_effect=Exception("boom")
        ):
            self.assertEqual(
                verify_custom_field("Warehouse", "ecs_ee_location_label"),
                "doctype_missing",
            )


class TestEnsureCustomField(unittest.TestCase):
    """Rescue path behavior."""

    def test_noop_when_already_ok(self) -> None:
        """A field that already exists must NOT trigger a new insert."""
        with (
            patch.object(frappe.db, "exists", return_value="W-ee_label"),
            patch.object(
                frappe.db, "has_column",
                side_effect=_has_column_factory(column_present=True),
            ),
            patch("frappe.new_doc") as new_doc_mock,
        ):
            name = ensure_custom_field("Warehouse", "ecs_ee_location_label", {})
        new_doc_mock.assert_not_called()
        self.assertEqual(name, "Warehouse-ecs_ee_location_label")

    def test_returns_empty_string_when_doctype_missing(self) -> None:
        """A missing parent DocType isn't an error; the rescue defers
        until that DocType ships. Caller can check '' to detect."""
        with patch.object(
            frappe.db, "has_column",
            side_effect=lambda dt, fname: False,
        ):
            result = ensure_custom_field("MadeUpDoctype", "fake", {})
        self.assertEqual(result, "")

    def test_creates_field_when_missing_row_and_column(self) -> None:
        fake_doc = MagicMock(name="ECS-CF-1")
        fake_doc.name = "Warehouse-ecs_ee_location_label"

        # First verify: missing_row_and_column. After insert + audit: ok.
        # Mimic via a sequence on has_column (non-"name" probes only) /
        # exists. The `name` probe always returns True via the helper.
        exists_returns = iter([None, True])

        with (
            patch.object(
                frappe.db, "exists", side_effect=lambda *a, **k: next(exists_returns)
            ),
            patch.object(
                frappe.db,
                "has_column",
                side_effect=_has_column_sequence([False, True]),
            ),
            patch.object(frappe.db, "get_value", return_value=None),
            patch("frappe.new_doc", return_value=fake_doc) as new_doc_mock,
            patch.object(frappe.db, "commit"),
        ):
            name = ensure_custom_field(
                "Warehouse",
                "ecs_ee_location_label",
                {
                    "label": "EE Location",
                    "fieldtype": "Data",
                    "read_only": 1,
                },
            )

        new_doc_mock.assert_called_once_with("Custom Field")
        fake_doc.insert.assert_called_once()
        # Sanity: the spec was passed through (excluding identity fields).
        update_payload = fake_doc.update.call_args.args[0]
        self.assertEqual(update_payload["dt"], "Warehouse")
        self.assertEqual(update_payload["fieldname"], "ecs_ee_location_label")
        self.assertEqual(update_payload["label"], "EE Location")
        self.assertEqual(name, "Warehouse-ecs_ee_location_label")

    def test_drops_insert_after_from_spec(self) -> None:
        """The rescue path appends to end of field list; insert_after
        is intentionally dropped from the spec passed to the new
        Custom Field."""
        fake_doc = MagicMock()
        fake_doc.name = "Warehouse-some_field"
        exists_returns = iter([None, True])
        with (
            patch.object(
                frappe.db, "exists", side_effect=lambda *a, **k: next(exists_returns)
            ),
            patch.object(
                frappe.db,
                "has_column",
                side_effect=_has_column_sequence([False, True]),
            ),
            patch.object(frappe.db, "get_value", return_value=None),
            patch("frappe.new_doc", return_value=fake_doc),
            patch.object(frappe.db, "commit"),
        ):
            ensure_custom_field(
                "Warehouse",
                "some_field",
                {
                    "label": "X",
                    "fieldtype": "Data",
                    "insert_after": "warehouse_name",
                },
            )
        payload = fake_doc.update.call_args.args[0]
        self.assertNotIn("insert_after", payload)
        self.assertEqual(payload["label"], "X")


class TestExpectedFieldsRegistry(unittest.TestCase):
    """The canonical registry must be well-formed and cover known
    gh-#issues."""

    def test_registry_is_non_empty(self) -> None:
        self.assertGreater(len(EXPECTED_FIELDS), 0)

    def test_warehouse_ee_location_label_in_registry(self) -> None:
        """gh#26's field — the audit's first concrete responsibility."""
        names = [(dt, fn) for dt, fn, _ in EXPECTED_FIELDS]
        self.assertIn(("Warehouse", "ecs_ee_location_label"), names)

    def test_every_entry_has_required_spec_keys(self) -> None:
        for dt, fieldname, spec in EXPECTED_FIELDS:
            with self.subTest(dt=dt, fieldname=fieldname):
                self.assertIn("label", spec, f"{dt}.{fieldname} missing label")
                self.assertIn(
                    "fieldtype", spec, f"{dt}.{fieldname} missing fieldtype"
                )


class TestRunAudit(unittest.TestCase):
    """The audit summary must report what's healthy and what got rescued."""

    def test_all_ok_summary_when_every_field_healthy(self) -> None:
        with (
            patch.object(frappe.db, "exists", return_value="x"),
            patch.object(
                frappe.db, "has_column",
                side_effect=_has_column_factory(column_present=True),
            ),
        ):
            summary = run_audit()
        self.assertEqual(summary["ok"], summary["total"])
        self.assertEqual(summary["rescued"], 0)
        self.assertEqual(summary["doctype_missing"], 0)

    def test_missing_columns_get_rescued(self) -> None:
        """Simulate the gh#48 reproduction — every field reports
        missing_column on the first check, ok on the second."""
        exists_returns = iter([True] * len(EXPECTED_FIELDS) * 10)

        fake_doc = MagicMock()
        fake_doc.name = "DummyName"

        with (
            patch.object(
                frappe.db, "exists", side_effect=lambda *a, **k: next(exists_returns)
            ),
            patch.object(
                frappe.db,
                "has_column",
                side_effect=_has_column_sequence(
                    [False, True] * len(EXPECTED_FIELDS) * 5
                ),
            ),
            patch.object(frappe.db, "get_value", return_value="dummy"),
            patch("frappe.new_doc", return_value=fake_doc),
            patch.object(frappe.db, "commit"),
        ):
            summary = run_audit()
        # All fields were missing column on entry; rescue path attempted.
        self.assertEqual(summary["total"], len(EXPECTED_FIELDS))


if __name__ == "__main__":
    unittest.main()
