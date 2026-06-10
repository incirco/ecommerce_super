"""gh#37 — `trace_item` diagnostic returns a structured walk of every
§8d gate + downstream artifact.

The user observed "Item updates not synced or mapped across ERPNext
and EasyEcom" but our code paths are correct — the issue is silent
gating (`auto_push_on_save=0` is the default; pull is manual-only).
This endpoint exposes the gates and the artifacts so the FDE can
diagnose without speculation.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


class TestTraceItemShape(unittest.TestCase):
    def _patch_permissions(self):
        return patch(
            "frappe.get_roles",
            return_value=["System Manager", "EasyEcom System Manager"],
        )

    def test_missing_item_returns_failed_item_exists_gate(self) -> None:
        from ecommerce_super.easyecom.api import item_sync_diagnostic

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=False),
        ):
            result = item_sync_diagnostic.trace_item(item_code="NOT-AN-ITEM")

        self.assertFalse(result["ok"])
        self.assertEqual(result["push_gates"][0]["gate"], "item_exists")
        self.assertFalse(result["push_gates"][0]["passed"])

    def test_auto_push_off_yields_off_verdict(self) -> None:
        """The headline gh#37 symptom — auto_push_on_save=0 — should
        produce a self-explanatory verdict that names the toggle and
        the workaround (manual button)."""
        from ecommerce_super.easyecom.api import item_sync_diagnostic

        fake_item = type("FakeItem", (), {})()
        fake_item.name = "WIDGET-001"
        fake_item.has_variants = 0
        fake_item.disabled = 0

        def _get_value(doctype, _filters, _fields, **_kwargs):
            if doctype == "EasyEcom Account":
                return type("R", (), {
                    "name": "EE-ACC",
                    "auto_push_on_save": 0,
                    "item_master_mode": "onboarding",
                })()
            return None

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=fake_item),
            patch.object(frappe.db, "get_value", side_effect=_get_value),
            patch.object(frappe.db, "get_all", return_value=[]),
        ):
            result = item_sync_diagnostic.trace_item(item_code="WIDGET-001")

        self.assertTrue(result["ok"])
        # auto_push gate is the failing one.
        gates = {g["gate"]: g for g in result["push_gates"]}
        self.assertIn("auto_push_on_save", gates)
        self.assertFalse(gates["auto_push_on_save"]["passed"])
        # Verdict names the toggle explicitly so the FDE knows what to fix.
        self.assertIn("Auto-push on save is OFF", result["verdict"])
        self.assertIn("Push to EasyEcom", result["verdict"])

    def test_response_has_all_expected_top_level_keys(self) -> None:
        from ecommerce_super.easyecom.api import item_sync_diagnostic

        fake_item = type("FakeItem", (), {})()
        fake_item.name = "WIDGET-002"
        fake_item.has_variants = 0
        fake_item.disabled = 0

        def _get_value(doctype, _filters, _fields, **_kwargs):
            if doctype == "EasyEcom Account":
                return type("R", (), {
                    "name": "EE-ACC",
                    "auto_push_on_save": 1,
                    "item_master_mode": "erpnext_mastered",
                })()
            if doctype == "EasyEcom Item Map":
                return {
                    "name": "ECS-IM-001",
                    "status": "Mapped",
                    "ee_product_id": "EE-PROD-9001",
                    "ee_cp_id": "EE-CP-9001",
                    "ee_sku": "WIDGET-002",
                    "flag_reason": None,
                }
            return None

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=fake_item),
            patch.object(frappe.db, "get_value", side_effect=_get_value),
            patch.object(frappe.db, "get_all", return_value=[]),
        ):
            result = item_sync_diagnostic.trace_item(item_code="WIDGET-002")

        # Top-level shape the form JS reads.
        for key in ("ok", "item_code", "push_gates", "pull_state", "downstream", "verdict"):
            self.assertIn(key, result)
        # Downstream buckets the JS expects.
        for key in ("item_map", "sync_records", "queue_jobs", "api_calls"):
            self.assertIn(key, result["downstream"])

    def test_non_role_caller_throws_permission_error(self) -> None:
        from ecommerce_super.easyecom.api import item_sync_diagnostic

        with (
            patch("frappe.get_roles", return_value=[]),
            self.assertRaises(frappe.PermissionError),
        ):
            item_sync_diagnostic.trace_item(item_code="WIDGET-003")


if __name__ == "__main__":
    unittest.main()
