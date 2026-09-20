"""gh#269 + gh#271 — invoice-mirror hardening.

gh#269: variance guard had no absolute-rupee floor. On a 64-line
discounted GST invoice ~₹22k, the ~₹2 paise-rounding drift between
ERPNext and EE exceeded the 0.01% (₹2.27) threshold and the mirror
refused correctly-billed split-shipment invoices. Fix adds a ₹10
absolute floor; the tighter of (floor, pct) applies via max().

gh#271: mirror idempotency keyed only on `ecs_easyecom_invoice_id`.
When EE regenerates an invoice for the same order it reuses the human
`invoice_number` but mints a new `invoice_id`. With the first mirror
SI still in Draft (billed_qty not yet updated by ERPNext), the lookup
missed and a duplicate SI was created. Fix: fall back to
`ecs_easyecom_invoice_number` lookup and restamp the new invoice_id
onto the existing SI.

Both bugs surfaced on Modern Marwar (MMPL) live in August 2026.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror as mod


class TestVarianceAbsoluteFloor(unittest.TestCase):
    """gh#269 — the variance check should tolerate paise-rounding drift
    on B2B invoice sizes, and still catch genuine rupee-level divergence.
    """

    def test_live_case_si_2603744_now_passes(self):
        # SO-2610694-1 → SI-2603744 (MMPL, 2026-08). Before fix: refused
        # because ₹2.36 diff on ₹22687 EE total = 0.0104% > 0.01% threshold.
        exceeds, abs_diff, threshold = mod._variance_exceeds_tolerance(
            si_total=22684.80, ee_total=22687.16
        )
        self.assertFalse(exceeds, msg=f"₹{abs_diff:.2f} diff vs ₹{threshold:.2f} threshold")
        self.assertAlmostEqual(abs_diff, 2.36, places=2)
        self.assertEqual(threshold, mod.VARIANCE_ABSOLUTE_FLOOR_RUPEES)  # ₹10 floor wins

    def test_live_case_si_2603746_now_passes(self):
        # SO-2610694-1 → SI-2603746 (companion split-shipment)
        exceeds, abs_diff, _ = mod._variance_exceeds_tolerance(
            si_total=13870.50, ee_total=13868.43
        )
        self.assertFalse(exceeds)
        self.assertAlmostEqual(abs_diff, 2.07, places=2)

    def test_real_divergence_still_fails(self):
        # ₹87 diff on ₹22687 — a real billing gap, must still trigger.
        exceeds, abs_diff, threshold = mod._variance_exceeds_tolerance(
            si_total=22600.00, ee_total=22687.00
        )
        self.assertTrue(exceeds)
        self.assertAlmostEqual(abs_diff, 87.00, places=2)
        self.assertEqual(threshold, mod.VARIANCE_ABSOLUTE_FLOOR_RUPEES)

    def test_pct_dominates_at_large_invoice_scale(self):
        # ₹1M EE total → pct threshold ₹100 > ₹10 floor. A ₹150 diff
        # fails (real money at scale); a ₹50 diff passes.
        exceeds_150, _, threshold = mod._variance_exceeds_tolerance(
            si_total=999_850.00, ee_total=1_000_000.00
        )
        self.assertTrue(exceeds_150)
        self.assertEqual(threshold, 100.0)  # pct wins

        exceeds_50, _, _ = mod._variance_exceeds_tolerance(
            si_total=999_950.00, ee_total=1_000_000.00
        )
        self.assertFalse(exceeds_50)

    def test_exact_match_passes(self):
        exceeds, abs_diff, _ = mod._variance_exceeds_tolerance(
            si_total=1000.0, ee_total=1000.0
        )
        self.assertFalse(exceeds)
        self.assertEqual(abs_diff, 0.0)


class TestMirrorDedupByInvoiceNumber(unittest.TestCase):
    """gh#271 — mirror must dedup on `ecs_easyecom_invoice_number` when
    EE regenerates an invoice with a fresh invoice_id but reused
    invoice_number.
    """

    def _map_doc(self):
        m = MagicMock()
        m.name = "ECS-B2B-SO-2610697"
        m.sales_order = "SO-2610697"
        m.company = "MMPL"
        m.sales_invoice = None
        m.get = lambda k, default=None: {"sales_invoice": None}.get(k, default)
        return m

    def _ee_row(self, invoice_id, invoice_number):
        return {
            "invoice_id": invoice_id,
            "invoice_number": invoice_number,
            "invoice_date": "2026-08-13",
            "total_amount": 26281.5,
            "order_items": [{"sku": "SKU-A", "item_quantity": 1}],
        }

    @patch.object(mod, "frappe")
    def test_regenerated_invoice_reuses_existing_si(self, mock_frappe):
        # First `get_value` call (invoice_id lookup) misses.
        # Second `get_value` call (invoice_number lookup) hits.
        # Third `get_value` call reads the existing SI's grand_total.
        mock_frappe.db.get_value.side_effect = [
            None,             # invoice_id lookup — miss (new EE invoice_id)
            "SI-2603888",     # invoice_number lookup — hit (existing Draft SI)
            26281.5,          # existing SI's grand_total
        ]
        mock_frappe.db.set_value = MagicMock()
        mock_frappe.db.commit = MagicMock()

        result = mod.mirror_si_from_ee_response(
            ee_row=self._ee_row(invoice_id="70499999095", invoice_number="BRJ-62"),
            map_doc=self._map_doc(),
        )

        self.assertEqual(result["sales_invoice"], "SI-2603888")
        self.assertEqual(result["operation"], "already_exists")
        # The new invoice_id must be restamped onto the existing SI so
        # the fast-path lookup hits next time.
        mock_frappe.db.set_value.assert_called_once_with(
            "Sales Invoice", "SI-2603888",
            "ecs_easyecom_invoice_id", "70499999095",
            update_modified=False,
        )
        mock_frappe.db.commit.assert_called_once()

    @patch.object(mod, "frappe")
    def test_blank_invoice_number_skips_fallback(self, mock_frappe):
        # invoice_id lookup misses; invoice_number is blank → the
        # invoice_number lookup MUST NOT be attempted (only 1 get_value
        # call before falling through to legacy adoption).
        mock_frappe.db.get_value.side_effect = [
            None,   # invoice_id lookup — miss
            None,   # legacy adoption check (if reached)
        ]
        mock_frappe.db.exists.return_value = False

        try:
            mod.mirror_si_from_ee_response(
                ee_row=self._ee_row(invoice_id="NEW-ID", invoice_number=""),
                map_doc=self._map_doc(),
            )
        except Exception:
            # We expect the function to proceed past both guards and
            # eventually hit make_sales_invoice (which we haven't mocked
            # — that's fine, we only care that the invoice_number
            # fallback was skipped).
            pass

        # Count how many "Sales Invoice" get_value calls were made
        # with ecs_easyecom_invoice_number in the filter — should be 0.
        invoice_number_calls = [
            c for c in mock_frappe.db.get_value.call_args_list
            if len(c.args) >= 2
            and isinstance(c.args[1], dict)
            and "ecs_easyecom_invoice_number" in c.args[1]
        ]
        self.assertEqual(len(invoice_number_calls), 0)


if __name__ == "__main__":
    unittest.main()
