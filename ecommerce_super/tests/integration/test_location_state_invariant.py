"""§8.4.1 state-aware Company invariant on EasyEcom Location.

The invariant:
  - workflow_state in {To Map, Skipped}            → frappe_company empty
  - workflow_state in {Mapped but not Live, Live}  → frappe_company set
  - workflow_state empty                           → short-circuit (back-fill path)

Tests:
  - Direct save violations:
      To Map + company set      → rejected
      Skipped + company set     → rejected (via direct field write)
      Mapped but not Live + no company → rejected
      Live + no company         → rejected (subsumes the old rule)
  - Transition paths still work:
      Map → Go Live (the happy path)
      Pause from Live: Live → Mapped but not Live keeps Company
      Mark Not Relevant from Mapped but not Live: clears Company (the clear-hook)
      Mark Not Relevant from To Map: nothing to clear
      Reconsider from Skipped → To Map
  - Discovery upsert (new row → To Map with no company) passes
  - Back-fill (legacy null workflow_state) is unaffected by the invariant
"""

from __future__ import annotations

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows.location_discovery import (
    upsert_locations_from_payload,
)
from ecommerce_super.patches.v0_1.backfill_location_workflow_state import execute as backfill
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)
from ecommerce_super.tests.integration.test_location_workflow import (
    _ensure_admin_has_fde_role,
)


