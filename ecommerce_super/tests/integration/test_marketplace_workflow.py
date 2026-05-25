"""Integration tests for the Marketplace Classification Workflow fixture.

§8.6.3: Unclassified → Classified → Active, branch Ignored. Classify
gated on channel_type being set. Role-gated to EasyEcom FDE (System
Manager inherits via duplicated transitions — same pattern as 8a).
"""

from __future__ import annotations

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.integration.test_location_workflow import (
    _ensure_admin_has_fde_role,
)


def _wipe_marketplaces(mids: list[int]) -> None:
    """marketplace_id is Int (§31.2.18) — wipe by id list."""
    if not mids:
        return
    for n in frappe.db.get_all(
        "Marketplace",
        filters={"marketplace_id": ("in", mids)},
        pluck="name",
    ):
        try:
            frappe.delete_doc("Marketplace", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()


# High-range test marketplace_ids, distinct from the seed (2/8/60/100/122)
# and unlikely to collide with real EE ids.
TEST_MIDS = [99701, 99702, 99703, 99704, 99705, 99706]


class TestWorkflowFixtureInstalled(FrappeTestCase):
    def test_workflow_doc_exists_and_is_active(self) -> None:
        self.assertTrue(
            frappe.db.exists("Workflow", "Marketplace Classification Workflow")
        )
        wf = frappe.get_doc("Workflow", "Marketplace Classification Workflow")
        self.assertEqual(wf.document_type, "Marketplace")
        self.assertEqual(wf.is_active, 1)
        self.assertEqual(wf.workflow_state_field, "workflow_state")

    def test_all_states_exist(self) -> None:
        for state in ("Unclassified", "Classified", "Active", "Ignored"):
            self.assertTrue(
                frappe.db.exists("Workflow State", state),
                f"Workflow State {state!r} missing",
            )

    def test_classify_and_activate_actions_exist(self) -> None:
        for action in ("Classify", "Activate", "Deactivate", "Reclassify"):
            self.assertTrue(
                frappe.db.exists("Workflow Action Master", action),
                f"Workflow Action Master {action!r} missing",
            )

    def test_transitions_table_has_expected_rows(self) -> None:
        wf = frappe.get_doc("Workflow", "Marketplace Classification Workflow")
        triples = {(t.state, t.action, t.next_state) for t in wf.transitions}
        self.assertIn(("Unclassified", "Classify", "Classified"), triples)
        self.assertIn(("Classified", "Activate", "Active"), triples)
        self.assertIn(("Unclassified", "Mark Not Relevant", "Ignored"), triples)
        self.assertIn(("Classified", "Mark Not Relevant", "Ignored"), triples)
        self.assertIn(("Active", "Deactivate", "Classified"), triples)
        self.assertIn(("Classified", "Reclassify", "Unclassified"), triples)
        self.assertIn(("Ignored", "Reconsider", "Unclassified"), triples)


class TestMarketplaceTransitions(FrappeTestCase):
    """The Classify transition requires channel_type; Activate flows
    cleanly; Mark Not Relevant lands in Ignored."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe_marketplaces(TEST_MIDS)
        self._original_user = frappe.session.user

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        _wipe_marketplaces(TEST_MIDS)

    def _make_unclassified(self, mid: int) -> str:
        doc = frappe.new_doc("Marketplace")
        doc.update(
            {
                "marketplace_id": mid,
                "marketplace_name": f"Test {mid}",
                "workflow_state": "Unclassified",
                "is_active": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_classify_blocked_without_channel_type(self) -> None:
        """The Classify transition's condition is `doc.channel_type`."""
        name = self._make_unclassified(TEST_MIDS[0])
        doc = frappe.get_doc("Marketplace", name)
        with self.assertRaises(frappe.ValidationError):
            apply_workflow(doc, "Classify")

    def test_classify_succeeds_with_channel_type(self) -> None:
        name = self._make_unclassified(TEST_MIDS[1])
        frappe.db.set_value("Marketplace", name, "channel_type", "B2C Marketplace")
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Classify")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Classified")
        self.assertEqual(doc.channel_type, "B2C Marketplace")

    def test_activate_from_classified(self) -> None:
        name = self._make_unclassified(TEST_MIDS[2])
        frappe.db.set_value("Marketplace", name, "channel_type", "B2C Marketplace")
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Classify")
        doc.reload()
        apply_workflow(doc, "Activate")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Active")

    def test_mark_not_relevant_routes_to_ignored(self) -> None:
        name = self._make_unclassified(TEST_MIDS[3])
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Ignored")

    def test_deactivate_back_to_classified(self) -> None:
        name = self._make_unclassified(TEST_MIDS[4])
        frappe.db.set_value("Marketplace", name, "channel_type", "B2B")
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Classify")
        doc.reload()
        apply_workflow(doc, "Activate")
        doc.reload()
        apply_workflow(doc, "Deactivate")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Classified")

    def test_is_active_and_workflow_state_are_independent_axes(self) -> None:
        """A channel can be EE-Active but still Unclassified on our side
        (§8.6.3) — workflow_state and is_active are two axes."""
        name = self._make_unclassified(TEST_MIDS[5])
        frappe.db.set_value("Marketplace", name, "is_active", 1)
        doc = frappe.get_doc("Marketplace", name)
        self.assertEqual(doc.is_active, 1)
        self.assertEqual(doc.workflow_state, "Unclassified")


