"""§12 — B2C marketplace SI builder tests.

Covers Path 2 invariants + the in-state / out-of-state pool resolution:
  - EE-supplied tax becomes SI.taxes (not ERPNext-computed)
  - ERPNext-computed tax stored as ecs_erpnext_tax_check_total
  - Variance > 1% → Discrepancy as upstream alert
  - Pool customer picked by shipping state vs Company state
  - Sync Record written for audit (replaces Marketplace Order Map)
  - Per-record failures: missing Item Map, missing pool customer, missing reference_code
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
    B2CBuilderError,
    _check_total_variance,
    _check_variance,
    _compute_erpnext_tax_check,
    _gstin_state_code_to_name,
    _hash_payload,
    _hsn_default_rate,
    _normalise_state,
    _resolve_company_state,
    _resolve_line_items,
    _resolve_pool_customer,
    _resolve_posting_date,
    _resolve_shipping_state,
)


# ============================================================
# Test fixtures
# ============================================================


def _account(
    *,
    name="ECS-MA-Acme Ltd-2",
    company="Acme Ltd",
    marketplace="2",
    pseudo_customer_in_state="Amazon.in B2C In-State - Acme Ltd",
    pseudo_customer_out_of_state="Amazon.in B2C Out-of-State - Acme Ltd",
):
    m = MagicMock()
    m.name = name
    m.company = company
    m.marketplace = marketplace
    state = {
        "pseudo_customer_in_state": pseudo_customer_in_state,
        "pseudo_customer_out_of_state": pseudo_customer_out_of_state,
    }
    m.get.side_effect = lambda key, d=None: state.get(key, d)
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
    shipping_state=None,
):
    payload: dict = {
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
    if shipping_state:
        payload["shipping_address"] = {"state": shipping_state}
    return payload


# ============================================================
# Pool customer resolution (in-state vs out-of-state)
# ============================================================


class TestResolvePoolCustomer(unittest.TestCase):

    def test_picks_in_state_when_shipping_matches_company(self):
        with patch("frappe.db.get_value", return_value="Karnataka"):
            result = _resolve_pool_customer(
                marketplace_account=_account(),
                order_row=_order(shipping_state="Karnataka"),
            )
        self.assertEqual(result["kind"], "in_state")
        self.assertIn("In-State", result["customer"])

    def test_picks_out_of_state_when_shipping_differs(self):
        with patch("frappe.db.get_value", return_value="Karnataka"):
            result = _resolve_pool_customer(
                marketplace_account=_account(),
                order_row=_order(shipping_state="Maharashtra"),
            )
        self.assertEqual(result["kind"], "out_of_state")
        self.assertIn("Out-of-State", result["customer"])

    def test_case_insensitive_state_comparison(self):
        with patch("frappe.db.get_value", return_value="Karnataka"):
            result = _resolve_pool_customer(
                marketplace_account=_account(),
                order_row=_order(shipping_state="karnataka"),  # lowercase
            )
        self.assertEqual(result["kind"], "in_state")

    def test_defaults_to_in_state_when_shipping_unknown(self):
        """Safer to over-charge CGST+SGST (variance surfaces) than
        under-charge IGST silently."""
        with patch("frappe.db.get_value", return_value="Karnataka"):
            result = _resolve_pool_customer(
                marketplace_account=_account(),
                order_row=_order(),  # no shipping_state
            )
        self.assertEqual(result["kind"], "in_state")

    def test_defaults_to_in_state_when_company_state_unknown(self):
        with patch("frappe.db.get_value", return_value=None):
            result = _resolve_pool_customer(
                marketplace_account=_account(),
                order_row=_order(shipping_state="Maharashtra"),
            )
        self.assertEqual(result["kind"], "in_state")

    def test_raises_when_both_pools_missing(self):
        bad_account = _account(
            pseudo_customer_in_state=None,
            pseudo_customer_out_of_state=None,
        )
        with self.assertRaises(B2CBuilderError) as ctx:
            _resolve_pool_customer(
                marketplace_account=bad_account,
                order_row=_order(shipping_state="Karnataka"),
            )
        self.assertIn("no pool customers", str(ctx.exception))

    def test_raises_when_needed_pool_missing(self):
        """Order ships in-state but only the out-of-state pool exists."""
        bad_account = _account(pseudo_customer_in_state=None)
        with patch("frappe.db.get_value", return_value="Karnataka"):
            with self.assertRaises(B2CBuilderError) as ctx:
                _resolve_pool_customer(
                    marketplace_account=bad_account,
                    order_row=_order(shipping_state="Karnataka"),
                )
        self.assertIn("pseudo_customer_in_state", str(ctx.exception))


# ============================================================
# State resolution helpers
# ============================================================


class TestResolveCompanyState(unittest.TestCase):

    def test_returns_company_state_field_when_set(self):
        with patch("frappe.db.get_value", return_value="Karnataka"):
            self.assertEqual(_resolve_company_state("Acme Ltd"), "Karnataka")

    def test_derives_from_gstin_when_state_missing(self):
        """Company.state blank → derive from GSTIN prefix (29 = Karnataka)."""
        with patch(
            "frappe.db.get_value",
            side_effect=[None, "29AAACA1234B1Z5"],
        ):
            self.assertEqual(_resolve_company_state("Acme Ltd"), "Karnataka")

    def test_returns_none_when_both_state_and_gstin_missing(self):
        with patch("frappe.db.get_value", side_effect=[None, None]):
            self.assertIsNone(_resolve_company_state("Acme Ltd"))


class TestResolveShippingState(unittest.TestCase):

    def test_picks_nested_shipping_address_state(self):
        self.assertEqual(
            _resolve_shipping_state({"shipping_address": {"state": "Maharashtra"}}),
            "Maharashtra",
        )

    def test_picks_camelcase_shippingAddress(self):
        self.assertEqual(
            _resolve_shipping_state({"shippingAddress": {"stateName": "Tamil Nadu"}}),
            "Tamil Nadu",
        )

    def test_picks_flat_shipping_state(self):
        self.assertEqual(
            _resolve_shipping_state({"shipping_state": "Delhi"}),
            "Delhi",
        )

    def test_picks_flat_buyer_state(self):
        self.assertEqual(
            _resolve_shipping_state({"buyer_state": "Gujarat"}),
            "Gujarat",
        )

    def test_returns_none_when_no_state_field(self):
        self.assertIsNone(_resolve_shipping_state({}))


class TestNormaliseState(unittest.TestCase):

    def test_strips_whitespace(self):
        self.assertEqual(_normalise_state("  Karnataka  "), "karnataka")

    def test_lowercases(self):
        self.assertEqual(_normalise_state("MAHARASHTRA"), "maharashtra")


class TestGstinStateCode(unittest.TestCase):

    def test_known_codes(self):
        self.assertEqual(_gstin_state_code_to_name("29"), "Karnataka")
        self.assertEqual(_gstin_state_code_to_name("27"), "Maharashtra")
        self.assertEqual(_gstin_state_code_to_name("07"), "Delhi")

    def test_unknown_returns_none(self):
        self.assertIsNone(_gstin_state_code_to_name("99X"))


# ============================================================
# _resolve_line_items — Item Map + qty + rate computation
# ============================================================


class TestResolveLineItems(unittest.TestCase):

    def test_resolves_via_item_map(self):
        with patch("frappe.db.get_value", side_effect=["Item-A", "1001.99.00"]):
            result = _resolve_line_items(_order())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["item_code"], "Item-A")
        self.assertEqual(result[0]["qty"], 1)
        self.assertEqual(result[0]["rate"], 1000.00)
        self.assertEqual(result[0]["gst_hsn_code"], "1001.99.00")

    def test_raises_on_unmapped_sku_listing_all(self):
        items = [
            {"sku": "BAD-A", "item_quantity": 1, "selling_price": 100},
            {"sku": "BAD-B", "item_quantity": 1, "selling_price": 200},
        ]
        with patch("frappe.db.get_value", return_value=None):
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
            "Item-A",
            "Item-B", "8517.12.00",
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
            "sku": "SKU-X", "item_quantity": 1,
            "selling_price": 118, "tax_rate": 18,
        }]
        with patch("frappe.db.get_value", side_effect=["Item-X", "1001.99.00"]):
            result = _resolve_line_items(_order(items=items))
        self.assertEqual(result[0]["rate"], 100.00)


# ============================================================
# _compute_erpnext_tax_check
# ============================================================


class TestComputeErpnextTaxCheck(unittest.TestCase):

    def test_sums_per_line_via_hsn_rate(self):
        line_items = [
            {"qty": 1, "rate": 1000, "gst_hsn_code": "1001.99.00"},
            {"qty": 2, "rate": 500, "gst_hsn_code": "8517.12.00"},
        ]
        with patch("frappe.db.get_value", side_effect=[18, 12]):
            result = _compute_erpnext_tax_check(line_items)
        self.assertEqual(result, 300.00)

    def test_zero_when_hsn_missing(self):
        line_items = [{"qty": 1, "rate": 1000, "gst_hsn_code": None}]
        self.assertEqual(_compute_erpnext_tax_check(line_items), 0.00)


class TestHsnDefaultRate(unittest.TestCase):

    def test_returns_rate_when_present(self):
        with patch("frappe.db.get_value", return_value=18):
            self.assertEqual(_hsn_default_rate("1001.99.00"), 18.0)

    def test_returns_zero_when_field_missing(self):
        with patch("frappe.db.get_value", side_effect=RuntimeError("no col")):
            self.assertEqual(_hsn_default_rate("X"), 0.0)


# ============================================================
# _check_variance
# ============================================================


class TestCheckVariance(unittest.TestCase):

    def _si(self):
        m = MagicMock()
        m.name = "ACC-SINV-2026-50001"
        m.company = "Acme Ltd"
        return m

    def test_no_variance_when_within_1pct(self):
        result = _check_variance(
            si=self._si(), marketplace_account=_account(),
            ee_invoice_id="EE-INV-100",
            ee_tax_total=180.00, erpnext_tax_check=181.00,
            correlation_id="cor-001",
        )
        self.assertFalse(result["discrepancy_raised"])
        self.assertLess(result["tax_variance_pct"], 1.0)

    def test_raises_discrepancy_when_above_1pct(self):
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
        mock_disc.assert_called_once()
        call_kwargs = mock_disc.call_args.kwargs
        self.assertIn("Path 2", call_kwargs["reason"])
        self.assertIn("immutable", call_kwargs["reason"])

    def test_skips_when_erpnext_check_is_zero(self):
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
        result = _check_variance(
            si=self._si(), marketplace_account=_account(),
            ee_invoice_id="EE-INV-100",
            ee_tax_total=0.0, erpnext_tax_check=0.0,
            correlation_id="cor-001",
        )
        self.assertFalse(result["discrepancy_raised"])


# ============================================================
# _check_total_variance — §12.9 1-paisa check
# ============================================================


class TestCheckTotalVariance(unittest.TestCase):

    def _si(self, grand_total: float = 1180.00):
        m = MagicMock()
        m.name = "ACC-SINV-2026-60001"
        m.company = "Acme Ltd"
        m.get.side_effect = lambda key, d=None: (
            grand_total if key == "grand_total" else d
        )
        return m

    def test_no_alert_when_totals_match_to_the_paisa(self):
        result = _check_total_variance(
            si=self._si(grand_total=1180.00),
            marketplace_account=_account(),
            ee_invoice_id="EE-INV-100",
            ee_grand_total=1180.00,
            correlation_id="cor-001",
        )
        self.assertFalse(result["discrepancy_raised"])
        self.assertEqual(result["total_variance_paise"], 0)

    def test_no_alert_when_delta_is_one_paisa(self):
        """1 paisa is exactly at threshold — within tolerance."""
        result = _check_total_variance(
            si=self._si(grand_total=1180.00),
            marketplace_account=_account(),
            ee_invoice_id="EE-INV-100",
            ee_grand_total=1180.01,
            correlation_id="cor-001",
        )
        self.assertFalse(result["discrepancy_raised"])
        self.assertEqual(result["total_variance_paise"], 1)

    def test_raises_when_delta_exceeds_one_paisa(self):
        """1.5 paisa rounds to 2 → exceeds threshold → Discrepancy."""
        with patch(
            "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy"
        ) as mock_disc:
            result = _check_total_variance(
                si=self._si(grand_total=1180.00),
                marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_grand_total=1180.02,
                correlation_id="cor-001",
            )
        self.assertTrue(result["discrepancy_raised"])
        self.assertGreater(result["total_variance_paise"], 1)
        mock_disc.assert_called_once()
        call_kwargs = mock_disc.call_args.kwargs
        self.assertIn("§12.9", call_kwargs["kind"])
        self.assertIn("Path 2", call_kwargs["reason"])
        self.assertIn("UPSTREAM-ISSUE", call_kwargs["reason"])

    def test_raises_for_large_delta(self):
        """₹10 delta — clearly an upstream bug."""
        with patch(
            "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy"
        ) as mock_disc:
            result = _check_total_variance(
                si=self._si(grand_total=1170.00),
                marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_grand_total=1180.00,
                correlation_id="cor-001",
            )
        self.assertTrue(result["discrepancy_raised"])
        self.assertEqual(result["total_variance_paise"], 1000)  # ₹10.00 = 1000 paise
        mock_disc.assert_called_once()

    def test_skips_when_ee_total_is_zero(self):
        """Refund-only / promotional orders may have zero total —
        don't alert."""
        with patch(
            "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy"
        ) as mock_disc:
            result = _check_total_variance(
                si=self._si(grand_total=0),
                marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_grand_total=0,
                correlation_id="cor-001",
            )
        self.assertFalse(result["discrepancy_raised"])
        mock_disc.assert_not_called()

    def test_silent_when_discrepancy_raise_fails(self):
        """Variance check must never break the SI creation flow."""
        with (
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=RuntimeError("substrate down"),
            ),
            patch("frappe.log_error"),
        ):
            result = _check_total_variance(
                si=self._si(grand_total=1180.00),
                marketplace_account=_account(),
                ee_invoice_id="EE-INV-100",
                ee_grand_total=1190.00,
                correlation_id="cor-001",
            )
        self.assertFalse(result["discrepancy_raised"])
        self.assertGreater(result["total_variance_paise"], 1)


# ============================================================
# Misc helpers
# ============================================================


class TestResolvePostingDate(unittest.TestCase):

    def test_uses_order_date(self):
        self.assertEqual(
            str(_resolve_posting_date({"order_date": "2026-06-15"})),
            "2026-06-15",
        )

    def test_accepts_camelcase(self):
        self.assertEqual(
            str(_resolve_posting_date({"orderDate": "2026-06-15"})),
            "2026-06-15",
        )

    def test_falls_back_to_today_when_no_date_field(self):
        self.assertIsNotNone(_resolve_posting_date({}))


class TestHashPayload(unittest.TestCase):

    def test_deterministic(self):
        self.assertEqual(
            _hash_payload({"x": 1, "y": [2, 3]}),
            _hash_payload({"y": [2, 3], "x": 1}),
        )

    def test_64_hex_chars(self):
        h = _hash_payload({"a": 1})
        self.assertEqual(len(h), 64)


if __name__ == "__main__":
    unittest.main()
