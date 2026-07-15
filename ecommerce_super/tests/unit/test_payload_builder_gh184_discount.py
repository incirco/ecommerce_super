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
    """Minimal Sales Order Item stand-in."""
    defaults = {
        "idx": 1,
        "item_code": "FG06476-CHOUHAN",
        "item_name": "Test Item",
        "qty": 1,
        "rate": 0,
        "discount_amount": 0,
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
