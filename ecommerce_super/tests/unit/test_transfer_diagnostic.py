"""gh#26 — `trace_dn` diagnostic returns a structured walk of every
§10 gate and downstream artifact.

The endpoint is read-only: it inspects existing DB state and walks the
same gate logic the on_submit hook uses. These tests verify the shape
the form JS depends on — gates list, downstream dict (transfer_map,
sync_records, queue_jobs, api_calls), and the human-readable verdict
string.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


class TestTraceDnShape(unittest.TestCase):
    def _patch_permissions(self):
        return patch(
            "frappe.get_roles",
            return_value=["System Manager", "EasyEcom System Manager"],
        )

    def test_missing_dn_returns_failed_dn_exists_gate(self) -> None:
        from ecommerce_super.easyecom.api import transfer_diagnostic

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=False),
        ):
            result = transfer_diagnostic.trace_dn(dn_name="MAT-DN-DOESNT-EXIST")

        self.assertFalse(result["ok"])
        self.assertEqual(result["gates"][0]["gate"], "dn_exists")
        self.assertFalse(result["gates"][0]["passed"])

    def test_response_has_all_expected_top_level_keys(self) -> None:
        from ecommerce_super.easyecom.api import transfer_diagnostic

        # Mock the simplest passing-DN scenario the form JS depends on
        # — `get_doc`, gate helpers, and the downstream queries.
        fake_dn = type("FakeDN", (), {})()
        fake_dn.docstatus = 1
        fake_dn.is_internal_customer = 1
        fake_dn.name = "MAT-DN-2026-00001"
        fake_dn.company = "_Test Company"

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=fake_dn),
            patch(
                "ecommerce_super.easyecom.api.transfer_diagnostic._resolve_source_target_pair",
                return_value=("WH-A - TC", "WH-B - TC"),
            ),
            patch(
                "ecommerce_super.easyecom.api.transfer_diagnostic._is_ee_mapped_warehouse",
                return_value=True,
            ),
            patch("frappe.db.get_value", return_value=None),
            patch("frappe.db.get_all", return_value=[]),
        ):
            result = transfer_diagnostic.trace_dn(dn_name=fake_dn.name)

        # Top-level shape the form JS reads.
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dn_name"], fake_dn.name)
        self.assertIn("gates", result)
        self.assertIn("downstream", result)
        self.assertIn("verdict", result)
        # Downstream buckets the JS expects.
        for key in ("transfer_map", "sync_records", "queue_jobs", "api_calls"):
            self.assertIn(key, result["downstream"])

    def test_no_artifacts_yields_distinctive_verdict(self) -> None:
        """When every gate passes but no Transfer Map exists, the
        verdict should point the FDE at the on_submit-hook /
        Error-Log hypothesis."""
        from ecommerce_super.easyecom.api import transfer_diagnostic

        fake_dn = type("FakeDN", (), {})()
        fake_dn.docstatus = 1
        fake_dn.is_internal_customer = 1
        fake_dn.name = "MAT-DN-2026-00002"
        fake_dn.company = "_Test Company"

        with (
            self._patch_permissions(),
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=fake_dn),
            patch(
                "ecommerce_super.easyecom.api.transfer_diagnostic._resolve_source_target_pair",
                return_value=("WH-A - TC", "WH-B - TC"),
            ),
            patch(
                "ecommerce_super.easyecom.api.transfer_diagnostic._is_ee_mapped_warehouse",
                return_value=True,
            ),
            patch("frappe.db.get_value", return_value=None),  # no transfer_map
            patch("frappe.db.get_all", return_value=[]),
        ):
            result = transfer_diagnostic.trace_dn(dn_name=fake_dn.name)

        self.assertIn("All gates passed but no Transfer Map", result["verdict"])

    def test_non_role_caller_throws_permission_error(self) -> None:
        from ecommerce_super.easyecom.api import transfer_diagnostic

        with (
            patch("frappe.get_roles", return_value=[]),
            self.assertRaises(frappe.PermissionError),
        ):
            transfer_diagnostic.trace_dn(dn_name="MAT-DN-2026-00099")


if __name__ == "__main__":
    unittest.main()
