"""§11 mirror refactor — locks the make_sales_invoice-based architecture.

Replaces test_b2b_invoice_mirror.py + test_invoice_mirror_gh181_discount.py
+ test_gh206_mirror_uses_erpnext_tax_template.py + test_gh214_mirror_gst_context.py
(all deleted). Those locked implementation details of the hand-copy
mirror — the whole class of bugs that made this refactor necessary.

Post-refactor invariants (what these tests lock):

  1. `make_sales_invoice(so.name)` is the SI-construction primitive —
     called with `ignore_permissions=True`; every field ERPNext knows
     how to map comes across natively (customer, GST context,
     item_tax_template, gst_treatment, etc.).

  2. Per-item qty override — EE's `item_quantity` replaces SO qty on
     each SI item; items with EE qty=0 or missing-from-EE dropped.

  3. Payment terms are COPIED from SO (ERPNext's make_sales_invoice
     deliberately excludes them via field_no_map; we WANT them).

  4. EE-specific overrides — invoice_id, invoice_number, posting_date,
     back-references land on the SI after the primitive returns.

  5. Idempotency — same EE invoice_id → same SI, no duplicates.

  6. Variance check — SI (from SO) vs EE.total_amount → throw
     InvoiceMirrorVariance if divergence >1%.

  7. Missing prerequisites throw InvoiceMirrorError (no SO, no
     invoice_id, stale SO reference).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror as mod


def _ee_row(**overrides):
    base = {
        "invoice_id": 999888,
        "invoice_number": "TEST-INV-001",
        "invoice_date": "2026-07-16",
        "invoice_currency_code": "INR",
        "total_amount": 4830.0,
        "order_items": [
            {"sku": "SKU-A", "item_quantity": 2},
            {"sku": "SKU-B", "item_quantity": 4},
        ],
        "warehouse_id": 53313,
    }
    base.update(overrides)
    return base


def _map_doc(sales_order="SO-TEST-001", name="ECS-B2B-SO-TEST-001", company="MMPL"):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.company = company
    return m


def _si_stub(name="SI-NEW-001", grand_total=4830.0, items=None):
    """Fabricate what make_sales_invoice would return — a Draft SI
    object with items that carry ERPNext's SI Item shape."""
    si = MagicMock()
    si.name = name
    si.grand_total = grand_total
    si.total_taxes_and_charges = grand_total - sum(
        (it.get("amount", 0) if isinstance(it, dict) else it.amount)
        for it in (items or [])
    ) if items else 0
    si.items = []
    for entry in (items or [
        {"item_code": "ITEM-A", "qty": 2, "rate": 300, "amount": 600, "idx": 1},
        {"item_code": "ITEM-B", "qty": 4, "rate": 1000, "amount": 4000, "idx": 2},
    ]):
        si.items.append(SimpleNamespace(**entry))
    si.insert = MagicMock()
    si.flags = SimpleNamespace(ignore_permissions=False)
    return si


class TestIdempotencyPath(unittest.TestCase):
    """Existing SI for this EE invoice_id → return it, don't rebuild."""

    def test_existing_si_returned_without_rebuild(self):
        with (
            patch.object(mod.frappe.db, "get_value", side_effect=[
                "SI-EXISTING",  # ecs_easyecom_invoice_id lookup
                4830.0,         # grand_total on existing SI
            ]),
            # make_sales_invoice MUST NOT be called on the idempotent path
            patch(
                "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice"
            ) as msi,
        ):
            result = mod.mirror_si_from_ee_response(
                map_doc=_map_doc(), ee_row=_ee_row(),
            )
        self.assertEqual(result["sales_invoice"], "SI-EXISTING")
        self.assertEqual(result["operation"], "already_exists")
        msi.assert_not_called()


class TestPrerequisitesThrow(unittest.TestCase):
    """Missing prerequisites raise InvoiceMirrorError with clear messages."""

    def test_no_invoice_id_raises(self):
        with self.assertRaises(mod.InvoiceMirrorError) as ctx:
            mod.mirror_si_from_ee_response(
                map_doc=_map_doc(), ee_row=_ee_row(invoice_id=None),
            )
        self.assertIn("invoice_id", str(ctx.exception))

    def test_map_without_sales_order_raises(self):
        with (
            patch.object(mod.frappe.db, "get_value", return_value=None),
            self.assertRaises(mod.InvoiceMirrorError) as ctx,
        ):
            mod.mirror_si_from_ee_response(
                map_doc=_map_doc(sales_order=""), ee_row=_ee_row(),
            )
        self.assertIn("no sales_order", str(ctx.exception))

    def test_stale_sales_order_reference_raises(self):
        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=False),
            self.assertRaises(mod.InvoiceMirrorError) as ctx,
        ):
            mod.mirror_si_from_ee_response(
                map_doc=_map_doc(sales_order="SO-DELETED"),
                ee_row=_ee_row(),
            )
        self.assertIn("not found", str(ctx.exception))


