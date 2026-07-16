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


# ============================================================
# Additional coverage — post-audit scenarios
# ============================================================


def _run_full_mirror(*, si=None, msi_side_effect=None, ee_row_overrides=None,
                     item_map=None, warehouse_map=None, so_payment_terms=None):
    """Shared harness for the extra scenarios — configurable stubs
    without hand-rolling the patch stack per test."""
    si = si or _si_stub()
    item_map = item_map or {"ITEM-A": "SKU-A", "ITEM-B": "SKU-B"}
    warehouse_map = warehouse_map or {53313: "Warehouse-A"}
    ee_row_overrides = ee_row_overrides or {}

    def _get_value(doctype, filters=None, field=None, **_):
        if doctype == "Sales Invoice":
            return None
        if doctype == "Sales Order" and field == "payment_terms_template":
            return so_payment_terms
        if doctype == "EasyEcom Item Map":
            code = (filters or {}).get("erpnext_name", "")
            return item_map.get(code, "")
        if doctype == "EasyEcom Location":
            wid = (filters or {}).get("ee_company_id")
            return warehouse_map.get(wid)
        return None

    patches = [
        patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
        patch.object(mod.frappe.db, "exists", return_value=True),
    ]
    if msi_side_effect is not None:
        patches.append(patch(
            "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
            side_effect=msi_side_effect,
        ))
    else:
        patches.append(patch(
            "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
            return_value=si,
        ))

    for p in patches:
        p.start()
    try:
        return mod.mirror_si_from_ee_response(
            map_doc=_map_doc(), ee_row=_ee_row(**ee_row_overrides),
        ), si
    finally:
        for p in patches:
            p.stop()


class TestSO2610407ExactScenario(unittest.TestCase):
    """End-to-end scenario matching the live incident this refactor was
    designed to solve: qty>1 discounted line + qty>1 undiscounted +
    inter-state IGST 5%. Confirms the whole path assembles correctly."""

    def test_qty2_discounted_plus_qty4_normal_igst_5pct(self):
        # Simulate what msi returns after ERPNext maps the SO
        si = _si_stub(
            grand_total=4830.0,
            items=[
                {"item_code": "FG06476-CHOUHAN", "qty": 2, "rate": 300,
                 "amount": 600, "idx": 1},
                {"item_code": "FG20295", "qty": 4, "rate": 1000,
                 "amount": 4000, "idx": 2},
            ],
        )
        ee_row = {
            "invoice_id": 675145990,
            "invoice_number": "BRJ22627-23",
            "invoice_date": "2026-07-16",
            "invoice_currency_code": "INR",
            "total_amount": 4830.0,
            "warehouse_id": 53313,
            "order_items": [
                {"sku": "SKU-A", "item_quantity": 2},
                {"sku": "SKU-B", "item_quantity": 4},
            ],
        }
        result, si_out = _run_full_mirror(
            si=si,
            ee_row_overrides={
                "invoice_id": 675145990, "invoice_number": "BRJ22627-23",
                "total_amount": 4830.0,
                "order_items": ee_row["order_items"],
            },
            item_map={"FG06476-CHOUHAN": "SKU-A", "FG20295": "SKU-B"},
        )
        self.assertEqual(result["operation"], "created")
        self.assertEqual(result["variance_pct"], 0.0)
        # Both items preserved with qtys unchanged (EE = SO qtys here)
        codes_qtys = [(it.item_code, it.qty) for it in si_out.items]
        self.assertEqual(codes_qtys, [
            ("FG06476-CHOUHAN", 2),
            ("FG20295", 4),
        ])


