"""gh#184 regression — outbound B2B item payload must NOT double-count
the discount. EE expects `Price` = list (pre-discount) per-unit price
and `itemDiscount` = discount to subtract. ERPNext's `so_item.rate` is
already post-discount, so we must reconstruct list price as
`rate + discount_amount`.

Pre-fix: SO-2610392 (₹300 item, 50% Pricing Rule) landed in EE as a
zero-total invoice because we sent `Price=300, itemDiscount=300`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _item(**kw):
    """Build a minimal Sales Order Item stand-in."""
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


def _so(name="SO-TEST"):
    return SimpleNamespace(name=name)


def _run(builder_name: str, so_item):
    from ecommerce_super.easyecom.flows.b2b_sales import payload_builder

    # Bypass Item Map lookup — return the SKU as-is.
    with patch(
        "ecommerce_super.easyecom.flows.b2b_sales.payload_builder."
        "resolve_ee_sku_or_throw",
        side_effect=lambda code: code,
    ):
        fn = getattr(payload_builder, builder_name)
        return fn(_so(), so_item)


def test_no_discount_sends_rate_as_price() -> None:
    """Undiscounted item — Price=rate, itemDiscount=0. Same behavior
    as before the fix, no regression."""
    item = _item(qty=1, rate=300, discount_amount=0)
    p = _run("build_new_b2b_item", item)
    assert p["Price"] == 300, p
    assert p["itemDiscount"] == 0, p


def test_50pct_discount_reconstructs_list_price() -> None:
    """SO-2610392 exact scenario: rate=300 (post 50% discount),
    discount_amount=300. Price should be 600 (list), itemDiscount=300.
    EE math: 600 - 300 = 300 net. Matches our SO's pre-tax amount."""
    item = _item(qty=1, rate=300, discount_amount=300)
    p = _run("build_new_b2b_item", item)
    assert p["Price"] == 600, p
    assert p["itemDiscount"] == 300, p


def test_100pct_discount_still_works() -> None:
    """Edge case: 100% discount. Pre-fix bug would already have hit here.
    Post-fix: Price=list, itemDiscount=list. EE math: list - list = 0."""
    item = _item(qty=1, rate=0, discount_amount=500)
    p = _run("build_new_b2b_item", item)
    assert p["Price"] == 500, p
    assert p["itemDiscount"] == 500, p


def test_old_b2b_variant_same_pricing_logic() -> None:
    """The Old B2B builder must have the SAME price/discount math as
    New B2B — only the Quantity type + productName field differ."""
    item = _item(qty=2, rate=100, discount_amount=50)
    p = _run("build_old_b2b_item", item)
    assert p["Price"] == 150, p          # 100 + 50 = list
    assert p["itemDiscount"] == 50, p
    assert p["Quantity"] == "2", p       # Old B2B quirk: string
    assert "productName" in p            # Old B2B ships product name