def _wipe(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Location",
        filters={"location_key": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Location", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestDirectSaveViolations(FrappeTestCase):
    """Direct doc.save() must reject combinations that violate the invariant."""

    PREFIX = "inv-direct-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe(self.PREFIX)

    def tearDown(self) -> None:
        _wipe(self.PREFIX)

    def _new(self, key: str, **fields) -> "frappe.model.document.Document":
        defaults = {
            "location_key": key,
            "location_name": f"Inv {key}",
            "enabled": 1,
            "workflow_state": "To Map",
        }
        defaults.update(fields)
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(defaults)
        return doc

    def test_to_map_with_company_is_allowed(self) -> None:
        """The §8.4.1 relaxation: To Map is the mid-mapping state. The
        FDE legitimately sets frappe_company on a To Map row in
        preparation for the Map transition (whose condition is
        doc.frappe_company). Saving with both set must persist the
        Company — clearing it would trap the FDE in a state where the
        Map button vanishes (its condition is unmet) and they cannot
        transition out."""
        doc = self._new(f"{self.PREFIX}tm-with-co", frappe_company=self.company)
        doc.insert(ignore_permissions=True)
        # Company persisted (no auto-clear in To Map).
        self.assertEqual(doc.frappe_company, self.company)
        self.assertEqual(doc.workflow_state, "To Map")
        # Re-save still preserves it.
        doc.save(ignore_permissions=True)
        doc.reload()
        self.assertEqual(doc.frappe_company, self.company)

    def test_to_map_save_then_map_button_path(self) -> None:
        """The exact FDE flow that the original strict invariant broke:
        edit Company on To Map row → Save → reload → Company still there →
        Actions → Map transitions cleanly. Regression test for the
        bug surfaced in the live sandbox session."""
        doc = self._new(f"{self.PREFIX}fde-flow", frappe_company=self.company)
        doc.insert(ignore_permissions=True)
        # FDE clicks Save. Company must survive.
        doc.save(ignore_permissions=True)
        doc.reload()
        self.assertEqual(doc.frappe_company, self.company)
        # FDE then clicks Actions → Map. Map's condition is met.
        apply_workflow(doc, "Map")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Mapped but not Live")
        self.assertEqual(doc.frappe_company, self.company)

    def test_skipped_with_company_via_direct_write_rejected(self) -> None:
        """A Skipped row that gains a Company through a direct field
        write (no transition) — the clear hook only fires ON transition
        INTO Skipped, so a plain Save while in Skipped sees the
        invariant catch the violation and refuse the save."""
        # Reach Skipped via the workflow. The Mark Not Relevant
        # transition's clear-hook fires (transition into Skipped from
        # To Map; clears Company if any was set — none here).
        doc = self._new(f"{self.PREFIX}skip-with-co")
        doc.insert(ignore_permissions=True)
        apply_workflow(frappe.get_doc("EasyEcom Location", doc.name), "Mark Not Relevant")
        skipped = frappe.get_doc("EasyEcom Location", doc.name)
        self.assertEqual(skipped.workflow_state, "Skipped")
        self.assertIsNone(skipped.frappe_company)

        # Now try to set Company on the Skipped row via direct write.
        # This is NOT a transition (workflow_state stays Skipped), so
        # the clear-hook doesn't fire. The invariant must REJECT.
        skipped.frappe_company = self.company
        with self.assertRaises(frappe.ValidationError):
            skipped.save(ignore_permissions=True)

    def test_mapped_but_not_live_without_company_rejected(self) -> None:
        """Reaching Mapped but not Live without a Company is impossible
        via the workflow (the Map transition's condition refuses), but a
        bypass that writes the state directly must be rejected by the
        invariant."""
        doc = self._new(f"{self.PREFIX}mapped-noco")
        doc.insert(ignore_permissions=True)
        # Try to direct-write the state without setting Company.
        doc.workflow_state = "Mapped but not Live"
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_live_without_company_rejected(self) -> None:
        """Subsumes the former 'is_operational requires frappe_company' rule."""
        doc = self._new(f"{self.PREFIX}live-noco")
        doc.insert(ignore_permissions=True)
        doc.workflow_state = "Live"
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)


class TestTransitionPaths(FrappeTestCase):
    """All legal workflow transitions still pass under the new rule."""

    PREFIX = "inv-trans-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe(self.PREFIX)

    def tearDown(self) -> None:
        _wipe(self.PREFIX)

    def _make_to_map(self, key: str) -> str:
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(
            {
                "location_key": key,
                "location_name": f"trans {key}",
                "enabled": 1,
                "workflow_state": "To Map",
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_map_then_go_live_happy_path(self) -> None:
        """The canonical FDE flow: To Map → set Company → Map → Go Live."""
        name = self._make_to_map(f"{self.PREFIX}happy")
        frappe.db.set_value("EasyEcom Location", name, "frappe_company", self.company)
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Mapped but not Live")
        self.assertEqual(doc.frappe_company, self.company)
        apply_workflow(doc, "Go Live")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Live")
        self.assertEqual(doc.frappe_company, self.company)
        self.assertEqual(doc.is_operational, 1)

    def test_pause_keeps_company(self) -> None:
        """Pause: Live → Mapped but not Live. Company stays set
        (Mapped but not Live requires it)."""
        name = self._make_to_map(f"{self.PREFIX}pause")
        frappe.db.set_value("EasyEcom Location", name, "frappe_company", self.company)
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        apply_workflow(doc, "Go Live")
        doc.reload()
        apply_workflow(doc, "Pause")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Mapped but not Live")
        self.assertEqual(doc.frappe_company, self.company)  # preserved

    def test_mark_not_relevant_from_mapped_clears_company(self) -> None:
        """THE clear-hook test: Mapped but not Live → Skipped via
        Mark Not Relevant. The transition shouldn't require the FDE to
        first clear Company manually; the controller does it as part of
        landing in Skipped."""
        name = self._make_to_map(f"{self.PREFIX}mark-from-mapped")
        frappe.db.set_value("EasyEcom Location", name, "frappe_company", self.company)
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        self.assertEqual(doc.frappe_company, self.company)

        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Skipped")
        # The clear hook stripped Company; invariant passed.
        self.assertIsNone(doc.frappe_company)
        self.assertIsNone(doc.mapped_warehouse)

    def test_mark_not_relevant_from_to_map_no_company_to_clear(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}mark-from-tomap")
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Skipped")
        self.assertIsNone(doc.frappe_company)

    def test_reconsider_from_skipped(self) -> None:
        """Skipped (no company) → To Map (no company). Both states demand
        empty Company; the transition is a no-op for the field."""
        name = self._make_to_map(f"{self.PREFIX}reconsider")
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        apply_workflow(doc, "Reconsider")
        doc.reload()
        self.assertEqual(doc.workflow_state, "To Map")
        self.assertIsNone(doc.frappe_company)


class TestDiscoveryAndBackfillUnderNewRule(FrappeTestCase):
    """Two foundation-critical paths that must not break under the new invariant:
    the discovery upsert (which creates new rows in To Map without Company)
    and the back-fill patch (which sets workflow_state on legacy rows that
    may carry Company already)."""

    PREFIX = "inv-paths-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe(self.PREFIX)
        _wipe("ne2948810")  # the real-payload prefix

    def tearDown(self) -> None:
        _wipe(self.PREFIX)
        _wipe("ne2948810")

    def test_discovery_new_row_to_map_passes_invariant(self) -> None:
        """upsert_locations_from_payload creates new rows in To Map with
        no frappe_company. The invariant: To Map + no Company → OK."""
        rows = [
            {
                "location_key": f"{self.PREFIX}new",
                "location_name": "discovery new",
                "company_id": 999,
                "stockHandle": 1,
            }
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        self.assertEqual(outcome.succeeded_count, 1)
        self.assertEqual(outcome.failed_count, 0)
        doc = frappe.get_doc("EasyEcom Location", f"ECS-LOC-{self.PREFIX}new")
        self.assertEqual(doc.workflow_state, "To Map")
        self.assertIsNone(doc.frappe_company)

    def test_backfill_runs_under_new_rule(self) -> None:
        """Build a legacy row (workflow_state=NULL, is_operational=1,
        company set) directly via SQL, then run the back-fill. It should
        land the row in Live without tripping the invariant — back-fill
        uses db.set_value which bypasses validate."""
        key = f"{self.PREFIX}legacy-live"
        docname = f"ECS-LOC-{key}"
        # Insert clean.
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(
            {
                "location_key": key,
                "location_name": "legacy",
                "enabled": 1,
                "workflow_state": "To Map",
            }
        )
        doc.insert(ignore_permissions=True)
        # Stamp the legacy state directly.
        frappe.db.sql(
            """UPDATE `tabEasyEcom Location`
               SET workflow_state=NULL, is_operational=1, frappe_company=%s
               WHERE name=%s""",
            (self.company, docname),
        )
        frappe.db.commit()

        # Run the back-fill.
        backfill()
        frappe.db.commit()

        # Row is now Live; invariant holds (Live + company set).
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", docname, "workflow_state"),
            "Live",
        )
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", docname, "frappe_company"),
            self.company,
        )
