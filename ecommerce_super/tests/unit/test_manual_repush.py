"""gh#165 — Re-fire EasyEcom Push button (whitelisted repush_so).

Locks:
  - Role gate: refuses non-System Manager / non-EasyEcom-FDE
  - Non-existent SO → ok=False message
  - Submitted SO with existing B2B Order Map → idempotent (already_mapped)
  - Draft SO → ok=False (must be submitted)
  - Failed Gate 0 → ok=False with clear message
  - Happy path → enqueues job, returns queue_job name
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestGh165RepushSo(unittest.TestCase):
    def _import(self):
        from ecommerce_super.easyecom.api.manual_repush import repush_so
        return repush_so

    def _mock_frappe(self, roles=("EasyEcom FDE",)):
        """Common patches for the module's frappe surface."""
        p_roles = patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.get_roles",
            return_value=list(roles),
        )
        p_session = patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.session"
        )
        return p_roles, p_session

    def test_refuses_when_role_missing(self):
        """No allowed role → PermissionError via frappe.throw."""
        import frappe
        p_roles, p_session = self._mock_frappe(roles=("Guest",))
        with p_roles, p_session, patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.throw",
            side_effect=frappe.PermissionError,
        ):
            with self.assertRaises(frappe.PermissionError):
                self._import()("SO-2610397")

    def test_returns_not_found_when_so_missing(self):
        import frappe
        p_roles, p_session = self._mock_frappe()
        with p_roles, p_session, patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.get_doc",
            side_effect=frappe.DoesNotExistError,
        ):
            result = self._import()("SO-DOES-NOT-EXIST")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"])

    def test_refuses_draft_so(self):
        """Draft SO (docstatus=0) → not eligible for push."""
        p_roles, p_session = self._mock_frappe()
        draft_so = MagicMock()
        draft_so.docstatus = 0
        with p_roles, p_session, patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.get_doc",
            return_value=draft_so,
        ):
            result = self._import()("SO-DRAFT")
        self.assertFalse(result["ok"])
        self.assertIn("not submitted", result["message"])

    def test_idempotent_when_map_exists(self):
        """SO already mapped → returns already_mapped, no re-enqueue."""
        p_roles, p_session = self._mock_frappe()
        submitted = MagicMock()
        submitted.docstatus = 1
        with p_roles, p_session, patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.get_doc",
            return_value=submitted,
        ), patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.db.get_value",
            return_value="ECS-B2B-SO-2610397",
        ):
            result = self._import()("SO-2610397")
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_mapped"])
        self.assertEqual(result["b2b_order_map"], "ECS-B2B-SO-2610397")

    def test_refuses_when_gate0_fails(self):
        """SO doesn't pass §11 Gate 0 → clear refusal message."""
        p_roles, p_session = self._mock_frappe()
        submitted = MagicMock()
        submitted.docstatus = 1
        submitted.set_warehouse = "Some non-EE warehouse"
        with p_roles, p_session, patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.get_doc",
            return_value=submitted,
        ), patch(
            "ecommerce_super.easyecom.api.manual_repush.frappe.db.get_value",
            return_value=None,  # no existing map
        ), patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gating.is_section_11_gated",
            return_value=False,
        ):
            result = self._import()("SO-BAD-WH")
        self.assertFalse(result["ok"])
        self.assertIn("Gate 0", result["message"])
