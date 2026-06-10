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


def _account_row(*, name="EE-ACC", auto_push=0, mode="onboarding"):
    return {"name": name, "auto_push_on_save": auto_push, "item_master_mode": mode}


def _fake_item(*, name="WIDGET", has_variants=0, disabled=0):
    item = type("FakeItem", (), {})()
    item.name = name
    item.has_variants = has_variants
    item.disabled = disabled
    return item


class TestTraceItemShape(unittest.TestCase):
    def _patch_permissions(self):
        return patch(
            "frappe.get_roles",
            return_value=["System Manager", "EasyEcom System Manager"],
        )

    def _run(
        self,
        *,
        item,
        accounts: list[dict],
        item_map: dict | None = None,
        sync_records: list | None = None,
        queue_jobs: list | None = None,
        api_calls: list | None = None,
    ):
        """Drive the diagnostic with mocked Frappe surfaces matching the
        new query pattern (accounts via get_all, item_map via get_value,
        sync_records/queue_jobs/api_calls via get_all)."""
        from ecommerce_super.easyecom.api import item_sync_diagnostic

        def _get_value(doctype, _filters, _fields=None, **_kwargs):
            if doctype == "EasyEcom Item Map":
                return item_map
            if doctype == "EasyEcom Account":
                # The pull-state block reads item_pull_cursor_at via
                # get_value — return None for the smoke tests.
                return None
            return None

        def _get_all(doctype, **kwargs):
            if doctype == "EasyEcom Account":
                return [type("R", (), row)() for row in accounts]
            if doctype == "EasyEcom Sync Record":
                return sync_records or []
            if doctype == "EasyEcom Queue Job":
                return queue_jobs or []
            if doctype == "EasyEcom API Call":
                return api_calls or []
            return []

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=item),
            patch.object(frappe.db, "get_value", side_effect=_get_value),
            patch.object(frappe.db, "get_all", side_effect=_get_all),
        ):
            return item_sync_diagnostic.trace_item(item_code=item.name)

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
        result = self._run(
            item=_fake_item(name="WIDGET-001"),
            accounts=[_account_row(auto_push=0)],
        )

        self.assertTrue(result["ok"])
        gates = {g["gate"]: g for g in result["push_gates"]}
        self.assertIn("auto_push_on_save", gates)
        self.assertFalse(gates["auto_push_on_save"]["passed"])
        self.assertIn("Auto-push on save is OFF", result["verdict"])
        self.assertIn("Push to EasyEcom", result["verdict"])

    def test_response_has_all_expected_top_level_keys(self) -> None:
        result = self._run(
            item=_fake_item(name="WIDGET-002"),
            accounts=[_account_row(auto_push=1, mode="erpnext_mastered")],
            item_map={
                "name": "ECS-IM-001",
                "status": "Mapped",
                "ee_product_id": "EE-PROD-9001",
                "ee_cp_id": "EE-CP-9001",
                "ee_sku": "WIDGET-002",
                "flag_reason": None,
            },
        )

        for key in ("ok", "item_code", "push_gates", "pull_state", "downstream", "verdict"):
            self.assertIn(key, result)
        for key in ("item_map", "sync_records", "queue_jobs", "api_calls"):
            self.assertIn(key, result["downstream"])

    def test_non_role_caller_throws_permission_error(self) -> None:
        from ecommerce_super.easyecom.api import item_sync_diagnostic

        with (
            patch("frappe.get_roles", return_value=[]),
            self.assertRaises(frappe.PermissionError),
        ):
            item_sync_diagnostic.trace_item(item_code="WIDGET-003")

    def test_pushed_but_stuck_on_ee_verdict(self) -> None:
        """Map row exists with status=Flagged but ee_product_id blank —
        push attempted, EE rejected. Verdict must name flag_reason +
        point at the form button to re-push, not at the toggle."""
        result = self._run(
            item=_fake_item(name="WIDGET-004"),
            accounts=[_account_row(auto_push=1, mode="erpnext_mastered")],
            item_map={
                "name": "ECS-IM-004",
                "status": "Flagged-Not-Created",
                "ee_product_id": None,
                "ee_cp_id": None,
                "ee_sku": None,
                "flag_reason": "ProductTaxCode (HSN) missing",
            },
        )

        self.assertIn("Map row exists", result["verdict"])
        self.assertIn("ee_product_id", result["verdict"])
        self.assertIn("ProductTaxCode (HSN) missing", result["verdict"])
        self.assertIn("re-push", result["verdict"])

    def test_pushed_success_verdict_cites_ee_product_id(self) -> None:
        """When the Item is mapped AND ee_product_id is set, verdict
        confirms green-path and points the FDE at Discover Products
        if they need to pull EE-side state back."""
        result = self._run(
            item=_fake_item(name="WIDGET-005"),
            accounts=[_account_row(auto_push=1, mode="erpnext_mastered")],
            item_map={
                "name": "ECS-IM-005",
                "status": "Mapped",
                "ee_product_id": "EE-PROD-9005",
                "ee_cp_id": "EE-CP-9005",
                "ee_sku": "WIDGET-005",
                "flag_reason": None,
            },
        )

        self.assertIn("mapped to EE", result["verdict"])
        self.assertIn("EE-PROD-9005", result["verdict"])
        self.assertIn("Discover Products", result["verdict"])

    def test_multi_account_ambiguity_surfaced_in_gate_detail(self) -> None:
        """When more than one EasyEcom Account has enabled=1, push
        silently picks first-by-name. The diagnostic must surface that
        ambiguity so the FDE knows to disable the others."""
        result = self._run(
            item=_fake_item(name="WIDGET-006"),
            accounts=[
                _account_row(name="ACC-A", auto_push=1),
                _account_row(name="ACC-B", auto_push=0),
            ],
        )

        account_gate = next(
            g for g in result["push_gates"]
            if g["gate"] == "easyecom_account_enabled"
        )
        self.assertTrue(account_gate["passed"])
        self.assertIn("multiple Accounts", account_gate["detail"])
        self.assertIn("ACC-A", account_gate["detail"])


if __name__ == "__main__":
    unittest.main()
