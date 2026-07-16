"""gh#214 — mirror must copy the source SO's GST-determination context
onto the SI, and fail loud if EE billed tax but the SI computed none.

Regression background: gh#206 switched the mirror to ERPNext-native tax
computation, copying only `taxes_and_charges` (template name) + per-line
`item_tax_template`. India Compliance recomputes item-wise GST on
`si.insert()` from the SI's `place_of_supply` / `tax_category` / company
GSTIN — none of which gh#206 copied — so IC computed 0% GST and the SI
came back net-only (live: SO-2610405 → SI ₹3,600 vs SO ₹3,780).

These tests mock `si.insert()` (IC's real computation needs a bench), so
they lock the pre-insert CONTRACT: the correct GST context is set on the
SI before insert, and the post-insert guard fires when the recompute
still yields zero tax against a taxed EE order.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _fake_so(
    *,
    name="SAL-ORD-2026-TEST",
    company="Modern Marwar Private Limited",
    taxes_and_charges="Output GST Out-state - MMPL",
    tax_category="Registered Regular",
    place_of_supply="09-Uttar Pradesh",
    company_gstin="08AAMCM6783B1Z6",
    billing_address_gstin="09AAACC1206D2ZD",
    items=None,
):
    so = MagicMock()
    so.name = name
    so.company = company
    so.taxes_and_charges = taxes_and_charges
    so.tax_category = tax_category
    so.place_of_supply = place_of_supply
    so.company_gstin = company_gstin
    so.billing_address_gstin = billing_address_gstin
    so.items = items or []
    return so


def _fake_so_item(item_code, item_tax_template=None):
    return SimpleNamespace(
        item_code=item_code, item_tax_template=item_tax_template,
    )


def _fake_map_doc(sales_order="SAL-ORD-2026-TEST"):
    m = MagicMock()
    m.name = "ECS-B2B-SAL-ORD-TEST"
    m.sales_order = sales_order
    m.company = None
    m.get = lambda field, default=None: {"sales_invoice": None}.get(field, default)
    return m


def _run_mirror(*, source_so, ee_row, si_grand_total, si_total_tax):
    """Run the mirror with all Frappe side-effects stubbed. `si_grand_total`
    and `si_total_tax` model what IC's (mocked-out) recompute would leave on
    the SI. Returns the fake SI so tests can inspect the fields the mirror
    set on it. Raises whatever the mirror raises."""
    from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror

    map_doc = _fake_map_doc(sales_order=source_so.name)
    fake_si = MagicMock()
    fake_si.name = "SI-MIRROR-214"
    fake_si.grand_total = si_grand_total
    fake_si.total_taxes_and_charges = si_total_tax
    fake_si.taxes = []
    fake_si.items = []
    appended: list[dict] = []
    fake_si.append.side_effect = lambda field, row: (
        appended.append(row) if field == "items" else None
    )

    def _get_doc(doctype, name=None):
        if doctype == "Sales Order":
            return source_so
        if doctype == "Customer":
            return MagicMock(tax_id="GSTIN-TEST")
        return MagicMock()

    def _get_value(doctype, filters=None, field=None, **_kw):
        if doctype == "EasyEcom Customer Map":
            return "CUST-A"
        if doctype == "EasyEcom Item Map":
            return "ITEM-A"
        if doctype == "Sales Invoice":
            return None
        if doctype == "Item":
            return "71179090"
        return None

    with (
        patch.object(invoice_mirror.frappe, "new_doc", return_value=fake_si),
        patch.object(invoice_mirror.frappe, "get_doc", side_effect=_get_doc),
        patch.object(invoice_mirror.frappe.db, "get_value", side_effect=_get_value),
    ):
        invoice_mirror.mirror_si_from_ee_response(map_doc=map_doc, ee_row=ee_row)
    return fake_si, appended


def _ee_row(total_amount=630.0, total_tax=30.0):
    return {
        "invoice_id": 214,
        "invoice_number": "GH214-INV",
        "invoice_date": "2026-07-16",
        "invoice_currency_code": "INR",
        "merchant_c_id": 42,
        "total_amount": total_amount,
        "total_tax": total_tax,
        "order_items": [
            {"sku": "SKU-A", "item_quantity": 2, "taxable_value": 600.0},
        ],
    }


class TestGh214CopiesGstContext(unittest.TestCase):
    def test_copies_place_of_supply_tax_category_gstins(self):
        so = _fake_so(items=[_fake_so_item("ITEM-A", "GST 5% - MMPL")])
        # total_tax present + si tax present → guard passes; grand matches
        # → variance passes. We only care that context was copied.
        si, _ = _run_mirror(
            source_so=so, ee_row=_ee_row(total_amount=630.0, total_tax=30.0),
            si_grand_total=630.0, si_total_tax=30.0,
        )
        self.assertEqual(si.tax_category, "Registered Regular")
        self.assertEqual(si.place_of_supply, "09-Uttar Pradesh")
        self.assertEqual(si.company_gstin, "08AAMCM6783B1Z6")
        self.assertEqual(si.billing_address_gstin, "09AAACC1206D2ZD")

    def test_still_copies_template_and_item_tax_template(self):
        """gh#206 behavior preserved: template name + per-line template."""
        so = _fake_so(
            taxes_and_charges="Output GST Out-state - MMPL",
            items=[_fake_so_item("ITEM-A", "GST 5% - MMPL")],
        )
        si, appended = _run_mirror(
            source_so=so, ee_row=_ee_row(),
            si_grand_total=630.0, si_total_tax=30.0,
        )
        self.assertEqual(si.taxes_and_charges, "Output GST Out-state - MMPL")
        self.assertEqual(appended[0]["item_tax_template"], "GST 5% - MMPL")

    def test_blank_gst_field_on_so_not_copied(self):
        """A field the SO doesn't carry must not be forced onto the SI."""
        so = _fake_so(
            place_of_supply=None,
            items=[_fake_so_item("ITEM-A", "GST 5% - MMPL")],
        )
        si, _ = _run_mirror(
            source_so=so, ee_row=_ee_row(),
            si_grand_total=630.0, si_total_tax=30.0,
        )
        # place_of_supply was never set (stays the MagicMock default, not
        # a copied value) — assert we did not assign the SO's None.
        self.assertNotEqual(si.place_of_supply, None)