class TestMakeSalesInvoicePrimitiveUsed(unittest.TestCase):
    """Cornerstone: mirror calls make_sales_invoice — NOT frappe.new_doc.
    Any refactor that reverts to hand-building is caught here."""

    def _run(self, msi_return=None, **ee_overrides):
        """Standard patch stack: idempotency miss, SO exists, msi
        returns a stubbed SI, item map resolves everything."""
        si = msi_return or _si_stub()

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None  # idempotency miss
            if doctype == "Sales Order" and field == "payment_terms_template":
                return "90 days"  # SO has payment terms
            if doctype == "EasyEcom Item Map":
                # Map ITEM-A → SKU-A, ITEM-B → SKU-B
                code = (filters or {}).get("erpnext_name", "")
                return {"ITEM-A": "SKU-A", "ITEM-B": "SKU-B"}.get(code, "")
            if doctype == "EasyEcom Location":
                return "Warehouse-Mapped"
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch(
                "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
                return_value=si,
            ) as msi,
        ):
            result = mod.mirror_si_from_ee_response(
                map_doc=_map_doc(), ee_row=_ee_row(**ee_overrides),
            )
        return result, msi, si

    def test_make_sales_invoice_called_with_so_name_and_ignore_perms(self):
        _, msi, _ = self._run()
        msi.assert_called_once()
        kwargs = msi.call_args.kwargs
        self.assertEqual(kwargs.get("source_name"), "SO-TEST-001")
        self.assertTrue(kwargs.get("ignore_permissions"))

    def test_ee_backrefs_set_on_si_after_msi(self):
        """EE-specific fields land on the SI after msi returns."""
        _, _, si = self._run()
        self.assertEqual(si.ecs_easyecom_invoice_id, "999888")
        self.assertEqual(si.ecs_easyecom_invoice_number, "TEST-INV-001")
        self.assertEqual(si.ecs_easyecom_b2b_order_map, "ECS-B2B-SO-TEST-001")
        # gh#160 — update_stock=1 for Mode 1 invoice-first
        self.assertEqual(si.update_stock, 1)
        # gh#161 — set_posting_time=1 freezes posting_date
        self.assertEqual(si.set_posting_time, 1)

    def test_payment_terms_copied_from_source_so(self):
        """User decision #4 — payment_terms_template flows from SO
        (ERPNext's make_sales_invoice deliberately excludes it via
        field_no_map; we override AFTER msi returns)."""
        _, _, si = self._run()
        self.assertEqual(si.payment_terms_template, "90 days")

    def test_insert_called_on_si(self):
        """The returned SI actually gets persisted."""
        _, _, si = self._run()
        si.insert.assert_called_once()


class TestPerItemQtyOverride(unittest.TestCase):
    """User decision #3 — EE's item_quantity overrides SO qty per line.
    Items with qty=0 or missing-from-EE are dropped."""

    def _run(self, ee_qtys, msi_items=None):
        """ee_qtys: {sku: qty} dict.
        msi_items: list of SI items make_sales_invoice would return."""
        si = _si_stub(items=msi_items or [
            {"item_code": "ITEM-A", "qty": 2, "rate": 300, "amount": 600, "idx": 1},
            {"item_code": "ITEM-B", "qty": 4, "rate": 1000, "amount": 4000, "idx": 2},
        ])
        ee_row = _ee_row(order_items=[
            {"sku": sku, "item_quantity": qty}
            for sku, qty in ee_qtys.items()
        ])

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None
            if doctype == "Sales Order" and field == "payment_terms_template":
                return None
            if doctype == "EasyEcom Item Map":
                code = (filters or {}).get("erpnext_name", "")
                return {"ITEM-A": "SKU-A", "ITEM-B": "SKU-B"}.get(code, "")
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch(
                "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
                return_value=si,
            ),
        ):
            mod.mirror_si_from_ee_response(
                map_doc=_map_doc(), ee_row=ee_row,
            )
        return si

    def test_qty_from_ee_payload_overrides_so_qty(self):
        """EE says qty 3 for SKU-A, SI item ends up qty 3 (not SO's 2)."""
        si = self._run({"SKU-A": 3, "SKU-B": 4})
        by_code = {it.item_code: it for it in si.items}
        self.assertEqual(by_code["ITEM-A"].qty, 3)
        self.assertEqual(by_code["ITEM-B"].qty, 4)

    def test_zero_qty_item_dropped(self):
        """EE explicitly invoices 0 of SKU-A → SI has only ITEM-B."""
        si = self._run({"SKU-A": 0, "SKU-B": 4})
        codes = [it.item_code for it in si.items]
        self.assertNotIn("ITEM-A", codes)
        self.assertIn("ITEM-B", codes)

    def test_missing_from_ee_item_dropped(self):
        """EE doesn't include SKU-B at all → SI drops ITEM-B."""
        si = self._run({"SKU-A": 2})
        codes = [it.item_code for it in si.items]
        self.assertIn("ITEM-A", codes)
        self.assertNotIn("ITEM-B", codes)

    def test_amount_recomputed_after_qty_override(self):
        """rate × qty = amount when we override qty."""
        si = self._run({"SKU-A": 5, "SKU-B": 4})
        item_a = next(it for it in si.items if it.item_code == "ITEM-A")
        # rate=300, qty=5 → amount=1500
        self.assertEqual(item_a.amount, 1500)

    def test_idx_resequenced_after_drops(self):
        """After dropping items, remaining items get sequential idx."""
        si = self._run({"SKU-A": 0, "SKU-B": 4})
        self.assertEqual(len(si.items), 1)
        self.assertEqual(si.items[0].idx, 1)


