"""Integration tests for the §7.1.1 EasyEcom Sync Record Line child table.

The §7 packet adds the empty schema as a foundation prerequisite — no
population logic yet. These tests verify:
  1. The child table can hold rows (schema works end-to-end through
     Frappe's insert + reload path).
  2. The Sync Record status enum is binary (§7.3) — Partial /
     Discrepancy / completed_with_discrepancy are NOT permitted values.
  3. The line_status enum has exactly the three values the spec defines.

We do NOT test population by a flow handler — that's a §9+ obligation;
flows that populate the table don't exist yet.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_sync_record.easyecom_sync_record import (
    upsert,
)
from ecommerce_super.easyecom.utils import idempotency
from ecommerce_super.tests.factories import cleanup_easyecom_state, make_location


def _wipe_sync_records(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"correlation_id": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Sync Record", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestSyncRecordLineSchema(FrappeTestCase):
    """The child table accepts rows and round-trips them. Schema-only test —
    no flow populates this yet."""

    LOC_KEY = "TEST-SR-LINES-LOC"
    LOC_DOCNAME = "ECS-LOC-TEST-SR-LINES-LOC"
    CORR_PREFIX = "test-sr-lines-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cleanup_easyecom_state()
        make_location(location_key=cls.LOC_KEY)

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_easyecom_state()
        super().tearDownClass()

    def setUp(self) -> None:
        _wipe_sync_records(self.CORR_PREFIX)

    def tearDown(self) -> None:
        _wipe_sync_records(self.CORR_PREFIX)

    def _make_parent(self, marker: str) -> str:
        key = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="_Test Company",
            payload={"_marker": marker},
        )
        doc = upsert(
            company="_Test Company",
            entity_doctype="EasyEcom Location",
            entity_name=self.LOC_DOCNAME,
            entity_type="Warehouse",
            direction="Pull",
            correlation_id=f"{self.CORR_PREFIX}{marker}",
            idempotency_key=key,
        )
        return doc.name

    def test_can_hold_line_rows_round_trip(self) -> None:
        """Three lines — OK, Failed, Discrepancy — persist and reload with
        all fields intact. Proves the Table field + child DocType wiring
        works end-to-end."""
        parent_name = self._make_parent("rt-1")
        parent = frappe.get_doc("EasyEcom Sync Record", parent_name)
        parent.append(
            "lines",
            {
                "source_line_ref": "SKU-OK-7",
                "target_field": "item_code",
                "line_status": "OK",
            },
        )
        parent.append(
            "lines",
            {
                "source_line_ref": "SKU-BAD-3",
                "target_field": "item_code",
                "line_status": "Failed",
                "reason": "Unmapped item_code: ERPNext has no Item for this SKU.",
            },
        )
        parent.append(
            "lines",
            {
                "source_line_ref": "SKU-VAR-1",
                "target_field": "qty",
                "line_status": "Discrepancy",
                "reason": "Received 9, expected 10 (tolerance 0).",
            },
        )
        parent.save(ignore_permissions=True)
        frappe.db.commit()

        reloaded = frappe.get_doc("EasyEcom Sync Record", parent_name)
        self.assertEqual(len(reloaded.lines), 3)

        by_ref = {row.source_line_ref: row for row in reloaded.lines}
        self.assertEqual(by_ref["SKU-OK-7"].line_status, "OK")
        self.assertEqual(by_ref["SKU-OK-7"].target_field, "item_code")
        self.assertIsNone(by_ref["SKU-OK-7"].reason)

        self.assertEqual(by_ref["SKU-BAD-3"].line_status, "Failed")
        self.assertIn("Unmapped", by_ref["SKU-BAD-3"].reason)

        self.assertEqual(by_ref["SKU-VAR-1"].line_status, "Discrepancy")
        self.assertIn("Received 9", by_ref["SKU-VAR-1"].reason)
        # Integration Discrepancy link is empty until §23 ships the DocType.
        self.assertFalse(by_ref["SKU-VAR-1"].ecs_integration_discrepancy)

    def test_empty_lines_is_the_default(self) -> None:
        """Foundation contract — single-entity flows leave `lines` empty.
        The schema must not auto-populate or require rows."""
        parent_name = self._make_parent("empty")
        reloaded = frappe.get_doc("EasyEcom Sync Record", parent_name)
        self.assertEqual(len(reloaded.lines), 0)
        # §9 Stage 4 — empty lines → empty summary (no "0/0 OK" noise).
        self.assertEqual(reloaded.ecs_lines_summary or "", "")

    def test_lines_summary_derived_on_save(self) -> None:
        """§9 Stage 4 line-child outcome chip — the Sync Record list
        renders `ecs_lines_summary` as a coloured pill. The field is
        recomputed in validate() from the lines child, so FDEs see
        the rollout immediately after the flow handler writes the
        lines."""
        parent_name = self._make_parent("summary-1")
        parent = frappe.get_doc("EasyEcom Sync Record", parent_name)
        for ln in (
            ("SKU-A", "OK"),
            ("SKU-B", "OK"),
            ("SKU-C", "OK"),
            ("SKU-D", "Discrepancy"),
            ("SKU-E", "Failed"),
        ):
            parent.append(
                "lines",
                {
                    "source_line_ref": ln[0],
                    "target_field": "item_code",
                    "line_status": ln[1],
                },
            )
        parent.save(ignore_permissions=True)
        frappe.db.commit()

        reloaded = frappe.get_doc("EasyEcom Sync Record", parent_name)
        self.assertEqual(
            reloaded.ecs_lines_summary,
            "3/5 OK · 1 Failed · 1 Discrepancy",
        )

    def test_lines_summary_all_ok_omits_failed_and_discrepancy(self) -> None:
        """When every line is OK, the chip shows only the OK count —
        no '0 Failed · 0 Discrepancy' clutter."""
        parent_name = self._make_parent("summary-ok")
        parent = frappe.get_doc("EasyEcom Sync Record", parent_name)
        for i in range(4):
            parent.append(
                "lines",
                {
                    "source_line_ref": f"SKU-OK-{i}",
                    "target_field": "item_code",
                    "line_status": "OK",
                },
            )
        parent.save(ignore_permissions=True)
        frappe.db.commit()
        reloaded = frappe.get_doc("EasyEcom Sync Record", parent_name)
        self.assertEqual(reloaded.ecs_lines_summary, "4/4 OK")


class TestSyncRecordStatusIsBinary(FrappeTestCase):
    """§7.3: the per-record outcome is *not* simply binary — it's
    Success | Failed | Discrepancy. Discrepancy is the
    "succeeded-but-found-divergence" outcome (first used by §8d drift
    detection). The §7.3 line says conflict/divergence routes to a
    Discrepancy outcome, NOT Failed — keeping the two visibly
    distinct so §22 alert routing can subscribe to drift events
    without conflating with sync failures.

    Partial is still forbidden — partial outcomes belong at the line
    level, not the parent Sync Record."""

    def test_status_enum_includes_discrepancy(self) -> None:
        """Status options: Pending | Running | Success | Failed |
        Discrepancy | Cancelled | AlreadySynced. Discrepancy added by
        the §8d audit follow-up; matches the same value already
        present on Sync Record Line's line_status enum."""
        meta = frappe.get_meta("EasyEcom Sync Record")
        field = meta.get_field("status")
        options = set(field.options.split("\n"))
        expected = {
            "Pending",
            "Running",
            "Success",
            "Failed",
            "Discrepancy",
            "Cancelled",
            "AlreadySynced",
        }
        self.assertEqual(options, expected)
        # Partial remains forbidden — partial outcomes are line-level,
        # never parent-level (§7.3).
        self.assertNotIn("Partial", options)
        self.assertNotIn("Completed With Discrepancy", options)

    def test_line_status_enum_is_exactly_three(self) -> None:
        """§31.2.3: line_status is OK | Failed | Discrepancy."""
        meta = frappe.get_meta("EasyEcom Sync Record Line")
        field = meta.get_field("line_status")
        options = set(field.options.split("\n"))
        self.assertEqual(options, {"OK", "Failed", "Discrepancy"})

    def test_partial_status_rejected_at_persistence(self) -> None:
        """Defence-in-depth — even if someone bypasses the meta check and
        writes status='Partial' directly, the Select field rejects it."""
        key = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="_Test Company",
            payload={"_marker": "binary-defence"},
        )
        doc = frappe.new_doc("EasyEcom Sync Record")
        doc.update(
            {
                "company": "_Test Company",
                "entity_doctype": "Company",
                "entity_name": "_Test Company",
                "entity_type": "Channel",
                "direction": "Pull",
                "status": "Partial",  # not a permitted value
                "correlation_id": "test-binary-defence",
                "idempotency_key": key,
            }
        )
        # Frappe raises ValidationError for invalid Select values on insert.
        with self.assertRaises(Exception):
            doc.insert(ignore_permissions=True)