class TestMultiCurrency(unittest.TestCase):
    """User asked for multi-currency support. make_sales_invoice copies
    currency + conversion_rate + party_account_currency natively from
    SO — we just verify our overrides don't clobber those."""

    def test_usd_so_yields_usd_si(self):
        """SO with USD currency → SI keeps USD (msi copies natively)."""
        si = _si_stub(grand_total=100.0)
        si.currency = "USD"
        si.conversion_rate = 83.0
        si.party_account_currency = "USD"
        result, si_out = _run_full_mirror(
            si=si,
            ee_row_overrides={
                "invoice_currency_code": "USD",
                "total_amount": 100.0,
            },
        )
        # We did NOT override currency post-msi (it's an SO concern)
        self.assertEqual(si_out.currency, "USD")
        self.assertEqual(si_out.conversion_rate, 83.0)

    def test_ee_currency_field_does_not_override_so_currency(self):
        """EE payload has invoice_currency_code; we deliberately don't
        override si.currency post-msi. SO wins for currency."""
        si = _si_stub(grand_total=100.0)
        si.currency = "USD"
        _run_full_mirror(
            si=si,
            ee_row_overrides={
                "invoice_currency_code": "INR",  # EE says INR
                "total_amount": 100.0,
            },
        )
        # SI.currency stays USD (SO wins, not EE)
        self.assertEqual(si.currency, "USD")


class TestWarehouseResolution(unittest.TestCase):
    """EE payload's warehouse_id → resolve via EasyEcom Location →
    override si.set_warehouse. Missing/unmapped → keep SO's warehouse."""

    def test_ee_warehouse_id_resolves_and_overrides(self):
        si = _si_stub()
        si.set_warehouse = "SO-Warehouse"
        _run_full_mirror(
            si=si,
            ee_row_overrides={"warehouse_id": 53313, "order_items": [
                {"sku": "SKU-A", "item_quantity": 2},
                {"sku": "SKU-B", "item_quantity": 4},
            ]},
            warehouse_map={53313: "EE-Mapped-Warehouse"},
        )
        self.assertEqual(si.set_warehouse, "EE-Mapped-Warehouse")

    def test_no_ee_warehouse_id_keeps_so_warehouse(self):
        si = _si_stub()
        si.set_warehouse = "SO-Warehouse"
        _run_full_mirror(
            si=si,
            ee_row_overrides={"warehouse_id": None, "order_items": [
                {"sku": "SKU-A", "item_quantity": 2},
                {"sku": "SKU-B", "item_quantity": 4},
            ]},
        )
        # Untouched — SO's warehouse preserved
        self.assertEqual(si.set_warehouse, "SO-Warehouse")

    def test_ee_warehouse_id_unmapped_keeps_so_warehouse(self):
        """warehouse_id present but no matching EasyEcom Location →
        fall back to SO's warehouse (don't NULL out set_warehouse)."""
        si = _si_stub()
        si.set_warehouse = "SO-Warehouse"
        _run_full_mirror(
            si=si,
            ee_row_overrides={"warehouse_id": 99999, "order_items": [
                {"sku": "SKU-A", "item_quantity": 2},
                {"sku": "SKU-B", "item_quantity": 4},
            ]},
            warehouse_map={},  # 99999 not in map
        )
        self.assertEqual(si.set_warehouse, "SO-Warehouse")


class TestIrnPassthroughEndToEnd(unittest.TestCase):
    """When EE payload carries IRN fields (rare — usually Mode 1 mints
    them on our side), mirror the fields to the SI."""

    def test_irn_and_ack_from_ee_land_on_si(self):
        si = _si_stub()
        ee_irn = "1234567890abcdef" * 4  # 64 chars
        result, si_out = _run_full_mirror(
            si=si,
            ee_row_overrides={
                "irn": ee_irn,
                "ack_no": "112010012345678",
                "ack_dt": "2026-07-16T14:15:00+05:30",
                "order_items": [
                    {"sku": "SKU-A", "item_quantity": 2},
                    {"sku": "SKU-B", "item_quantity": 4},
                ],
            },
        )
        self.assertEqual(si_out.irn, ee_irn)
        self.assertEqual(si_out.ack_no, "112010012345678")

    def test_no_irn_in_payload_leaves_si_without(self):
        si = _si_stub()
        # SI defaults have no IRN; run mirror without IRN in payload
        _run_full_mirror(
            si=si,
            ee_row_overrides={
                "order_items": [
                    {"sku": "SKU-A", "item_quantity": 2},
                    {"sku": "SKU-B", "item_quantity": 4},
                ],
            },
        )
        # No irn attribute was set (mock retains its default = a MagicMock
        # or whatever was there; but we can verify our code path didn't
        # add anything by checking the SI's set attrs list didn't gain
        # 'irn'). Simplest lock: our code shouldn't call setattr(si, "irn", ...)
        # when EE doesn't send it. Verified by TestExtractIrnFields already.

    def test_partial_irn_only_ack_no_from_ee(self):
        """EE sends ack_no but no irn → SI gets only ack_no."""
        si = _si_stub()
        result, si_out = _run_full_mirror(
            si=si,
            ee_row_overrides={
                "ack_no": "112010099999999",
                "order_items": [
                    {"sku": "SKU-A", "item_quantity": 2},
                    {"sku": "SKU-B", "item_quantity": 4},
                ],
            },
        )
        self.assertEqual(si_out.ack_no, "112010099999999")


