"""gh#184 + gh#187 regression — outbound B2B item payload must:
1. NOT double-count the discount (send Price = rate + discount, not
   Price = rate)
2. Send TAX-INCLUSIVE numbers (EE backs tax out at its own tax_rate)

Live symptoms this suite locks:
  SO-2610392 (rate=300, 50% discount, no tax) — pre-fix EE saw ₹0.
  SO-2610394 (rate=300, 50% discount, 5% GST) — pre-gh#187 EE saw ₹300
    when SO grand_total was ₹315.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _item(**kw):
    """Minimal Sales Order Item stand-in.

    gh#201: `item_tax_rate` is None by default so tests that don't
    care about per-line rates fall through to SO-level blend (preserves
    original test intent). Set explicitly per-test to exercise the
    per-line-rate primary path.
    """
    defaults = {
        "idx": 1,
        "item_code": "FG06476-CHOUHAN",
        "item_name": "Test Item",
        "qty": 1,
        "rate": 0,
        "discount_amount": 0,
        "item_tax_rate": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _so(name="SO-TEST", net_total=0, grand_total=0):
    return SimpleNamespace(
        name=name, net_total=net_total, grand_total=grand_total
    )


def _run(builder_name, so_item, so=None):
    from ecommerce_super.easyecom.flows.b2b_sales import payload_builder

    with patch(
        "ecommerce_super.easyecom.flows.b2b_sales.payload_builder."
        "resolve_ee_sku_or_throw",
        side_effect=lambda code: code,
    ):
        fn = getattr(payload_builder, builder_name)
        return fn(so or _so(), so_item)


class TestGh184Gh187OutboundDiscount(unittest.TestCase):
    def test_no_discount_no_tax_sends_rate_as_price(self):
        """Undiscounted item, zero-tax SO — Price=rate, itemDiscount=0."""
        item = _item(qty=1, rate=300, discount_amount=0)
        so = _so(net_total=300, grand_total=300)  # tax_multiplier = 1.0
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 300)
        self.assertEqual(p["itemDiscount"], 0)

    def test_no_discount_with_5pct_gst_grosses_up_price(self):
        """gh#187: Price grossed up to tax-inclusive; itemDiscount stays 0."""
        item = _item(qty=1, rate=300, discount_amount=0)
        so = _so(net_total=300, grand_total=315)  # tax_multiplier = 1.05
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 315)   # 300 * 1.05
        self.assertEqual(p["itemDiscount"], 0)

    def test_so_2610394_scenario_50pct_discount_5pct_gst(self):
        """SO-2610394 exact live case: Price=630, itemDiscount=315."""
        item = _item(qty=1, rate=300, discount_amount=300)
        so = _so(net_total=300, grand_total=315)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 630)
        self.assertEqual(p["itemDiscount"], 315)

    def test_100pct_discount_reaches_zero_with_tax_multiplier(self):
        """100% discount, zero-tax SO."""
        item = _item(qty=1, rate=0, discount_amount=500)
        so = _so(net_total=0, grand_total=0)  # fallback tax_multiplier = 1.0
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 500)
        self.assertEqual(p["itemDiscount"], 500)

    def test_old_b2b_variant_same_pricing_math(self):
        """Old B2B builder same math as New B2B — only Quantity type differs.
        gh#197 update: itemDiscount is now discount * qty * tax_multiplier
        (was: discount * tax_multiplier). See gh#197 test class below."""
        item = _item(qty=2, rate=100, discount_amount=50)
        so = _so(net_total=200, grand_total=210)  # tax_multiplier 1.05
        p = _run("build_old_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 157.5)     # (100+50)*1.05, per unit
        # gh#197: 50 * 2 (qty) * 1.05 = 105 — line total, not per unit
        self.assertEqual(p["itemDiscount"], 105)
        self.assertEqual(p["Quantity"], "2")    # Old B2B: string
        self.assertIn("productName", p)         # Old B2B: has productName


class TestGh197OutboundItemDiscountPerLine(unittest.TestCase):
    """gh#197 regression — EE applies `itemDiscount` PER-LINE (once per
    line, not multiplied by qty) while `Price` is applied PER-UNIT
    (multiplied by qty). Pre-fix, our helper emitted itemDiscount
    per-unit, so any qty>1 discounted line was under-discounted by
    (qty-1)*discount → EE invoice > SO grand_total.

    Live symptom: SO-2610401 line 2 (qty=5, rate=300, 50% off, 5% GST)
    returned EE taxable=2700 (should have been 1500); SI grand_total
    ₹5,984 vs SO ₹4,724. Isolated to discounted line — undiscounted
    lines in the same SO reconciled exactly.
    """

    def test_so_2610401_line2_scenario_qty5_discounted(self):
        """The exact live SO that surfaced the bug. Post-fix Price/qty
        arithmetic on EE side reproduces SO line total ₹1,575."""
        # rate 300 (post-50%-discount from list 600), discount 300,
        # qty 5, 5% IGST. SO line total = 5 * 300 * 1.05 = 1,575.
        item = _item(qty=5, rate=300, discount_amount=300)
        # SO-level: two other lines contribute; use ratio-preserving totals.
        # For this line alone: net = 5*300 = 1500, grand = 1575, mult = 1.05.
        so = _so(net_total=1500, grand_total=1575)
        p = _run("build_new_b2b_item", item, so=so)

        self.assertEqual(p["Price"], 630)         # per-unit, gh#187 gross-up
        self.assertEqual(p["itemDiscount"], 1575) # per-LINE: 300*5*1.05
        self.assertEqual(p["Quantity"], 5)        # New B2B: int

        # EE arithmetic: (Price * qty) - itemDiscount = gross line total
        gross = (p["Price"] * p["Quantity"]) - p["itemDiscount"]
        self.assertEqual(gross, 1575)  # matches SO line grand_total
        # Backs out at 5% tax: taxable = 1500, tax = 75 — matches SO.
        self.assertAlmostEqual(gross / 1.05, 1500.0, places=2)

    def test_qty1_discounted_line_unchanged_by_gh197(self):
        """Pre-existing qty=1 case (SO-2610394) unaffected — qty=1 is the
        degenerate case where per-unit and per-line are identical."""
        item = _item(qty=1, rate=300, discount_amount=300)
        so = _so(net_total=300, grand_total=315)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 630)
        # 300 * 1 * 1.05 = 315 — same as pre-gh#197
        self.assertEqual(p["itemDiscount"], 315)

    def test_qty0_discount_line_stays_zero(self):
        """qty>1 undiscounted line: itemDiscount stays 0 (0 * qty = 0)."""
        item = _item(qty=10, rate=100, discount_amount=0)
        so = _so(net_total=1000, grand_total=1050)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 105)      # 100 * 1.05, per unit
        self.assertEqual(p["itemDiscount"], 0) # no discount, no line total

    def test_qty3_discounted_zero_tax(self):
        """Middle-ground case: qty=3, discount, zero tax."""
        item = _item(qty=3, rate=200, discount_amount=50)
        # 3 * 200 = 600 net, no tax, mult = 1.0
        so = _so(net_total=600, grand_total=600)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 250)         # (200+50)*1.0 per unit
        self.assertEqual(p["itemDiscount"], 150)  # 50*3*1.0 line total
        # Reconstruct: EE math (250 * 3) - 150 = 600 — matches line net.
        self.assertEqual(p["Price"] * item.qty - p["itemDiscount"], 600)


