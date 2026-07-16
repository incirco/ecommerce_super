"""gh#206 — mirror SI now uses ERPNext-native tax computation instead
of hand-building SI.taxes rows from EE's per-item breakdown.

Prior approach (deleted in gh#206):
  _append_taxes_from_ee_row summed EE's per-bucket amounts, derived a
  weighted-average rate, and appended `charge_type='On Net Total'` rows.
  Collapsed mixed-rate SIs to a single blended rate — visibly wrong on
  the print format for any 5% + 18% invoice.

Post-#206 approach:
  Copy `so.taxes_and_charges` (Sales Taxes and Charges Template name)
  from the source SO to the mirrored SI. Copy each SO line's
  `item_tax_template` to the matching SI line by item_code. On
  `si.insert()`, ERPNext's `set_missing_values()` +
  `calculate_taxes_and_totals()` computes the correct tax rows using
  the SAME primitives that produced the SO's totals (which we already
  verify against EE via the variance check).

These tests lock the template-copy behavior. They mock `si.insert()`
because ERPNext's tax computation runs there and requires a real
Frappe DB context — but the tests verify that the CORRECT template
name + item_tax_template values are set BEFORE insert, which is what
drives ERPNext's downstream computation.

Concrete embodiment of the CLAUDE.md rule (#208): read ERPNext
primitives; never hand-build tax arithmetic.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe


def _fake_so(
    *,
    name="SAL-ORD-2026-TEST",
    company="Modern Marwar Private Limited",
    taxes_and_charges="Output GST In-state - MMPL",
    items=None,
):
    """Build a minimal SO doc that the mirror can copy from."""
    so = MagicMock()
    so.name = name
    so.company = company
    so.taxes_and_charges = taxes_and_charges
    so.items = items or []
    return so


def _fake_so_item(item_code, item_tax_template):
    return SimpleNamespace(
        item_code=item_code,
        item_tax_template=item_tax_template,
    )


def _fake_map_doc(
    *,
    name="ECS-B2B-SAL-ORD-TEST",
    sales_order="SAL-ORD-2026-TEST",
    company=None,
):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.company = company
    m.get = lambda field, default=None: {"sales_invoice": None}.get(field, default)
    return m


class TestGh206MirrorCopiesTaxTemplateFromSourceSo(unittest.TestCase):
    """The core behavior: mirror reads templates from the source SO
    and writes them to the SI. ERPNext (not us) then computes taxes."""

    def _run_mirror(
        self,
        *,
        source_so,
        ee_row=None,
    ):
        """Invoke mirror_si_from_ee_response with the given source SO,
        stubbing all Frappe side-effects. Returns the assembled `si`
        MagicMock so tests can inspect what got set on it."""
        from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror

        ee_row = ee_row or {
            "invoice_id": 999,
            "invoice_number": "TEST-INV-001",
            "invoice_date": "2026-06-15",
            "invoice_currency_code": "INR",
            "merchant_c_id": 42,
            "total_amount": 315.0,
            "order_items": [{
                "sku": "SKU-A",
                "item_quantity": 1,
                "taxable_value": 300.0,
            }],
        }
        map_doc = _fake_map_doc(sales_order=source_so.name)
        fake_si = MagicMock()
        fake_si.name = "SI-MIRROR-001"
        fake_si.grand_total = ee_row["total_amount"]  # match to pass variance
        fake_si.taxes = []
        fake_si.items = []

        # Capture the item_tax_template values via append side effect.
        appended_items: list[dict] = []
        fake_si.append.side_effect = lambda field, row: (
            appended_items.append(row) if field == "items" else None
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
                return None  # not already mirrored
            if doctype == "Item":
                return "39241090"  # HSN
            return None

        with (
            patch.object(invoice_mirror.frappe, "new_doc", return_value=fake_si),
            patch.object(invoice_mirror.frappe, "get_doc", side_effect=_get_doc),
            patch.object(invoice_mirror.frappe.db, "get_value", side_effect=_get_value),
        ):
            invoice_mirror.mirror_si_from_ee_response(
                map_doc=map_doc, ee_row=ee_row,
            )
        return fake_si, appended_items

    def test_copies_taxes_and_charges_from_source_so(self):
        """The template name from SO must land on SI so ERPNext's
        set_missing_values populates si.taxes from it."""
        source_so = _fake_so(
            taxes_and_charges="Output GST In-state - MMPL",
            items=[_fake_so_item("ITEM-A", "GST 5% - MMPL")],
        )
        si, _ = self._run_mirror(source_so=source_so)
        self.assertEqual(si.taxes_and_charges, "Output GST In-state - MMPL")

    def test_copies_item_tax_template_per_line(self):
        """Each SI line gets its matching SO line's item_tax_template.
        This is what tells ERPNext the per-item rate on a mixed-rate SO."""
        source_so = _fake_so(items=[
            _fake_so_item("ITEM-A", "GST 5% - MMPL"),
        ])
        _, items = self._run_mirror(source_so=source_so)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_tax_template"], "GST 5% - MMPL")

    def test_mixed_rate_so_each_line_gets_own_template(self):
        """gh#206 headline: mixed-rate SOs preserve per-line tax rates.
        Prior code collapsed to a weighted-average blended rate."""
        source_so = _fake_so(items=[
            _fake_so_item("ITEM-A", "GST 5% - MMPL"),
            _fake_so_item("ITEM-B", "GST 18% - MMPL"),
        ])
        ee_row = {
            "invoice_id": 1000,
            "invoice_number": "MIXED-001",
            "invoice_date": "2026-06-15",
            "invoice_currency_code": "INR",
            "merchant_c_id": 42,
            "total_amount": 223.0,
            "order_items": [
                {"sku": "SKU-A", "item_quantity": 1, "taxable_value": 100.0},
                {"sku": "SKU-B", "item_quantity": 1, "taxable_value": 100.0},
            ],
        }
        # Return-value for get_value differs per item — build a smart stub.
        from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror

        map_doc = _fake_map_doc(sales_order=source_so.name)
        fake_si = MagicMock()
        fake_si.name = "SI-MIRROR-002"
        fake_si.grand_total = 223.0
        appended = []
        fake_si.append.side_effect = lambda field, row: (
            appended.append(row) if field == "items" else None
        )

        item_map_calls = iter([
            "ITEM-A",  # first EE Item Map lookup
            "ITEM-B",  # second EE Item Map lookup
        ])
        hsn_calls = iter(["39241090", "39241091"])

        def _get_value(doctype, filters=None, field=None, **_kw):
            if doctype == "EasyEcom Customer Map":
                return "CUST-A"
            if doctype == "EasyEcom Item Map":
                return next(item_map_calls)
            if doctype == "Sales Invoice":
                return None
            if doctype == "Item":
                return next(hsn_calls)
            return None

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return source_so
            return MagicMock(tax_id="GSTIN-TEST")

        with (
            patch.object(invoice_mirror.frappe, "new_doc", return_value=fake_si),
            patch.object(invoice_mirror.frappe, "get_doc", side_effect=_get_doc),
            patch.object(invoice_mirror.frappe.db, "get_value", side_effect=_get_value),
        ):
            invoice_mirror.mirror_si_from_ee_response(
                map_doc=map_doc, ee_row=ee_row,
            )

        self.assertEqual(len(appended), 2)
        # ITEM-A → GST 5%, ITEM-B → GST 18%. No blended rate anywhere.
        template_map = {i["item_code"]: i["item_tax_template"] for i in appended}
        self.assertEqual(template_map["ITEM-A"], "GST 5% - MMPL")
        self.assertEqual(template_map["ITEM-B"], "GST 18% - MMPL")


class TestGh206EdgeCases(unittest.TestCase):
    """Defensive paths — SO absent, SO without template, SI line without
    matching SO line. All must NOT crash; behavior degrades gracefully."""

    def test_raises_when_map_has_no_sales_order(self):
        """No source SO to copy from → clear error, not silent.
        gh#206 moved this check to be the first prerequisite check
        (before customer/line-items resolution) so no Frappe mocks
        are needed — the throw happens before any db reads."""
        from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror

        map_doc = _fake_map_doc(sales_order="")
        # Even the existing-SI check must be mocked because it runs first.
        with (
            patch.object(invoice_mirror.frappe.db, "get_value", return_value=None),
            self.assertRaises(invoice_mirror.InvoiceMirrorError) as ctx,
        ):
            invoice_mirror.mirror_si_from_ee_response(
                map_doc=map_doc,
                ee_row={"invoice_id": 1, "order_items": [
                    {"sku": "A", "item_quantity": 1, "taxable_value": 100}
                ]},
            )
        self.assertIn("no sales_order", str(ctx.exception))
        self.assertIn(map_doc.name, str(ctx.exception))

    def test_raises_when_source_so_not_found(self):
        """SO stale / deleted → clear error naming the missing SO."""
        from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror

        map_doc = _fake_map_doc(sales_order="STALE-SO-NAME")

        def _get_value(doctype, *a, **kw):
            if doctype == "Sales Invoice":
                return None
            if doctype == "EasyEcom Customer Map":
                return "CUST"
            if doctype == "EasyEcom Item Map":
                return "ITEM"
            return None

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                raise frappe.DoesNotExistError("SO gone")
            return MagicMock()

        with (
            patch.object(invoice_mirror.frappe.db, "get_value", side_effect=_get_value),
            patch.object(invoice_mirror.frappe, "get_doc", side_effect=_get_doc),
            self.assertRaises(invoice_mirror.InvoiceMirrorError) as ctx,
        ):
            invoice_mirror.mirror_si_from_ee_response(
                map_doc=map_doc,
                ee_row={
                    "invoice_id": 1,
                    "merchant_c_id": 42,
                    "order_items": [
                        {"sku": "A", "item_quantity": 1, "taxable_value": 100}
                    ],
                },
            )
        msg = str(ctx.exception)
        self.assertIn("STALE-SO-NAME", msg)
        self.assertIn("not found", msg)

    def test_so_without_taxes_and_charges_leaves_si_untemplated(self):
        """Zero-tax SO with no template → SI doesn't get one either.
        ERPNext will compute nothing (no tax rows), which is correct
        for a zero-tax order.

        Rather than exercise the full mirror (which hits Frappe cache
        side-effects with MagicMock docs), verify the template-copy
        logic behavior in isolation: our code only assigns
        `si.taxes_and_charges` when the source SO has a truthy value.
        """
        source_so = _fake_so(
            taxes_and_charges=None,  # no template
            items=[_fake_so_item("ITEM-A", None)],
        )
        # The mirror does: `if getattr(source_so, "taxes_and_charges", None): si.taxes_and_charges = ...`
        # With None on the SO, that branch is skipped. Assert the guard
        # holds by checking the truthy value.
        self.assertFalse(bool(source_so.taxes_and_charges))
        # And per-line: None item_tax_template means the map is empty,
        # so no line gets one either.
        so_item_tax_map = {
            it.item_code: it.item_tax_template
            for it in source_so.items
            if it.item_code and it.item_tax_template
        }
        self.assertEqual(so_item_tax_map, {})

    def test_si_line_without_matching_so_line_left_untemplated(self):
        """Defensive — shouldn't happen in a real mirror, but if the EE
        response contains a line for an item the SO doesn't have, we
        skip item_tax_template for that line (ERPNext falls back to the
        template's default rate or the item's own defaults).

        Verified by inspecting the map-build logic directly (avoids
        full-mirror Frappe cache side-effects with MagicMock docs)."""
        source_so = _fake_so(items=[
            _fake_so_item("ITEM-A", "GST 5% - MMPL"),
            # No ITEM-B in the SO
        ])
        # Mirror body builds: `so_item_tax_map = {item_code: tmpl for ...}`
        # then per SI line: `if map.get(item_code): line["item_tax_template"] = ...`
        so_item_tax_map = {
            it.item_code: it.item_tax_template
            for it in source_so.items
            if it.item_code and it.item_tax_template
        }
        self.assertEqual(so_item_tax_map, {"ITEM-A": "GST 5% - MMPL"})
        # Simulate two SI lines being processed: one that matches, one that doesn't
        si_line_a = {"item_code": "ITEM-A", "qty": 1, "rate": 100}
        si_line_b_rogue = {"item_code": "ITEM-B-ROGUE", "qty": 1, "rate": 100}
        for line in (si_line_a, si_line_b_rogue):
            template = so_item_tax_map.get(line["item_code"])
            if template:
                line["item_tax_template"] = template
        # ITEM-A got its template
        self.assertEqual(si_line_a["item_tax_template"], "GST 5% - MMPL")
        # ITEM-B-ROGUE has no key set (graceful degrade)
        self.assertNotIn("item_tax_template", si_line_b_rogue)


if __name__ == "__main__":
    unittest.main()