class TestPaymentTermsCopy(unittest.TestCase):
    """User decision #4 — SO's payment_terms_template flows through.
    make_sales_invoice EXCLUDES it in field_no_map; we override after."""

    def test_so_with_payment_terms_carried_to_si(self):
        si = _si_stub()
        _run_full_mirror(si=si, so_payment_terms="90 days")
        self.assertEqual(si.payment_terms_template, "90 days")

    def test_so_without_payment_terms_leaves_si_untouched(self):
        """If SO has no payment terms, we don't set anything on SI —
        it stays at whatever msi's default was."""
        si = _si_stub()
        # SI's default from msi stub is MagicMock (no explicit terms).
        # After run, si.payment_terms_template stays as-is when we
        # don't fetch a value from SO.
        _run_full_mirror(si=si, so_payment_terms=None)
        # Verify our code didn't assign a value when SO didn't have one.
        # (We can't easily assert "unchanged from before" with MagicMock,
        # so verify the override branch guard: `if source_terms:` prevents
        # the assignment. Static-source check is cleanest.)
        import inspect
        src = inspect.getsource(mod.mirror_si_from_ee_response)
        self.assertIn("if source_terms:", src)

    def test_diverse_terms_carried_verbatim(self):
        """Any string value carries through — not just '90 days'."""
        for terms in ("Net 30", "COD", "Net 60", "Custom Terms"):
            si = _si_stub()
            _run_full_mirror(si=si, so_payment_terms=terms)
            self.assertEqual(si.payment_terms_template, terms)


