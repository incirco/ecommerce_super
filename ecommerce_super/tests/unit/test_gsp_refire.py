"""Manual Re-fire endpoint for /einvoice/update — MTTR relief for
post-code-fix remediation of stuck B2B orders.

Locks:
  - Permission gate: only Administrator / System Manager / EasyEcom FDE
  - Missing Map name → clear throw
  - Missing SO on Map → early-error fail with clear message (not crash)
  - EE returns no rows → early-error fail (not crash)
  - Happy path: SI created + IRN minted → ok=True with names in response
  - Handler error → ok=False with the error surfaced (not swallowed)
  - Mint error after SI created → ok=False with BOTH SI name AND
    mint failure reason (partial-success reporting)
  - Comment logged on Map for every attempt (audit trail)
  - Comment failure never muffles the outcome response
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.api import gsp_refire as mod


def _mock_map(name="ECS-B2B-RETEST", sales_order="SO-RE-01",
              ee_account="EE-ACC-01", invoice_id="INV-999"):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.easyecom_account = ee_account
    m.invoice_id = invoice_id
    m.add_comment = MagicMock()
    return m


def _fresh_ee_row(invoice_id="INV-999"):
    """A plausible EE row shape as returned by getOrderDetails."""
    return {
        "invoice_id": invoice_id,
        "reference_code": "SO-RE-01",
        "merchant_c_id": 42,
        "total_amount": 1050.0,
        "order_items": [
            {"sku": "SKU-A", "item_quantity": 1, "taxable_value": 1000.0}
        ],
    }


def _allow(*_a, **_kw):
    """Bypass the permission gate — separately covered."""
    return None


class TestPermissionGate(unittest.TestCase):
    """Same pattern as gsp_dry_run's gate test — direct set/restore
    on frappe.session (which is a _dict, so patch.object doesn't
    work reliably on it)."""

    def _with_user(self, user: str, roles=None):
        original = mod.frappe.session.get("user")
        mod.frappe.session.user = user
        rp = None
        if roles is not None:
            rp = patch.object(mod.frappe, "get_roles", return_value=roles)
            rp.start()
        try:
            mod._require_refire_permission()
        finally:
            if rp is not None:
                rp.stop()
            if original is None:
                mod.frappe.session.pop("user", None)
            else:
                mod.frappe.session.user = original

    def test_administrator_passes(self):
        self._with_user("Administrator")

    def test_system_manager_passes(self):
        self._with_user("sm@x.com", roles=["System Manager"])

    def test_easyecom_fde_passes(self):
        self._with_user("fde@x.com", roles=["EasyEcom FDE"])

    def test_sales_user_refused(self):
        with self.assertRaises(Exception) as ctx:
            self._with_user("sales@x.com", roles=["Sales User"])
        msg = str(ctx.exception)
        self.assertIn("System Manager", msg)
        self.assertIn("EasyEcom FDE", msg)


class TestRefireInputValidation(unittest.TestCase):
    def test_empty_map_name_throws(self):
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            self.assertRaises(Exception) as ctx,
        ):
            mod.refire_einvoice(map_name="")
        self.assertIn("map_name", str(ctx.exception))

    def test_missing_map_throws(self):
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            patch.object(mod.frappe.db, "exists", return_value=False),
            self.assertRaises(Exception) as ctx,
        ):
            mod.refire_einvoice(map_name="GHOST-MAP")
        self.assertIn("not found", str(ctx.exception))


class TestRefireHappyPath(unittest.TestCase):
    """End-to-end successful re-fire: fetch → handler → mint → OK."""

    def _stack(self, *, ee_row=None, si_name="SI-RETEST-01",
               irn="IRN-abc123", ee_account="EE-ACC-01"):
        """Return the standard patch stack for a happy-path Re-fire."""
        map_doc = _mock_map()
        ee_row = ee_row or _fresh_ee_row()
        return {
            "patches": [
                patch.object(mod, "_require_refire_permission", _allow),
                patch.object(mod.frappe.db, "exists", return_value=True),
                patch.object(
                    mod, "_fetch_fresh_ee_row",
                    return_value=(ee_row, ee_account),
                ),
                patch(
                    "ecommerce_super.easyecom.api.gsp._elevated_session",
                    MagicMock(return_value=MagicMock(
                        __enter__=MagicMock(return_value=None),
                        __exit__=MagicMock(return_value=None),
                    )),
                ),
                patch(
                    "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                    "find_or_create_si_for_gsp",
                    return_value=si_name,
                ),
                patch(
                    "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                    "mint_irn_for_si",
                    return_value={
                        "data": {
                            "invoice_details": {
                                "irn_details": {"irn": irn}
                            }
                        }
                    },
                ),
                patch.object(mod.frappe, "get_doc", return_value=map_doc),
            ],
            "map_doc": map_doc,
        }

    def test_returns_ok_true_with_si_and_irn(self):
        stack = self._stack()
        for p in stack["patches"]:
            p.start()
        try:
            result = mod.refire_einvoice(map_name="ECS-B2B-RETEST")
        finally:
            for p in stack["patches"]:
                p.stop()

        self.assertTrue(result["ok"])
        self.assertEqual(result["sales_invoice"], "SI-RETEST-01")
        self.assertEqual(result["irn"], "IRN-abc123")
        self.assertIn("SI SI-RETEST-01", result["message"])
        self.assertIn("IRN IRN-abc123", result["message"])

    def test_success_adds_comment_to_map(self):
        stack = self._stack()
        for p in stack["patches"]:
            p.start()
        try:
            mod.refire_einvoice(map_name="ECS-B2B-RETEST")
        finally:
            for p in stack["patches"]:
                p.stop()
        stack["map_doc"].add_comment.assert_called_once()
        call_kwargs = stack["map_doc"].add_comment.call_args.kwargs
        text = call_kwargs.get("text", "")
        self.assertIn("Re-fire", text)
        self.assertIn("✓ success", text)
        self.assertIn("SI-RETEST-01", text)
        self.assertIn("IRN-abc123", text)


class TestRefireFailurePaths(unittest.TestCase):
    """Every failure path must return ok=False with a descriptive
    message — no propagating exceptions to the JS button."""

    def _run_with_early_error(self, message: str):
        """Simulate an early-fetch failure via _RefireEarlyError."""
        map_doc = _mock_map()
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(
                mod, "_fetch_fresh_ee_row",
                side_effect=mod._RefireEarlyError(message),
            ),
            patch.object(mod.frappe, "get_doc", return_value=map_doc),
        ):
            return mod.refire_einvoice(map_name="ECS-B2B-RETEST"), map_doc

    def test_early_error_missing_so_reported_cleanly(self):
        result, _ = self._run_with_early_error(
            "Map has no sales_order — cannot re-fire."
        )
        self.assertFalse(result["ok"])
        self.assertIn("no sales_order", result["message"])
        self.assertIsNone(result["sales_invoice"])
        self.assertIsNone(result["irn"])

    def test_early_error_ee_no_rows_reported_cleanly(self):
        result, _ = self._run_with_early_error(
            "EE returned no order rows for reference_code=SO-01"
        )
        self.assertFalse(result["ok"])
        self.assertIn("no order rows", result["message"])

    def test_unexpected_fetch_exception_captured(self):
        """A crash inside the EE call must be caught and reported —
        the JS button must always get a clean response."""
        map_doc = _mock_map()
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(
                mod, "_fetch_fresh_ee_row",
                side_effect=RuntimeError("EE endpoint 500"),
            ),
            patch.object(mod.frappe, "get_doc", return_value=map_doc),
        ):
            result = mod.refire_einvoice(map_name="ECS-B2B-RETEST")
        self.assertFalse(result["ok"])
        self.assertIn("Could not fetch EE row", result["message"])
        self.assertIn("EE endpoint 500", result["message"])

    def test_handler_failure_reports_reason(self):
        """SI find/create failed → GSPHandlerError → reported."""
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
            GSPHandlerError,
        )
        map_doc = _mock_map()
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(
                mod, "_fetch_fresh_ee_row",
                return_value=(_fresh_ee_row(), "EE-ACC-01"),
            ),
            patch(
                "ecommerce_super.easyecom.api.gsp._elevated_session",
                MagicMock(return_value=MagicMock(
                    __enter__=MagicMock(return_value=None),
                    __exit__=MagicMock(return_value=None),
                )),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                "find_or_create_si_for_gsp",
                side_effect=GSPHandlerError("no Customer Map for ee_c_id 42"),
            ),
            patch.object(mod.frappe, "get_doc", return_value=map_doc),
        ):
            result = mod.refire_einvoice(map_name="ECS-B2B-RETEST")
        self.assertFalse(result["ok"])
        self.assertIn("SI create/find failed", result["message"])
        self.assertIn("Customer Map", result["message"])
        self.assertIsNone(result["sales_invoice"])

    def test_mint_error_after_si_created_reports_both(self):
        """Partial success — SI created but IRN mint failed. Report
        BOTH the SI name AND the mint failure so FDE knows what state
        we're in."""
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
            GSPHandlerError,
        )
        map_doc = _mock_map()
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(
                mod, "_fetch_fresh_ee_row",
                return_value=(_fresh_ee_row(), "EE-ACC-01"),
            ),
            patch(
                "ecommerce_super.easyecom.api.gsp._elevated_session",
                MagicMock(return_value=MagicMock(
                    __enter__=MagicMock(return_value=None),
                    __exit__=MagicMock(return_value=None),
                )),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                "find_or_create_si_for_gsp",
                return_value="SI-PARTIAL-01",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                "mint_irn_for_si",
                side_effect=GSPHandlerError("NIC IRP 502 timeout"),
            ),
            patch.object(mod.frappe, "get_doc", return_value=map_doc),
        ):
            result = mod.refire_einvoice(map_name="ECS-B2B-RETEST")
        self.assertFalse(result["ok"])
        self.assertEqual(result["sales_invoice"], "SI-PARTIAL-01")
        self.assertIsNone(result["irn"])
        self.assertIn("SI SI-PARTIAL-01 created", result["message"])
        self.assertIn("IRN mint failed", result["message"])

    def test_failure_still_logs_comment_on_map(self):
        """Even on failure, the attempt must be logged so the audit
        trail is complete."""
        map_doc = _mock_map()
        with (
            patch.object(mod, "_require_refire_permission", _allow),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch.object(
                mod, "_fetch_fresh_ee_row",
                side_effect=mod._RefireEarlyError("test-early-fail"),
            ),
            patch.object(mod.frappe, "get_doc", return_value=map_doc),
        ):
            mod.refire_einvoice(map_name="ECS-B2B-RETEST")
        map_doc.add_comment.assert_called_once()
        text = map_doc.add_comment.call_args.kwargs.get("text", "")
        self.assertIn("✗ failed", text)
        self.assertIn("test-early-fail", text)