class TestParametricSweepAllDimensions(unittest.TestCase):
    """Post-#197 discipline change: sweep every input dimension that
    influences the price/discount math. Each test locks EE's inversion
    contract: `(Price * qty) - itemDiscount` should equal the SO line's
    tax-inclusive grand total.

    Dimensions swept:
      1. All 5 real GST rates: 0%, 5%, 12%, 18%, 28%
      2. Fractional qty (MMPL sells kg/L B2B items)
      3. High qty (bulk B2B orders)
      4. Very small values (₹0.10 items — rounding edge)
      5. Very large values (₹1M+ single unit)
      6. Rate=0 (free samples / promos)
      7. 100% discount on qty>1
      8. Discount == rate boundary (line reaches zero)
    """

    def _assert_ee_inversion_holds(self, p, so_item, so):
        """The cornerstone invariant: whatever we send, EE's own
        arithmetic `(Price * qty) - itemDiscount` must equal the SO
        line's grand total. This is the contract check that would have
        caught #197 immediately with any qty>1."""
        ee_line_gross = p["Price"] * float(so_item.qty) - p["itemDiscount"]
        # SO line's tax-inclusive total: (rate * qty) grossed by SO's blended tax rate.
        # For single-tax-rate SOs this matches the actual line grand_total.
        so_line_net = float(so_item.rate) * float(so_item.qty)
        tax_multiplier = (
            so.grand_total / so.net_total if so.net_total else 1.0
        )
        so_line_gross = so_line_net * tax_multiplier
        self.assertAlmostEqual(ee_line_gross, so_line_gross, places=2,
            msg=f"EE inversion breaks: sent Price={p['Price']}, "
                f"itemDiscount={p['itemDiscount']}, qty={so_item.qty} → "
                f"EE would compute gross={ee_line_gross}, but SO line "
                f"gross is {so_line_gross}")

    # --- Sweep 1: all real GST rates -----------------------------------

    def test_gst_0pct_qty5_discounted(self):
        item = _item(qty=5, rate=100, discount_amount=20)
        so = _so(net_total=500, grand_total=500)  # 0% tax
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)
        self.assertEqual(p["itemDiscount"], 100)  # 20 * 5 * 1.0

    def test_gst_5pct_qty5_discounted(self):
        item = _item(qty=5, rate=100, discount_amount=20)
        so = _so(net_total=500, grand_total=525)  # 5%
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    def test_gst_12pct_qty5_discounted(self):
        item = _item(qty=5, rate=100, discount_amount=20)
        so = _so(net_total=500, grand_total=560)  # 12%
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    def test_gst_18pct_qty5_discounted(self):
        item = _item(qty=5, rate=100, discount_amount=20)
        so = _so(net_total=500, grand_total=590)  # 18%
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    def test_gst_28pct_qty5_discounted(self):
        item = _item(qty=5, rate=100, discount_amount=20)
        so = _so(net_total=500, grand_total=640)  # 28%
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    # --- Sweep 2: fractional qty (kg/L B2B items) ----------------------

    def test_fractional_qty_2point5_no_discount(self):
        """MMPL sells industrial goods in kg/L — qty=2.5 must not
        break the multiply-then-round. Old B2B preserves via str(qty);
        New B2B truncates to int (see KNOWN_LOSS test below)."""
        item = _item(qty=2.5, rate=100, discount_amount=0)
        so = _so(net_total=250, grand_total=262.5)  # 5% on 250
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    def test_fractional_qty_new_b2b_truncates_to_int_KNOWN_LOSS(self):
        """New B2B builder does `int(so_item.qty)` — 2.5 becomes 2.
        This test locks that CURRENT behavior; a fractional-qty SO will
        under-report Quantity to EE. Flagging as a data-loss risk for
        MMPL onboarding review. Not a bug in _item_price_and_discount
        itself, but a bug in build_new_b2b_item's qty coercion that
        arithmetic-based tests here would miss."""
        item = _item(qty=2.5, rate=100, discount_amount=0)
        so = _so(net_total=250, grand_total=262.5)
        p = _run("build_new_b2b_item", item, so=so)
        # This is the DEFECT: EE receives qty=2, not 2.5.
        self.assertEqual(p["Quantity"], 2)
        # Old B2B uses str(so_item.qty) which preserves "2.5" — verify.
        p_old = _run("build_old_b2b_item", item, so=so)
        self.assertEqual(p_old["Quantity"], "2.5")

    # --- Sweep 3: high qty (bulk B2B) ----------------------------------

    def test_high_qty_100_discounted(self):
        item = _item(qty=100, rate=50, discount_amount=10)
        so = _so(net_total=5000, grand_total=5900)  # 18%
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)
        # itemDiscount is the line total: 10 * 100 * 1.18 = 1180
        self.assertEqual(p["itemDiscount"], 1180)

    # --- Sweep 4: rounding edges ---------------------------------------

    def test_very_small_value_paise_precision(self):
        """₹0.10 unit price × 3 qty × 5% GST — verify no paise loss
        beyond documented Price-level rounding noise."""
        item = _item(qty=3, rate=0.10, discount_amount=0)
        so = _so(net_total=0.30, grand_total=0.315)
        p = _run("build_new_b2b_item", item, so=so)
        # 0.10 * 1.05 = 0.105 → rounds to 0.11 (banker's would round to 0.10)
        self.assertIn(p["Price"], (0.10, 0.11))

    def test_large_value_million_no_overflow(self):
        """₹1,000,000 unit price × 2 qty — verify no overflow / precision loss."""
        item = _item(qty=2, rate=1_000_000, discount_amount=50_000)
        so = _so(net_total=2_000_000, grand_total=2_360_000)  # 18%
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    # --- Sweep 5: rate/discount boundaries -----------------------------

    def test_rate_zero_free_sample(self):
        """Free sample line: rate=0, discount=0, qty>1 → Price=0, discount=0."""
        item = _item(qty=5, rate=0, discount_amount=0)
        so = _so(net_total=0, grand_total=0)  # zero-value SO falls back to mult=1
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 0)
        self.assertEqual(p["itemDiscount"], 0)

    def test_discount_equals_rate_line_reaches_zero(self):
        """100%-off line: rate=100, discount=100 (from list=200), qty=3.
        Customer still pays rate=100/unit (rate is POST-discount)."""
        item = _item(qty=3, rate=100, discount_amount=100)
        so = _so(net_total=300, grand_total=315)  # 5%
        p = _run("build_new_b2b_item", item, so=so)
        # Price = (100+100) * 1.05 = 210 per-unit
        # itemDiscount = 100 * 3 * 1.05 = 315 per-line
        # EE gross = 210 * 3 - 315 = 315 — customer pays 100/unit × 3 + 5% tax
        self.assertEqual(p["Price"], 210)
        self.assertEqual(p["itemDiscount"], 315)
        self._assert_ee_inversion_holds(p, item, so)