class TestGh214FailLoudGuard(unittest.TestCase):
    def test_raises_when_ee_taxed_but_si_zero(self):
        """The live SO-2610405 case: EE billed ₹30 tax, SI recomputed ₹0
        → InvoiceMirrorError (hard-fails through the handler, not
        swallowed like a variance)."""
        from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror

        so = _fake_so(items=[_fake_so_item("ITEM-A", "GST 5% - MMPL")])
        with self.assertRaises(invoice_mirror.InvoiceMirrorError) as ctx:
            _run_mirror(
                source_so=so, ee_row=_ee_row(total_amount=630.0, total_tax=30.0),
                si_grand_total=600.0, si_total_tax=0.0,
            )
        msg = str(ctx.exception)
        self.assertIn("SI-MIRROR-214", msg)
        self.assertIn("GST did not apply", msg)

    def test_silent_when_zero_tax_order(self):
        """Legitimately zero-tax order (EE total_tax = 0) → no guard."""
        so = _fake_so(items=[_fake_so_item("ITEM-A", None)])
        si, _ = _run_mirror(
            source_so=so, ee_row=_ee_row(total_amount=600.0, total_tax=0.0),
            si_grand_total=600.0, si_total_tax=0.0,
        )
        self.assertEqual(si.name, "SI-MIRROR-214")  # completed, no raise

    def test_silent_when_si_tax_matches_ee(self):
        """EE billed tax and the SI computed it → no guard, completes."""
        so = _fake_so(items=[_fake_so_item("ITEM-A", "GST 5% - MMPL")])
        si, _ = _run_mirror(
            source_so=so, ee_row=_ee_row(total_amount=630.0, total_tax=30.0),
            si_grand_total=630.0, si_total_tax=30.0,
        )
        self.assertEqual(si.name, "SI-MIRROR-214")


if __name__ == "__main__":
    unittest.main()
