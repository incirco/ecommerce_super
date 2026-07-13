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
        """Old B2B builder same math as New B2B — only Quantity type differs."""
        item = _item(qty=2, rate=100, discount_amount=50)
        so = _so(net_total=200, grand_total=210)  # tax_multiplier 1.05
        p = _run("build_old_b2b_item", item, so=so)
        self.assertEqual(p["Price"], 157.5)     # (100+50)*1.05
        self.assertEqual(p["itemDiscount"], 52.5)  # 50*1.05
        self.assertEqual(p["Quantity"], "2")    # Old B2B: string
        self.assertIn("productName", p)         # Old B2B: has productName