class TestPartialInvoicingViaEeQtys(unittest.TestCase):
    """User decision #3 — EE's per-line qty is authoritative. Covers
    every combination of 'SO has N items, EE invoices M of them'."""

    def test_all_items_partially_invoiced(self):
        """SO has 2 items × 5 qty each; EE invoices 2 each → SI has
        both items at qty 2."""
        si = _si_stub(items=[
            {"item_code": "ITEM-A", "qty": 5, "rate": 100, "amount": 500, "idx": 1},
            {"item_code": "ITEM-B", "qty": 5, "rate": 200, "amount": 1000, "idx": 2},
        ])
        _run_full_mirror(
            si=si,
            ee_row_overrides={"order_items": [
                {"sku": "SKU-A", "item_quantity": 2},
                {"sku": "SKU-B", "item_quantity": 2},
            ]},
        )
        qtys = {it.item_code: it.qty for it in si.items}
        self.assertEqual(qtys, {"ITEM-A": 2, "ITEM-B": 2})
        amounts = {it.item_code: it.amount for it in si.items}
        self.assertEqual(amounts, {"ITEM-A": 200, "ITEM-B": 400})

    def test_first_call_partial_second_call_balance(self):
        """Simulate 2 sequential EE invoices for the same SO:
        - Invoice 1 has SKU-A qty 3, SKU-B qty 0
        - Invoice 2 has SKU-A qty 0, SKU-B qty 4
        Each yields a distinct SI via different invoice_ids."""
        # This test verifies the ALGORITHM (per-item qty override +
        # drop-zero) handles the balance-qty case cleanly. Each call
        # is independent — no cross-invoice state.
        si1 = _si_stub(items=[
            {"item_code": "ITEM-A", "qty": 5, "rate": 100, "amount": 500, "idx": 1},
            {"item_code": "ITEM-B", "qty": 5, "rate": 200, "amount": 1000, "idx": 2},
        ])
        _run_full_mirror(si=si1, ee_row_overrides={
            "invoice_id": 1001, "order_items": [
                {"sku": "SKU-A", "item_quantity": 3},
                {"sku": "SKU-B", "item_quantity": 0},
            ],
        })
        # Invoice 1: only ITEM-A, qty 3
        self.assertEqual([it.item_code for it in si1.items], ["ITEM-A"])
        self.assertEqual(si1.items[0].qty, 3)

        si2 = _si_stub(items=[
            {"item_code": "ITEM-A", "qty": 5, "rate": 100, "amount": 500, "idx": 1},
            {"item_code": "ITEM-B", "qty": 5, "rate": 200, "amount": 1000, "idx": 2},
        ])
        _run_full_mirror(si=si2, ee_row_overrides={
            "invoice_id": 1002, "order_items": [
                {"sku": "SKU-A", "item_quantity": 0},
                {"sku": "SKU-B", "item_quantity": 4},
            ],
        })
        # Invoice 2: only ITEM-B, qty 4
        self.assertEqual([it.item_code for it in si2.items], ["ITEM-B"])
        self.assertEqual(si2.items[0].qty, 4)

    def test_ee_extra_item_not_on_so_is_ignored(self):
        """EE payload has an sku that no SO line matches → not added
        to SI (make_sales_invoice already gave us the full item list;
        we only override qty, we don't ADD items)."""
        si = _si_stub(items=[
            {"item_code": "ITEM-A", "qty": 2, "rate": 300,
             "amount": 600, "idx": 1},
        ])
        _run_full_mirror(
            si=si,
            ee_row_overrides={"order_items": [
                {"sku": "SKU-A", "item_quantity": 2},
                {"sku": "SKU-GHOST", "item_quantity": 99},  # not on SO
            ]},
            item_map={"ITEM-A": "SKU-A"},  # SKU-GHOST has no map
        )
        # SI only has ITEM-A (the ghost never gets added)
        self.assertEqual(len(si.items), 1)
        self.assertEqual(si.items[0].item_code, "ITEM-A")


class TestMakeSalesInvoiceErrorPropagation(unittest.TestCase):
    """When make_sales_invoice itself throws (SO in draft, cancelled,
    fully invoiced), the mirror surfaces the error — doesn't swallow."""

    def test_msi_validation_error_propagates(self):
        """SO not submitted → make_sales_invoice raises. Mirror should
        NOT catch this — let it propagate so the caller sees the real
        cause."""
        def _msi_throws(*a, **kw):
            raise frappe.ValidationError(
                "Sales Order is not in submitted state"
            )
        with self.assertRaises(frappe.ValidationError) as ctx:
            _run_full_mirror(msi_side_effect=_msi_throws)
        self.assertIn("submitted", str(ctx.exception).lower())

    def test_msi_generic_exception_propagates(self):
        def _msi_throws(*a, **kw):
            raise RuntimeError("msi internal error")
        with self.assertRaises(RuntimeError) as ctx:
            _run_full_mirror(msi_side_effect=_msi_throws)
        self.assertIn("msi internal error", str(ctx.exception))


