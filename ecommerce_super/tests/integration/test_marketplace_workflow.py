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


def _wipe_marketplaces(prefix: str) -> None:
    for n in frappe.db.get_all(
        "Marketplace",
        filters={"marketplace_id": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc("Marketplace", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()


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

    PREFIX = "wf-mkt-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe_marketplaces(self.PREFIX)
        self._original_user = frappe.session.user

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        _wipe_marketplaces(self.PREFIX)

    def _make_unclassified(self, key: str) -> str:
        doc = frappe.new_doc("Marketplace")
        doc.update(
            {
                "marketplace_id": key,
                "marketplace_name": f"Test {key}",
                "workflow_state": "Unclassified",
                "is_active": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_classify_blocked_without_channel_type(self) -> None:
        """The Classify transition's condition is `doc.channel_type`."""
        name = self._make_unclassified(f"{self.PREFIX}block")
        doc = frappe.get_doc("Marketplace", name)
        with self.assertRaises(frappe.ValidationError):
            apply_workflow(doc, "Classify")

    def test_classify_succeeds_with_channel_type(self) -> None:
        name = self._make_unclassified(f"{self.PREFIX}ok")
        frappe.db.set_value("Marketplace", name, "channel_type", "B2C Marketplace")
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Classify")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Classified")
        self.assertEqual(doc.channel_type, "B2C Marketplace")

    def test_activate_from_classified(self) -> None:
        name = self._make_unclassified(f"{self.PREFIX}activate")
        frappe.db.set_value("Marketplace", name, "channel_type", "B2C Marketplace")
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Classify")
        doc.reload()
        apply_workflow(doc, "Activate")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Active")

    def test_mark_not_relevant_routes_to_ignored(self) -> None:
        name = self._make_unclassified(f"{self.PREFIX}ignore")
        doc = frappe.get_doc("Marketplace", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Ignored")

    def test_deactivate_back_to_classified(self) -> None:
        name = self._make_unclassified(f"{self.PREFIX}deact")
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
        name = self._make_unclassified(f"{self.PREFIX}axes")
        frappe.db.set_value("Marketplace", name, "is_active", 1)
        doc = frappe.get_doc("Marketplace", name)
        # is_active=1 but workflow_state=Unclassified — both valid.
        self.assertEqual(doc.is_active, 1)
        self.assertEqual(doc.workflow_state, "Unclassified")