class TestAutonameAndDedupe(FrappeTestCase):
    """Mechanical verification per user request: autoname produces a
    clean docname for Int marketplace_id; dedupe by Int filter works."""

    AUTONAME_MID = 99800

    def setUp(self) -> None:
        _wipe_marketplaces([self.AUTONAME_MID])

    def tearDown(self) -> None:
        _wipe_marketplaces([self.AUTONAME_MID])

    def test_autoname_yields_numeric_docname(self) -> None:
        """`autoname: field:marketplace_id` on an Int field should
        produce a docname equal to the int's string representation."""
        doc = frappe.new_doc("Marketplace")
        doc.update(
            {
                "marketplace_id": self.AUTONAME_MID,
                "marketplace_name": "Autoname Probe",
                "workflow_state": "Unclassified",
                "is_active": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.name, str(self.AUTONAME_MID))

    def test_dedupe_by_int_filter_works(self) -> None:
        """frappe.db.exists / get_value with an Int filter must find
        the row written with that Int. Critical for the channel
        sweep's dedupe."""
        doc = frappe.new_doc("Marketplace")
        doc.update(
            {
                "marketplace_id": self.AUTONAME_MID,
                "marketplace_name": "Dedupe Probe",
                "workflow_state": "Unclassified",
                "is_active": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        # Both int and str filters should resolve (Frappe / MariaDB
        # coerce in WHERE clauses).
        self.assertTrue(
            frappe.db.exists("Marketplace", {"marketplace_id": self.AUTONAME_MID})
        )
        self.assertTrue(
            frappe.db.exists("Marketplace", {"marketplace_id": str(self.AUTONAME_MID)})
        )
        # get_value with Int filter returns the same row.
        name = frappe.db.get_value(
            "Marketplace", {"marketplace_id": self.AUTONAME_MID}, "name"
        )
        self.assertEqual(name, str(self.AUTONAME_MID))

    def test_unique_constraint_blocks_duplicate_id(self) -> None:
        """The unique constraint on marketplace_id must reject a second
        row with the same Int id — the dedupe contract relies on this
        at the DB level (the sweep's exists() check is the soft layer)."""
        first = frappe.new_doc("Marketplace")
        first.update(
            {
                "marketplace_id": self.AUTONAME_MID,
                "marketplace_name": "First",
                "workflow_state": "Unclassified",
                "is_active": 0,
            }
        )
        first.insert(ignore_permissions=True)
        second = frappe.new_doc("Marketplace")
        second.update(
            {
                "marketplace_id": self.AUTONAME_MID,
                "marketplace_name": "Second",
                "workflow_state": "Unclassified",
                "is_active": 0,
            }
        )
        with self.assertRaises(
            (frappe.DuplicateEntryError, frappe.exceptions.UniqueValidationError)
        ):
            second.insert(ignore_permissions=True)