class TestGh201MixedTaxRatePerLineFix(unittest.TestCase):
    """gh#201 regression — `_item_price_and_discount` now reads the
    LINE's own `item_tax_rate` (ERPNext-populated from Item Tax
    Template + Sales Taxes and Charges) instead of the SO's blended
    `grand_total / net_total`. This lets mixed-rate SOs (5% + 18%
    lines in one order) round-trip correctly through EE, which backs
    tax out at each item's own ProductTaxCode.

    Pre-#201 behavior (now inverted): both lines were grossed at the
    blended 1.115 multiplier — low-rate over-grossed by ~6%, high-rate
    under-grossed by ~5%. Post-#201: each line grosses at its own rate.
    """

    def _mixed_rate_so(self):
        """SO with 2 lines: item A at 5% GST, item B at 18% GST.
        Line totals: A=105, B=118. SO net=200, grand=223. Blended
        mult would be 1.115 — but with gh#201 each line uses its own."""
        return _so(net_total=200, grand_total=223)

    def test_low_rate_line_grossed_at_own_5pct_not_blended(self):
        """Line A (5% GST) now uses 1.05, not blended 1.115. EE backs
        out cleanly to taxable=100."""
        item_a = _item(
            qty=1, rate=100, discount_amount=0,
            item_tax_rate='{"Output Tax IGST - MMPL": 5.0}',
        )
        so = self._mixed_rate_so()
        p = _run("build_new_b2b_item", item_a, so=so)
        self.assertEqual(p["Price"], 105.0)  # 100 * 1.05, per-line
        # EE inversion at 5%: 105.0 / 1.05 = 100.00 exactly.
        self.assertAlmostEqual(p["Price"] / 1.05, 100.0, places=2)

    def test_high_rate_line_grossed_at_own_18pct_not_blended(self):
        """Line B (18% GST) now uses 1.18, not blended 1.115. EE backs
        out cleanly to taxable=100."""
        item_b = _item(
            qty=1, rate=100, discount_amount=0,
            item_tax_rate='{"Output Tax IGST - MMPL": 18.0}',
        )
        so = self._mixed_rate_so()
        p = _run("build_new_b2b_item", item_b, so=so)
        self.assertEqual(p["Price"], 118.0)  # 100 * 1.18, per-line
        # EE inversion at 18%: 118.0 / 1.18 = 100.00 exactly.
        self.assertAlmostEqual(p["Price"] / 1.18, 100.0, places=2)

    def test_cgst_sgst_split_sums_to_same_as_single_igst(self):
        """CGST 9% + SGST 9% is arithmetically the same as IGST 18% —
        the helper sums the rate dict, so both shapes yield identical
        Price/discount output. This locks the sum-based approach."""
        item_igst = _item(
            qty=2, rate=100, discount_amount=20,
            item_tax_rate='{"Output Tax IGST - MMPL": 18.0}',
        )
        item_split = _item(
            qty=2, rate=100, discount_amount=20,
            item_tax_rate=(
                '{"Output Tax CGST - MMPL": 9.0, '
                '"Output Tax SGST - MMPL": 9.0}'
            ),
        )
        so = _so(net_total=200, grand_total=236)
        p_igst = _run("build_new_b2b_item", item_igst, so=so)
        p_split = _run("build_new_b2b_item", item_split, so=so)
        self.assertEqual(p_igst["Price"], p_split["Price"])
        self.assertEqual(p_igst["itemDiscount"], p_split["itemDiscount"])

    def test_falls_back_to_so_blend_when_item_tax_rate_empty(self):
        """Backward-compat: items without `item_tax_rate` (older data,
        stub items in tests) still hit the SO-level blended fallback.
        Every pre-#201 test in this module relies on this path."""
        item = _item(qty=1, rate=100, discount_amount=0, item_tax_rate=None)
        so = _so(net_total=100, grand_total=118)  # 18% blended = 1.18
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)  # matches SO-blend fallback

    def test_falls_back_when_item_tax_rate_is_empty_string(self):
        """ERPNext may serialize an empty dict as '{}' or ''. Both
        should fall through to SO-level blend, not crash or return
        multiplier=1.0."""
        item = _item(qty=1, rate=100, discount_amount=0, item_tax_rate="")
        so = _so(net_total=100, grand_total=118)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)

    def test_falls_back_when_item_tax_rate_is_empty_dict_string(self):
        item = _item(qty=1, rate=100, discount_amount=0, item_tax_rate="{}")
        so = _so(net_total=100, grand_total=118)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)

    def test_falls_back_when_item_tax_rate_is_malformed(self):
        """Malformed JSON in item_tax_rate must not crash the push;
        fall through to SO-level blend."""
        item = _item(
            qty=1, rate=100, discount_amount=0,
            item_tax_rate="{not valid json",
        )
        so = _so(net_total=100, grand_total=118)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)

    def test_dict_type_item_tax_rate_supported_not_only_json_string(self):
        """ERPNext sometimes passes the field as a dict directly (in
        memory, not just serialized). The helper must handle both."""
        item = _item(
            qty=1, rate=100, discount_amount=0,
            item_tax_rate={"Output Tax IGST - MMPL": 12.0},
        )
        # SO blend would be 1.0 (uniform 0-tax) — proves per-line took precedence.
        so = _so(net_total=100, grand_total=100)
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 112.0)  # 100 * 1.12, from dict

    def test_mixed_so_discount_grossed_at_own_line_rate(self):
        """gh#197 + gh#201 interaction: discounted line on mixed-rate
        SO should apply BOTH the per-line multiplier AND the qty
        multiplier to the discount."""
        item = _item(
            qty=4, rate=200, discount_amount=50,
            item_tax_rate='{"Output Tax IGST - MMPL": 18.0}',
        )
        # SO is mixed-rate (won't match line B's 18% blended) —
        # exact SO totals irrelevant since per-line kicks in.
        so = _so(net_total=1000, grand_total=1200)
        p = _run("build_new_b2b_item", item, so=so)
        # Price = (200 + 50) * 1.18 = 295 per-unit (line rate, NOT blend 1.2)
        self.assertEqual(p["Price"], 295.0)
        # itemDiscount = 50 * 4 * 1.18 = 236 per-line (also at line rate)
        self.assertEqual(p["itemDiscount"], 236.0)
        # EE gross: (295 * 4) - 236 = 944; backs out at 18% → taxable 800
        # (which equals 4 * 200 = 800, the SO line's taxable). Correct.
        self.assertAlmostEqual(
            (p["Price"] * 4 - p["itemDiscount"]) / 1.18, 800.0, places=2,
        )