class TestVarianceCheck(unittest.TestCase):
    """SI (built from SO) vs EE.total_amount — throw if divergence >1%."""

    def _run(self, si_grand_total, ee_total):
        si = _si_stub(grand_total=si_grand_total)

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None
            if doctype == "Sales Order":
                return None
            if doctype == "EasyEcom Item Map":
                code = (filters or {}).get("erpnext_name", "")
                return {"ITEM-A": "SKU-A", "ITEM-B": "SKU-B"}.get(code, "")
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=True),
            patch(
                "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
                return_value=si,
            ),
        ):
            return mod.mirror_si_from_ee_response(
                map_doc=_map_doc(),
                ee_row=_ee_row(total_amount=ee_total),
            )

    def test_exact_match_returns_ok(self):
        """Same total → variance 0 → no throw."""
        result = self._run(si_grand_total=4830.0, ee_total=4830.0)
        self.assertEqual(result["variance_pct"], 0.0)

    def test_sub_paise_rounding_tolerated(self):
        """Float rounding noise below 0.01% must NOT throw.
        ₹4,830.0000001 vs ₹4,830.0 is float noise, not real divergence."""
        result = self._run(si_grand_total=4830.0, ee_total=4830.0001)
        # No throw; variance is below 0.01% tolerance
        self.assertLess(abs(result["variance_pct"]), 0.01)

    def test_one_rupee_divergence_raises(self):
        """₹1 divergence on a ₹4,830 SO is 0.02% — must throw.
        User decision: ANY real divergence surfaces for human review."""
        with self.assertRaises(mod.InvoiceMirrorVariance) as ctx:
            self._run(si_grand_total=4830.0, ee_total=4831.0)
        self.assertIn("variance", str(ctx.exception).lower())

    def test_significant_divergence_raises_and_names_totals(self):
        """Large gap (₹530 lost/added) — must throw with both totals
        in the message so FDE sees the delta immediately."""
        with self.assertRaises(mod.InvoiceMirrorVariance) as ctx:
            self._run(si_grand_total=4830.0, ee_total=4300.0)
        msg = str(ctx.exception)
        self.assertIn("4830", msg)
        self.assertIn("4300", msg)

    def test_one_percent_still_raises(self):
        """Previously 1% was inside the threshold. User tightened:
        even 1% is unacceptable at B2B invoice scale (₹48+ on this
        SO). Must throw."""
        with self.assertRaises(mod.InvoiceMirrorVariance):
            # 4830 → 4878 is exactly 1% higher
            self._run(si_grand_total=4830.0, ee_total=4878.30)


class TestExtractIrnFields(unittest.TestCase):
    """Defensive IRN discovery — mirror IC fields if EE sends them."""

    def test_irn_at_row_level_extracted(self):
        result = mod._extract_irn_fields({
            "irn": "abc" * 21,
            "ack_no": "112010012345678",
            "ack_dt": "2026-07-16T14:15:00+05:30",
        })
        self.assertTrue(result["irn"].startswith("abc"))
        self.assertEqual(result["ack_no"], "112010012345678")

    def test_irn_in_nested_block_extracted(self):
        result = mod._extract_irn_fields({
            "einvoice": {"irn": "nested" * 10},
        })
        self.assertTrue(result["irn"].startswith("nested"))

    def test_no_irn_returns_empty(self):
        result = mod._extract_irn_fields({})
        self.assertEqual(result, {})

    def test_irn_qr_alias_names_scanned(self):
        result = mod._extract_irn_fields({"signed_qr_code": "qrdata"})
        self.assertEqual(result["signed_qr_code"], "qrdata")


class TestVariancePctHelper(unittest.TestCase):
    def test_zero_when_ee_total_zero(self):
        self.assertEqual(mod._variance_pct(100, 0), 0.0)

    def test_positive_when_si_higher(self):
        self.assertAlmostEqual(mod._variance_pct(110, 100), 10.0)

    def test_negative_when_si_lower(self):
        self.assertAlmostEqual(mod._variance_pct(90, 100), -10.0)


if __name__ == "__main__":
    unittest.main()
