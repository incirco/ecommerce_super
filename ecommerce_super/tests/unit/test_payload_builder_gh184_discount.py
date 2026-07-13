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


def _run(builder_name: str, so_item, so=None):
    from ecommerce_super.easyecom.flows.b2b_sales import payload_builder

    with patch(
        "ecommerce_super.easyecom.flows.b2b_sales.payload_builder."
        "resolve_ee_sku_or_throw",
        side_effect=lambda code: code,
    ):
        fn = getattr(payload_builder, builder_name)
        return fn(so or _so(), so_item)


def test_no_discount_no_tax_sends_rate_as_price() -> None:
    """Undiscounted item, zero-tax SO — Price=rate, itemDiscount=0."""
    item = _item(qty=1, rate=300, discount_amount=0)
    so = _so(net_total=300, grand_total=300)  # tax_multiplier = 1.0
    p = _run("build_new_b2b_item", item, so=so)
    assert p["Price"] == 300, p
    assert p["itemDiscount"] == 0, p


def test_no_discount_with_5pct_gst_grosses_up_price() -> None:
    """gh#187: undiscounted item, 5% GST — Price grossed up to
    tax-inclusive so EE's back-out gives the correct net."""
    item = _item(qty=1, rate=300, discount_amount=0)
    so = _so(net_total=300, grand_total=315)  # tax_multiplier = 1.05
    p = _run("build_new_b2b_item", item, so=so)
    assert p["Price"] == 315, p       # 300 * 1.05
    assert p["itemDiscount"] == 0, p


def test_so_2610394_scenario_50pct_discount_5pct_gst() -> None:
    """SO-2610394 exact live case:
      rate=300 (post 50% discount), discount_amount=300, GST 5%.
      Price=630, itemDiscount=315.
      EE math: 630 - 315 = 315 gross = SO grand_total.
      Backs out to taxable=300, tax=15.
    """
    item = _item(qty=1, rate=300, discount_amount=300)
    so = _so(net_total=300, grand_total=315)  # tax_multiplier = 1.05
    p = _run("build_new_b2b_item", item, so=so)
    assert p["Price"] == 630, p
    assert p["itemDiscount"] == 315, p


def test_100pct_discount_reaches_zero_with_tax_multiplier() -> None:
    """Edge case: 100% discount, zero-tax SO."""
    item = _item(qty=1, rate=0, discount_amount=500)
    so = _so(net_total=0, grand_total=0)  # tax_multiplier fallback = 1.0
    p = _run("build_new_b2b_item", item, so=so)
    assert p["Price"] == 500, p
    assert p["itemDiscount"] == 500, p


def test_old_b2b_variant_same_pricing_math() -> None:
    """Old B2B builder must apply the SAME price/discount + tax math
    as New B2B — only Quantity type + productName field differ."""
    item = _item(qty=2, rate=100, discount_amount=50)
    so = _so(net_total=200, grand_total=210)  # tax_multiplier 1.05
    p = _run("build_old_b2b_item", item, so=so)
    assert p["Price"] == 157.5, p        # (100 + 50) * 1.05
    assert p["itemDiscount"] == 52.5, p  # 50 * 1.05
    assert p["Quantity"] == "2", p       # Old B2B quirk: string
    assert "productName" in p            # Old B2B ships productName
