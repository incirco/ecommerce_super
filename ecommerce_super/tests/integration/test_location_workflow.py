"""Integration tests for the EasyEcom Location Workflow fixture (§8.4.1).

Covers:
  - The Workflow fixture is installed and active.
  - is_operational is workflow-derived: Live → 1, everything else → 0.
  - The 'Map' transition requires frappe_company to be set.
  - The 'Go Live' transition sets is_operational = 1.
  - Pause moves Live → Mapped but not Live and clears is_operational.
"""

from __future__ import annotations

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


def _wipe_locations(prefix: str) -> None:
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


class TestWorkflowFixtureInstalled(FrappeTestCase):
    """The Workflow + its States + Actions ship as fixtures."""

    def test_workflow_doc_exists_and_is_active(self) -> None:
        self.assertTrue(frappe.db.exists("Workflow", "EasyEcom Location Workflow"))
        wf = frappe.get_doc("Workflow", "EasyEcom Location Workflow")
        self.assertEqual(wf.document_type, "EasyEcom Location")
        self.assertEqual(wf.is_active, 1)
        self.assertEqual(wf.workflow_state_field, "workflow_state")

    def test_all_four_states_exist(self) -> None:
        for state in ("To Map", "Mapped but not Live", "Live", "Skipped"):
            self.assertTrue(
                frappe.db.exists("Workflow State", state),
                f"Workflow State {state!r} missing",
            )

    def test_all_five_actions_exist(self) -> None:
        for action in ("Map", "Go Live", "Mark Not Relevant", "Pause", "Reconsider"):
            self.assertTrue(
                frappe.db.exists("Workflow Action Master", action),
                f"Workflow Action Master {action!r} missing",
            )

    def test_transitions_table_has_expected_rows(self) -> None:
        wf = frappe.get_doc("Workflow", "EasyEcom Location Workflow")
        triples = {(t.state, t.action, t.next_state) for t in wf.transitions}
        self.assertIn(("To Map", "Map", "Mapped but not Live"), triples)
        self.assertIn(("Mapped but not Live", "Go Live", "Live"), triples)
        self.assertIn(("To Map", "Mark Not Relevant", "Skipped"), triples)
        self.assertIn(("Mapped but not Live", "Mark Not Relevant", "Skipped"), triples)
        self.assertIn(("Live", "Pause", "Mapped but not Live"), triples)
        self.assertIn(("Skipped", "Reconsider", "To Map"), triples)


class TestIsOperationalDerivedFromWorkflowState(FrappeTestCase):
    """The §8.4.1 rule: is_operational is derived from workflow_state.
    Live → 1, every other state → 0. The FDE no longer toggles it.

    Because Frappe's active workflow auto-applies on insert (refusing
    skip-transitions from the initial state), each test reaches its
    target state via the legal transition chain — same way the FDE
    would in production.
    """

    PREFIX = "wf-derive-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe_locations(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_locations(self.PREFIX)

    def _make_to_map(self, key: str) -> str:
        """Insert in the workflow's initial state."""
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(
            {
                "location_key": key,
                "location_name": f"WF Test {key}",
                "enabled": 1,
                "workflow_state": "To Map",
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_to_map_state_means_not_operational(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}to-map")
        doc = frappe.get_doc("EasyEcom Location", name)
        self.assertEqual(doc.workflow_state, "To Map")
        self.assertEqual(doc.is_operational, 0)

    def test_mapped_but_not_live_means_not_operational(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}mapped")
        # FDE assigns Company then transitions Map → Mapped but not Live.
        frappe.db.set_value(
            "EasyEcom Location", name, "frappe_company", self.company
        )
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Mapped but not Live")
        self.assertEqual(doc.is_operational, 0)

    def test_live_state_means_operational(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}live")
        frappe.db.set_value(
            "EasyEcom Location", name, "frappe_company", self.company
        )
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        apply_workflow(doc, "Go Live")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Live")
        self.assertEqual(doc.is_operational, 1)

    def test_skipped_state_is_not_operational(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}skip")
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Skipped")
        self.assertEqual(doc.is_operational, 0)


def _ensure_admin_has_fde_role() -> None:
    """Grant Administrator the EasyEcom FDE role for workflow tests.

    Production FDEs have this role assigned at setup. Administrator
    (the default test user) is a System Manager but does not get
    EasyEcom FDE by default; the workflow transitions are role-gated to
    EasyEcom FDE, so without it apply_workflow raises
    WorkflowPermissionError. We grant once per test class.

    After adding the role we MUST clear the user-roles cache and reset
    frappe.local — frappe.get_roles() reads from a per-session cache
    that doesn't auto-invalidate on role-table writes.
    """
    admin = frappe.get_doc("User", "Administrator")
    has = any(r.role == "EasyEcom FDE" for r in admin.roles)
    if not has:
        admin.append("roles", {"role": "EasyEcom FDE"})
        admin.save(ignore_permissions=True)
        frappe.db.commit()
    # Force a roles-cache refresh for the current session so the new
    # role is visible to frappe.get_roles() — set_user reads roles fresh.
    frappe.clear_cache(user="Administrator")
    frappe.set_user("Administrator")


class TestWorkflowTransitions(FrappeTestCase):
    """The Map transition requires frappe_company; Go Live flips
    is_operational to 1; Pause reverses it."""

    PREFIX = "wf-trans-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_admin_has_fde_role()

    def setUp(self) -> None:
        _wipe_locations(self.PREFIX)
        self._original_user = frappe.session.user

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        _wipe_locations(self.PREFIX)

    def _make_to_map(self, key: str) -> str:
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(
            {
                "location_key": key,
                "location_name": f"Trans Test {key}",
                "enabled": 1,
                "workflow_state": "To Map",
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_map_transition_blocked_without_frappe_company(self) -> None:
        """The Map transition's condition is `doc.frappe_company` —
        without it set, the transition is not legal."""
        name = self._make_to_map(f"{self.PREFIX}map-blocked")
        doc = frappe.get_doc("EasyEcom Location", name)
        # frappe_company is empty; the Map action's condition fails.
        with self.assertRaises(frappe.ValidationError):
            apply_workflow(doc, "Map")

    def test_map_transition_succeeds_with_frappe_company(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}map-ok")
        # Set company, then transition.
        frappe.db.set_value("EasyEcom Location", name, "frappe_company", self.company)
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Mapped but not Live")
        # Still not operational — Go Live is the next step.
        self.assertEqual(doc.is_operational, 0)

    def test_go_live_sets_is_operational(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}go-live")
        frappe.db.set_value("EasyEcom Location", name, "frappe_company", self.company)
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        apply_workflow(doc, "Go Live")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Live")
        self.assertEqual(doc.is_operational, 1)

    def test_pause_clears_is_operational(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}pause")
        frappe.db.set_value("EasyEcom Location", name, "frappe_company", self.company)
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Map")
        doc.reload()
        apply_workflow(doc, "Go Live")
        doc.reload()
        self.assertEqual(doc.is_operational, 1)
        apply_workflow(doc, "Pause")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Mapped but not Live")
        self.assertEqual(doc.is_operational, 0)

    def test_mark_not_relevant_routes_to_skipped(self) -> None:
        name = self._make_to_map(f"{self.PREFIX}skip")
        doc = frappe.get_doc("EasyEcom Location", name)
        apply_workflow(doc, "Mark Not Relevant")
        doc.reload()
        self.assertEqual(doc.workflow_state, "Skipped")
        self.assertEqual(doc.is_operational, 0)
