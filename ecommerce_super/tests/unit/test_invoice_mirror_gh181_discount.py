"""gh#181 regression — mirror line-item rate must respect EE's
post-promotion `taxable_value` (or breakup_types with promo). The
first shipped version read only `Item Amount Excluding Tax` from
breakup_types and ignored the sibling `Promotion Discount Excluding
Tax`, so SI totals didn't match EE totals on promo orders.

Live symptom: SO-2610392 on mmpl16 (2026-07-13) — EE grand_total=₹0,
SI grand_total=₹285.71.
"""
from __future__ import annotations

from unittest.mock import patch


def _run_resolver(order_items: list, *, item_map_hit: str = "FG06476-CHOUHAN") -> list:
    """Call _resolve_line_items with mocked EE Item Map + Item HSN reads."""
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        _resolve_line_items,
    )

    def _fake_get_value(*args, **kwargs):
        # First arg is the doctype
        doctype = args[0]
        if doctype == "EasyEcom Item Map":
            return item_map_hit
        if doctype == "Item":
            return "12345678"  # HSN
        return None

    with patch(
        "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.db.get_value",
        side_effect=_fake_get_value,
    ):
        return _resolve_line_items({"order_items": order_items})


def test_uses_taxable_value_directly_when_present() -> None:
    """SO-2610392 scenario: 100% promotion → taxable_value=0. Rate
    should be 0, not the pre-discount 285.71."""
    lines = _run_resolver([{
        "sku": "FG06476-CHOUHAN",
        "item_quantity": 1,
        "taxable_value": 0,
        "tax_rate": 5,
        "breakup_types": {
            # These SHOULD be ignored when taxable_value is present.
            "Item Amount Excluding Tax": 285.7143,
            "Promotion Discount Excluding Tax": -285.7143,
            "Item Amount IGST": 14.2857,
            "Promotion Discount IGST": -14.2857,
        },
    }])
    assert len(lines) == 1
    assert lines[0]["rate"] == 0.0, lines
    assert lines[0]["qty"] == 1


def test_uses_taxable_value_for_normal_priced_line() -> None:
    """Baseline: no promo, taxable_value = 952.38, qty=1. Rate=952.38."""
    lines = _run_resolver([{
        "sku": "FG06476-CHOUHAN",
        "item_quantity": 1,
        "taxable_value": 952.38,
        "tax_rate": 5,
    }])
    assert lines[0]["rate"] == 952.38, lines


def test_falls_back_to_breakup_sum_when_taxable_value_absent() -> None:
    """Legacy payload without taxable_value — sum the *Excluding Tax
    entries in breakup_types (nets out the promo)."""
    lines = _run_resolver([{
        "sku": "FG06476-CHOUHAN",
        "item_quantity": 2,
        # taxable_value NOT present in this payload
        "breakup_types": {
            "Item Amount Excluding Tax": 200.0,
            "Promotion Discount Excluding Tax": -50.0,
            "Item Amount IGST": 10.0,
        },
    }])
    # Net = 200 - 50 = 150; per-unit = 75.
    assert lines[0]["rate"] == 75.0, lines


def test_final_fallback_selling_price_when_both_missing() -> None:
    """Neither taxable_value nor breakup_types → derive from
    selling_price and tax_rate."""
    lines = _run_resolver([{
        "sku": "FG06476-CHOUHAN",
        "item_quantity": 1,
        "selling_price": 105.0,  # gross for qty=1
        "tax_rate": 5,
    }])
    # Net = 105 / 1.05 = 100.
    assert lines[0]["rate"] == 100.0, lines


def test_zero_qty_skipped() -> None:
    """Zero-qty lines are dropped (no divide-by-zero)."""
    lines = _run_resolver([{
        "sku": "FG06476-CHOUHAN",
        "item_quantity": 0,
        "taxable_value": 0,
    }])
    assert lines == []