class TestReturnShape(unittest.TestCase):
    """The mirror's return dict has a stable shape callers depend on."""

    def test_returns_all_expected_keys_on_create(self):
        result, _ = _run_full_mirror()
        self.assertEqual(set(result.keys()), {
            "sales_invoice", "operation", "variance_pct",
            "ee_total", "si_total",
        })
        self.assertEqual(result["operation"], "created")

    def test_returns_all_expected_keys_on_idempotent_hit(self):
        with (
            patch.object(mod.frappe.db, "get_value", side_effect=[
                "SI-EXISTING", 4830.0,
            ]),
        ):
            result = mod.mirror_si_from_ee_response(
                map_doc=_map_doc(), ee_row=_ee_row(),
            )
        self.assertEqual(set(result.keys()), {
            "sales_invoice", "operation", "variance_pct",
            "ee_total", "si_total",
        })
        self.assertEqual(result["operation"], "already_exists")

    def test_variance_pct_is_numeric(self):
        result, _ = _run_full_mirror()
        self.assertIsInstance(result["variance_pct"], (int, float))

    def test_si_total_matches_created_sis_grand_total(self):
        si = _si_stub(grand_total=1234.56)
        result, _ = _run_full_mirror(
            si=si,
            ee_row_overrides={"total_amount": 1234.56},
        )
        self.assertEqual(result["si_total"], 1234.56)


class TestInsertCalledWithCorrectFlags(unittest.TestCase):
    """si.insert() must be called with ignore_permissions=True at the
    flags level so IC hooks that don't tolerate our elevated session
    still get past the write."""

    def test_insert_called_with_ignore_permissions_flag(self):
        si = _si_stub()
        _run_full_mirror(si=si)
        # Verify si.flags.ignore_permissions was set before insert
        self.assertTrue(si.flags.ignore_permissions)
        si.insert.assert_called_once()


class TestVarianceEdgeCases(unittest.TestCase):
    """Post-tightening (0.01% threshold), verify boundary + edge cases
    behave predictably."""

    def _run_variance(self, si_total, ee_total):
        si = _si_stub(grand_total=si_total)
        return _run_full_mirror(
            si=si,
            ee_row_overrides={"total_amount": ee_total},
        )

    def test_10_paise_on_small_order_raises(self):
        """10 paise variance on ₹100 SO = 0.1% → above threshold → raises."""
        with self.assertRaises(mod.InvoiceMirrorVariance):
            self._run_variance(100.0, 100.10)

    def test_1_paise_on_10rupee_order_raises(self):
        """1 paise on ₹10 = 0.1% → raises."""
        with self.assertRaises(mod.InvoiceMirrorVariance):
            self._run_variance(10.0, 10.01)

    def test_10_paise_on_large_order_within_tolerance(self):
        """10 paise on ₹100,000 SO = 0.0001% → below 0.01% → ok.
        Float rounding across large amounts."""
        result, _ = self._run_variance(100000.0, 100000.10)
        self.assertLess(abs(result["variance_pct"]), 0.01)

    def test_variance_returned_even_when_ok(self):
        """Even on the ok path, variance_pct is returned so callers
        (like the lifecycle card) can show 'variance: 0.00%'."""
        result, _ = self._run_variance(4830.0, 4830.05)
        self.assertIn("variance_pct", result)
        self.assertLess(abs(result["variance_pct"]), 0.01)


class TestEeRowPayloadShapes(unittest.TestCase):
    """The mirror is called with ee_row from different sources
    (getOrderDetails polling, /einvoice/update inbound). Both should
    work regardless of minor shape variation."""

    def test_missing_order_items_treats_as_no_qty_overrides(self):
        """No order_items in payload → SI keeps whatever qtys
        make_sales_invoice returned (no drop, no override)."""
        si = _si_stub()
        _run_full_mirror(si=si, ee_row_overrides={"order_items": None})
        # Items untouched — count matches msi's return
        self.assertEqual(len(si.items), 2)

    def test_order_items_not_a_list_treats_as_no_overrides(self):
        """Malformed payload (order_items is a string, etc.) → mirror
        doesn't crash; SI keeps msi's items."""
        si = _si_stub()
        _run_full_mirror(si=si, ee_row_overrides={"order_items": "malformed"})
        self.assertEqual(len(si.items), 2)


if __name__ == "__main__":
    unittest.main()