class TestCommentLoggingIsBestEffort(unittest.TestCase):
    """A Comment insert failure must not muffle the real outcome."""

    def test_comment_exception_does_not_propagate(self):
        map_doc = _mock_map()
        map_doc.add_comment.side_effect = Exception("comment insert failed")
        # Should NOT raise
        mod._log_refire_comment(
            "ECS-B2B-RETEST",
            {"ok": True, "sales_invoice": "SI-X", "irn": None, "message": "ok"},
        )


class TestExtractRowsShapes(unittest.TestCase):
    """The response envelope has several shapes; extractor handles all."""

    def test_response_is_a_list(self):
        rows = mod._extract_rows([{"invoice_id": "1"}, {"invoice_id": "2"}])
        self.assertEqual(len(rows), 2)

    def test_response_data_is_a_list(self):
        rows = mod._extract_rows(
            {"data": [{"invoice_id": "1"}, {"invoice_id": "2"}]}
        )
        self.assertEqual(len(rows), 2)

    def test_response_data_is_a_single_dict(self):
        rows = mod._extract_rows({"data": {"invoice_id": "1"}})
        self.assertEqual(len(rows), 1)

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(mod._extract_rows(None), [])
        self.assertEqual(mod._extract_rows({}), [])
        self.assertEqual(mod._extract_rows({"data": None}), [])


if __name__ == "__main__":
    unittest.main()
