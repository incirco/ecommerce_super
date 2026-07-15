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

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _item(**kw):
    """Minimal Sales Order Item stand-in.

    gh#201 (v2): the per-line tax rate now comes from the SO's applied
    `item_wise_tax_detail` (see `_tax` / `_so`), NOT from the item. Keep
    `item_tax_rate` (default None) only so a test can prove it is
    ignored; it no longer influences the multiplier.
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


def _so(name="SO-TEST", net_total=0, grand_total=0, taxes=None):
    """Minimal Sales Order stand-in.

    gh#201: `taxes` holds Sales Taxes and Charges rows carrying
    `item_wise_tax_detail` (see `_tax`). Empty by default so tests that
    don't care about per-line rates fall through to the SO-level blend.
    """
    return SimpleNamespace(
        name=name, net_total=net_total, grand_total=grand_total,
        taxes=taxes or [],
    )


def _tax(item_wise):
    """Sales Taxes and Charges row stand-in.

    `item_wise` is a dict keyed by item_code with value [rate, amount],
    exactly as ERPNext serializes `item_wise_tax_detail` (a JSON string).
    Split GST is modelled as two rows (one CGST, one SGST).
    """
    return SimpleNamespace(item_wise_tax_detail=json.dumps(item_wise))


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

    def test_fractional_qty_price_math_still_works_at_float_precision(self):
        """qty=2.5 in the arithmetic (rate * qty * mult) must not
        introduce paise noise even though EE won't accept the
        fractional Quantity itself (see qty-coercion test below).
        Locks the math side; qty coercion is a separate contract."""
        item = _item(qty=2.5, rate=100, discount_amount=0)
        so = _so(net_total=250, grand_total=262.5)  # 5% on 250
        p = _run("build_new_b2b_item", item, so=so)
        self._assert_ee_inversion_holds(p, item, so)

    def test_ee_contract_both_builders_coerce_fractional_qty_to_int(self):
        """EE contract: fractional quantities are NOT supported on
        either B2B module. Both builders must coerce to int, else
        Old B2B would send "2.5" and be rejected by EE.

        Pre-#197 followup: Old B2B used `str(so_item.qty)` which sent
        "2.5" literally — a real bug. Now uses `str(int(qty))` for
        parity with New B2B's `int(qty)`.
        """
        item = _item(qty=2.5, rate=100, discount_amount=0)
        so = _so(net_total=250, grand_total=262.5)

        p_new = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p_new["Quantity"], 2)  # int, matches EE contract

        p_old = _run("build_old_b2b_item", item, so=so)
        self.assertEqual(p_old["Quantity"], "2")  # str-of-int, NOT "2.5"

    def test_ee_contract_whole_number_qty_unaffected_by_coercion(self):
        """Regression guard: whole-number qty must survive the int()
        coercion without any change on either builder."""
        item = _item(qty=5, rate=100, discount_amount=0)
        so = _so(net_total=500, grand_total=525)
        p_new = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p_new["Quantity"], 5)
        p_old = _run("build_old_b2b_item", item, so=so)
        self.assertEqual(p_old["Quantity"], "5")

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


class TestGh201PerLineTaxFromAppliedDetail(unittest.TestCase):
    """gh#201 (v2) — `_line_tax_multiplier` reads the rate ACTUALLY
    APPLIED to the line from the SO's `item_wise_tax_detail` (keyed by
    item_code, value [rate, amount]), instead of summing the item's full
    Item Tax Template (`item_tax_rate`).

    The first gh#201 fix summed `item_tax_rate`, which under India
    Compliance lists every account head (output + input + RCM, intra +
    inter) — e.g. 30% for a 5% item. That over-grossed every B2B line by
    30/5. Live symptom SO-2610402: SI ₹2,080 vs SO ₹1,680, every line
    inflated ×1.2381 (= 1.30 / 1.05). This suite locks the applied-rate
    source, the CGST+SGST row summation, and the fallback.
    """

    def test_low_rate_line_grossed_at_own_5pct(self):
        """Line A (5% GST) grosses at its own 1.05, not the SO blend."""
        item = _item(item_code="ITEM-A", qty=1, rate=100, discount_amount=0)
        so = _so(net_total=200, grand_total=223, taxes=[
            _tax({"ITEM-A": [5.0, 5.0], "ITEM-B": [18.0, 18.0]})])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 105.0)  # 100 * 1.05
        self.assertAlmostEqual(p["Price"] / 1.05, 100.0, places=2)

    def test_high_rate_line_grossed_at_own_18pct(self):
        """Line B (18% GST) grosses at its own 1.18 from the same SO."""
        item = _item(item_code="ITEM-B", qty=1, rate=100, discount_amount=0)
        so = _so(net_total=200, grand_total=223, taxes=[
            _tax({"ITEM-A": [5.0, 5.0], "ITEM-B": [18.0, 18.0]})])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)  # 100 * 1.18
        self.assertAlmostEqual(p["Price"] / 1.18, 100.0, places=2)

    def test_cgst_sgst_split_rows_sum_like_single_igst(self):
        """Intra-state: a CGST 9% row + SGST 9% row sum to the same 18%
        as a single IGST row. Rates sum ACROSS the applied tax rows for
        the item."""
        item_igst = _item(item_code="IG", qty=2, rate=100, discount_amount=20)
        so_igst = _so(net_total=200, grand_total=236,
                      taxes=[_tax({"IG": [18.0, 36.0]})])
        item_split = _item(item_code="SP", qty=2, rate=100, discount_amount=20)
        so_split = _so(net_total=200, grand_total=236, taxes=[
            _tax({"SP": [9.0, 18.0]}), _tax({"SP": [9.0, 18.0]})])
        p_igst = _run("build_new_b2b_item", item_igst, so=so_igst)
        p_split = _run("build_new_b2b_item", item_split, so=so_split)
        self.assertEqual(p_igst["Price"], p_split["Price"])
        self.assertEqual(p_igst["itemDiscount"], p_split["itemDiscount"])

    def test_ignores_item_tax_rate_template_sum_uses_applied_rate(self):
        """gh#201 live regression (SO-2610402 line 2, 01Test). The item's
        Item Tax Template sums to 30% across account heads, but the tax
        ACTUALLY applied is 5%. Must gross at 5% (Price 1050), NOT 30%
        (Price 1300, the pre-fix bug)."""
        item = _item(
            item_code="FG20295", qty=1, rate=1000, discount_amount=0,
            item_tax_rate=json.dumps({
                "Output Tax CGST - MMPL": 2.5, "Output Tax SGST - MMPL": 2.5,
                "Output Tax IGST - MMPL": 5.0, "Input Tax CGST - MMPL": 2.5,
                "Input Tax SGST - MMPL": 2.5, "Input Tax IGST - MMPL": 5.0,
                "Output Tax IGST RCM - MMPL": 5.0,
                "Output Tax CGST RCM - MMPL": 2.5,
                "Output Tax SGST RCM - MMPL": 2.5,
            }),  # sums to 30
        )
        so = _so(net_total=1000, grand_total=1050,
                 taxes=[_tax({"FG20295": [5.0, 50.0]})])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 1050.0)  # 1000 * 1.05, NOT 1300
        self.assertEqual(p["itemDiscount"], 0)

    def test_so2610402_button_line_discounted(self):
        """SO-2610402 line 1: BUTTON qty 2, rate 300, 50% disc, applied
        5%. Pre-fix over-grossed at 30% → Price/itemDiscount 780; post-fix
        grosses at the real 5%."""
        item = _item(item_code="FG06476-CHOUHAN", qty=2, rate=300,
                     discount_amount=300)
        so = _so(net_total=600, grand_total=630,
                 taxes=[_tax({"FG06476-CHOUHAN": [5.0, 30.0]})])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 630.0)         # (300+300) * 1.05
        self.assertEqual(p["itemDiscount"], 630.0)  # 300 * 2 * 1.05
        # EE gross (630*2)-630 = 630 → taxable 600 at 5%. Matches SO.
        self.assertAlmostEqual(
            (p["Price"] * 2 - p["itemDiscount"]) / 1.05, 600.0, places=2)

    def test_falls_back_to_so_blend_when_no_tax_rows(self):
        """No Sales Taxes and Charges rows → SO-level blended fallback.
        Every pre-#201 test in this module relies on this path."""
        item = _item(item_code="X", qty=1, rate=100, discount_amount=0)
        so = _so(net_total=100, grand_total=118)  # blend = 1.18
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)

    def test_falls_back_when_item_not_in_tax_detail(self):
        """Tax rows exist but none reference this item_code → fallback,
        not a silent 0% (which would send Price=rate ungrossed)."""
        item = _item(item_code="X", qty=1, rate=100, discount_amount=0)
        so = _so(net_total=100, grand_total=118,
                 taxes=[_tax({"OTHER-ITEM": [5.0, 5.0]})])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)  # blend, not 1.05

    def test_falls_back_when_tax_detail_malformed(self):
        """Malformed JSON in a tax row must not crash the push; skip the
        row and fall through to the SO-level blend."""
        item = _item(item_code="X", qty=1, rate=100, discount_amount=0)
        bad = SimpleNamespace(item_wise_tax_detail="{not valid json")
        so = _so(net_total=100, grand_total=118, taxes=[bad])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 118.0)

    def test_mixed_so_discount_grossed_at_own_line_rate(self):
        """gh#197 + gh#201 interaction: a discounted line on a mixed-rate
        SO applies BOTH the per-line multiplier AND the qty multiplier."""
        item = _item(item_code="B18", qty=4, rate=200, discount_amount=50)
        # SO blend (1200/1000 = 1.20) deliberately != the line's own 18%.
        so = _so(net_total=1000, grand_total=1200,
                 taxes=[_tax({"B18": [18.0, 144.0]})])
        p = _run("build_new_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 295.0)         # (200+50) * 1.18
        self.assertEqual(p["itemDiscount"], 236.0)  # 50 * 4 * 1.18
        # EE gross (295*4)-236 = 944; backs out at 18% → taxable 800.
        self.assertAlmostEqual(
            (p["Price"] * 4 - p["itemDiscount"]) / 1.18, 800.0, places=2)
