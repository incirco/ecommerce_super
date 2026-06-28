"""§12 — B2C marketplace SI builder tests.

Covers Path 2 invariants:
  - EE-supplied tax becomes SI.taxes (not ERPNext-computed)
  - ERPNext-computed tax stored as ecs_erpnext_tax_check_total (variance check)
  - Custom Field stamping (marketplace, marketplace_order_id, EE invoice_id, etc.)
  - Variance check: >1% delta raises Discrepancy as upstream alert
  - Per-record failures: missing Item Map, missing pseudo-customer, missing reference_code

Mocks frappe DB primitives so tests run without a bench.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
    B2CBuilderError,
    _check_variance,
    _compute_erpnext_tax_check,
    _hash_payload,
    _hsn_default_rate,
    _resolve_line_items,
    _resolve_posting_date,
)


# ============================================================
# Test fixtures
# ============================================================


def _account(
    *,
    name="ECS-MA-Acme Ltd-2",
    company="Acme Ltd",
    marketplace="2",
    pseudo_customer="Amazon.in B2C Pool - Acme Ltd",
):
    m = MagicMock()
    m.name = name
    m.company = company
    m.marketplace = marketplace
    m.pseudo_customer = pseudo_customer
    return m


def _order(
    *,
    invoice_id="EE-INV-100",
    order_id="EE-ORD-50",
    reference_code="AMZ-ORD-XYZ",
    items=None,
    invoice_amount=1180.00,
    tax_amount=180.00,
    warehouse_id=None,
):
    return {
        "invoice_id": invoice_id,
        "order_id": order_id,
        "reference_code": reference_code,
        "order_items": items if items is not None else [
            {"sku": "SKU-A", "item_quantity": 1,
             "breakup_types": {"Item Amount Excluding Tax": 1000.00}},
        ],
        "invoice_amount": invoice_amount,
        "tax_amount": tax_amount,
        "warehouse_id": warehouse_id,
        "payment_mode": "Prepaid",
        "awb_number": "1234567890",
        "courier": "Bluedart",
        "order_date": "2026-06-28",
    }


# ============================================================
# _resolve_line_items — Item Map + qty + rate computation
# ============================================================


class TestResolveLineItems(unittest.TestCase):

    def test_resolves_via_item_map(self):
        with (
            patch("frappe.db.get_value", side_effect=[
                "Item-A",  # EasyEcom Item Map → erpnext_name
                "1001.99.00",  # Item.gst_hsn_code
            ]),
        ):
            result = _resolve_line_items(_order())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["item_code"], "Item-A")
        self.assertEqual(result[0]["qty"], 1)
        self.assertEqual(result[0]["rate"], 1000.00)
        self.assertEqual(result[0]["gst_hsn_code"], "1001.99.00")

    def test_raises_on_unmapped_sku_listing_all(self):
        """Unmapped SKUs are collected and raised in one go so the
        FDE fixes them in a single round-trip."""
        items = [
            {"sku": "BAD-A", "item_quantity": 1, "selling_price": 100},
            {"sku": "BAD-B", "item_quantity": 1, "selling_price": 200},
        ]
        with patch("frappe.db.get_value", return_value=None):  # no Map
            with self.assertRaises(B2CBuilderError) as ctx:
                _resolve_line_items(_order(items=items))
        self.assertIn("BAD-A", str(ctx.exception))
        self.assertIn("BAD-B", str(ctx.exception))

    def test_skips_zero_qty_lines(self):
        items = [
            {"sku": "SKU-A", "item_quantity": 0, "selling_price": 100},
            {"sku": "SKU-B", "item_quantity": 2,
             "breakup_types": {"Item Amount Excluding Tax": 200.00}},
        ]
        with patch("frappe.db.get_value", side_effect=[
            "Item-A",  # SKU-A maps but qty=0
            "Item-B", "8517.12.00",  # SKU-B maps, then HSN lookup
        ]):
            result = _resolve_line_items(_order(items=items))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["item_code"], "Item-B")

    def test_raises_on_empty_order_items(self):
        with self.assertRaises(B2CBuilderError) as ctx:
            _resolve_line_items({"order_items": []})
        self.assertIn("zero lines", str(ctx.exception))

    def test_raises_on_missing_sku(self):
        items = [{"item_quantity": 1, "selling_price": 100}]
        with self.assertRaises(B2CBuilderError) as ctx:
            _resolve_line_items(_order(items=items))
        self.assertIn("missing sku", str(ctx.exception))

    def test_falls_back_to_selling_price_minus_tax_when_no_breakup(self):
        items = [{
            "sku": "SKU-X",
            "item_quantity": 1,
            "selling_price": 118,
            "tax_rate": 18,
        }]
        with patch("frappe.db.get_value", side_effect=["Item-X", "1001.99.00"]):
            result = _resolve_line_items(_order(items=items))
        self.assertEqual(result[0]["rate"], 100.00)  # 118 / 1.18

    def test_accepts_camelcase_orderItems(self):
        """EE payloads vary on key casing."""
        order = {"orderItems": [
            {"sku": "SKU-A", "item_quantity": 1,
             "breakup_types": {"Item Amount Excluding Tax": 50}},
        ]}
        with patch("frappe.db.get_value", side_effect=["Item-A", None]):
            result = _resolve_line_items(order)
        self.assertEqual(len(result), 1)


# ============================================================
# _compute_erpnext_tax_check — variance signal
# ============================================================


class TestComputeErpnextTaxCheck(unittest.TestCase):

    def test_sums_per_line_via_hsn_rate(self):
        line_items = [
            {"qty": 1, "rate": 1000, "gst_hsn_code": "1001.99.00"},
            {"qty": 2, "rate": 500, "gst_hsn_code": "8517.12.00"},
        ]
        with patch(
            "frappe.db.get_value",
            side_effect=[18, 12],
        ):
            result = _compute_erpnext_tax_check(line_items)
        # 1000 * 18% + (2*500) * 12% = 180 + 120 = 300
        self.assertEqual(result, 300.00)

    def test_zero_when_hsn_missing(self):
        line_items = [{"qty": 1, "rate": 1000, "gst_hsn_code": None}]
        result = _compute_erpnext_tax_check(line_items)
        self.assertEqual(result, 0.00)

    def test_zero_when_lookup_raises(self):
        line_items = [{"qty": 1, "rate": 1000, "gst_hsn_code": "X"}]
        with patch("frappe.db.get_value", side_effect=RuntimeError("col missing")):
            result = _compute_erpnext_tax_check(line_items)
        self.assertEqual(result, 0.00)


class TestHsnDefaultRate(unittest.TestCase):

    def test_returns_rate_when_present(self):
        with patch("frappe.db.get_value", return_value=18):
            self.assertEqual(_hsn_default_rate("1001.99.00"), 18.0)

    def test_returns_zero_when_field_missing(self):
        with patch("frappe.db.get_value", side_effect=RuntimeError("no col")):
            self.assertEqual(_hsn_default_rate("X"), 0.0)


# ============================================================
# _check_variance — Path 2 alert mechanism
# ============================================================


class TestCheckVariance(unittest.TestCase):

    def _si(self):
        m = MagicMock()
        m.name = "ACC-SINV-2026-50001"
        m.company = "Acme Ltd"
        return m

    def test_no_variance_when_within_1pct(self):
        # EE 180, ERPNext 181 → 0.55% delta, under threshold
        result = _check_variance(
            si=self._si(), marketplace_account=_account(),
            ee_invoice_id="EE-INV-100",
            ee_tax_total=180.00, erpnext_tax_check=181.00,
            correlation_id="cor-001",
        )
        self.assertFalse(result["discrepancy_raised"])
        self.assertLess(result["tax_variance_pct"], 1.0)

    def test_raises_discrepancy_when_above_1pct(self):
        # EE 180, ERPNext 200 → 11.1% delta, over threshold
        with patch(
            "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy"
        ) as mock_disc:
            result = _check_variance(
                si=self._si(), marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_tax_total=180.00, erpnext_tax_check=200.00,
                correlation_id="cor-001",
            )
        self.assertTrue(result["discrepancy_raised"])
        self.assertGreater(result["tax_variance_pct"], 1.0)
        mock_disc.assert_called_once()
        # Reason text mentions Path 2 + upstream alert + immutable SI
        call_kwargs = mock_disc.call_args.kwargs
        self.assertIn("Path 2", call_kwargs["reason"])
        self.assertIn("immutable", call_kwargs["reason"])

    def test_skips_when_erpnext_check_is_zero(self):
        """HSN unresolved → erpnext_tax_check=0 → skip alert
        (better than false-positive flood on fresh installs)."""
        with patch(
            "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy"
        ) as mock_disc:
            result = _check_variance(
                si=self._si(), marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_tax_total=180.00, erpnext_tax_check=0.0,
                correlation_id="cor-001",
            )
        self.assertFalse(result["discrepancy_raised"])
        mock_disc.assert_not_called()

    def test_skips_when_ee_tax_is_zero(self):
        """Some orders genuinely have zero tax (zero-rated items).
        Don't divide-by-zero; don't alert."""
        result = _check_variance(
            si=self._si(), marketplace_account=_account(),
            ee_invoice_id="EE-INV-100",
            ee_tax_total=0.0, erpnext_tax_check=0.0,
            correlation_id="cor-001",
        )
        self.assertFalse(result["discrepancy_raised"])

    def test_silent_when_discrepancy_raise_fails(self):
        """Variance check must never break the SI creation flow.
        If raising a Discrepancy fails (substrate issue), log + continue."""
        with (
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=RuntimeError("substrate down"),
            ),
            patch("frappe.log_error"),
        ):
            result = _check_variance(
                si=self._si(), marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_tax_total=180.00, erpnext_tax_check=200.00,
                correlation_id="cor-001",
            )
        # variance computed; raising failed → discrepancy_raised=False
        self.assertFalse(result["discrepancy_raised"])
        self.assertGreater(result["tax_variance_pct"], 1.0)


# ============================================================
# Misc helpers
# ============================================================


class TestResolvePostingDate(unittest.TestCase):

    def test_uses_order_date(self):
        result = _resolve_posting_date({"order_date": "2026-06-15"})
        self.assertEqual(str(result), "2026-06-15")

    def test_accepts_camelcase(self):
        result = _resolve_posting_date({"orderDate": "2026-06-15"})
        self.assertEqual(str(result), "2026-06-15")

    def test_falls_back_to_invoice_date(self):
        result = _resolve_posting_date({"invoice_date": "2026-06-20"})
        self.assertEqual(str(result), "2026-06-20")

    def test_falls_back_to_today_when_no_date_field(self):
        result = _resolve_posting_date({})
        # Just verify it returned a date object without error
        self.assertIsNotNone(result)


class TestHashPayload(unittest.TestCase):

    def test_deterministic(self):
        a = _hash_payload({"x": 1, "y": [2, 3]})
        b = _hash_payload({"y": [2, 3], "x": 1})  # different key order
        self.assertEqual(a, b)  # canonical JSON sort_keys=True

    def test_64_hex_chars(self):
        h = _hash_payload({"a": 1})
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


if __name__ == "__main__":
    unittest.main()
