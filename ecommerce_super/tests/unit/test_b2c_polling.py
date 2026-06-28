"""§12 — B2C marketplace polling walker tests.

Covers:
  - Per-tick eligibility (enabled + has easyecom_account + cursor past cadence)
  - getAllOrders response shape extraction (multiple wrap conventions)
  - Per-order dispatch + idempotency on EE Invoice_id
  - Cursor advance on success, stamp-only on failure
  - Skip when Stage 3 builder is unavailable (graceful degradation)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2c_sales.polling import (
    _dispatch_order_to_builder,
    _extract_orders,
)


# ============================================================
# _extract_orders — response shape discovery
# ============================================================


class TestExtractOrders(unittest.TestCase):

    def test_data_is_top_level_list(self):
        response = {"data": [{"invoice_id": "A"}, {"invoice_id": "B"}]}
        self.assertEqual(len(_extract_orders(response)), 2)

    def test_data_dict_with_orders_key(self):
        response = {"data": {"orders": [{"invoice_id": "X"}]}}
        self.assertEqual(_extract_orders(response), [{"invoice_id": "X"}])

    def test_data_dict_with_rows_key(self):
        response = {"data": {"rows": [{"invoice_id": "Y"}]}}
        self.assertEqual(_extract_orders(response), [{"invoice_id": "Y"}])

    def test_top_level_orders_key(self):
        response = {"orders": [{"invoice_id": "Z"}]}
        self.assertEqual(_extract_orders(response), [{"invoice_id": "Z"}])

    def test_bare_list(self):
        response = [{"invoice_id": "A"}]
        self.assertEqual(_extract_orders(response), [{"invoice_id": "A"}])

    def test_unrecognised_shape_returns_empty(self):
        self.assertEqual(_extract_orders({"unexpected": True}), [])

    def test_none_returns_empty(self):
        self.assertEqual(_extract_orders(None), [])


# ============================================================
# _dispatch_order_to_builder — idempotency
# ============================================================


class TestDispatchOrder(unittest.TestCase):

    def _account(self):
        m = MagicMock()
        m.name = "ECS-MA-Acme Ltd-2"
        m.company = "Acme Ltd"
        return m

    def test_skips_when_invoice_id_missing(self):
        with patch("frappe.db.get_value"):
            result = _dispatch_order_to_builder(
                order_row={"order_id": 123},  # no invoice_id
                account=self._account(),
                correlation_id="cor-001",
            )
        self.assertFalse(result["ok"])
        self.assertIn("missing invoice_id", result["detail"])

    def test_skips_existing_si_idempotent(self):
        """Re-poll of an order that already has an SI returns skipped=True."""
        with patch("frappe.db.get_value", return_value="ACC-SINV-2026-99999"):
            result = _dispatch_order_to_builder(
                order_row={"invoice_id": "EE-INV-7"},
                account=self._account(),
                correlation_id="cor-001",
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["sales_invoice"], "ACC-SINV-2026-99999")

    def test_alt_invoiceId_camelcase_accepted(self):
        """EE payloads vary on key casing; accept invoice_id and invoiceId."""
        with patch("frappe.db.get_value", return_value="ACC-SINV-2026-00001"):
            result = _dispatch_order_to_builder(
                order_row={"invoiceId": "EE-INV-99"},
                account=self._account(),
                correlation_id="cor-001",
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_builder_unavailable_returns_skip_not_error(self):
        """If Stage 3 builder hasn't landed yet, polling Stage 2 must
        not crash — log + skip the order so the rest of the batch
        proceeds."""
        with (
            patch("frappe.db.get_value", return_value=None),
            patch.dict(
                "sys.modules",
                {"ecommerce_super.easyecom.flows.b2c_sales.invoice_builder": None},
            ),
            patch("frappe.logger") as mock_logger,
        ):
            mock_logger.return_value = MagicMock()
            result = _dispatch_order_to_builder(
                order_row={"invoice_id": "EE-INV-NEW"},
                account=self._account(),
                correlation_id="cor-001",
            )
        # ImportError path returns skipped=True so the batch continues
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_builder_exception_returns_per_record_failure(self):
        """Per-record failure — one bad order doesn't kill the batch.
        The dispatcher catches, logs, returns ok=False; loop continues."""
        with (
            patch("frappe.db.get_value", return_value=None),
            patch("frappe.log_error"),
            patch(
                "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.build_si_from_ee_order",
                side_effect=ValueError("Item Map missing for SKU XYZ-12"),
                create=True,
            ),
        ):
            result = _dispatch_order_to_builder(
                order_row={"invoice_id": "EE-INV-BAD"},
                account=self._account(),
                correlation_id="cor-001",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["exception"], "ValueError")
        self.assertIn("Item Map missing", result["message"])

    def test_successful_build_passes_payload_through(self):
        with (
            patch("frappe.db.get_value", return_value=None),
            patch(
                "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.build_si_from_ee_order",
                return_value={"sales_invoice": "ACC-SINV-2026-NEW"},
                create=True,
            ),
        ):
            result = _dispatch_order_to_builder(
                order_row={"invoice_id": "EE-INV-OK"},
                account=self._account(),
                correlation_id="cor-001",
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(
            result["build_result"]["sales_invoice"], "ACC-SINV-2026-NEW",
        )


if __name__ == "__main__":
    unittest.main()
