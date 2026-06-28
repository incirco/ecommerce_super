"""§11.5.2 Mode 2 — Sales Invoice mirror unit tests.

Covers the resolution + variance + idempotency surfaces in
flows/b2b_sales/invoice_mirror.py without hitting a real bench DB
(uses mocks).

For full integration with real Item / Customer / Sales Invoice
documents, see tests/integration/test_b2b_invoice_mirror_*.py
(to be added when bench setup makes it cheap).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
    VARIANCE_THRESHOLD_PCT,
    InvoiceMirrorError,
    InvoiceMirrorVariance,
    _extract_irn_fields,
    _parse_posting_date,
    _resolve_line_items,
    _resolve_pdf_url,
    _resolve_warehouse,
    _variance_pct,
    mirror_si_from_ee_response,
)


def _ee_row(
    *,
    invoice_id=176305783,
    invoice_number="BMH1-2526-8",
    invoice_date="2026-06-28",
    merchant_c_id=342,
    warehouse_id=53313,
    total_amount=1304.73,
    currency="INR",
    items=None,
    documents=None,
):
    return {
        "order_type_key": "businessorder",
        "reference_code": "1382",
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "merchant_c_id": merchant_c_id,
        "warehouse_id": warehouse_id,
        "total_amount": total_amount,
        "invoice_currency_code": currency,
        "documents": documents,
        "invoice_documents": documents,
        "order_items": items or [
            {
                "sku": "FOO-001",
                "item_quantity": 3,
                "selling_price": "1304.73",
                "tax_rate": 18,
                "breakup_types": {
                    "Item Amount Excluding Tax": 1105.7034,
                    "Item Amount CGST": 99.5133,
                    "Item Amount SGST": 99.5133,
                },
            }
        ],
    }


def _fake_map(name="ECS-B2B-SAL-ORD-XYZ", sales_order="SAL-ORD-XYZ", company="Smoke Test Co", sales_invoice=None):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.company = company
    m.get = lambda field, default=None: {"sales_invoice": sales_invoice}.get(field, default)
    return m


class TestVariancePct(unittest.TestCase):
    def test_zero_when_totals_match(self):
        self.assertEqual(_variance_pct(100, 100), 0.0)

    def test_positive_when_si_exceeds_ee(self):
        self.assertAlmostEqual(_variance_pct(105, 100), 5.0)

    def test_negative_when_si_below_ee(self):
        self.assertAlmostEqual(_variance_pct(95, 100), -5.0)

    def test_zero_when_ee_total_zero(self):
        # Defensive — don't divide by zero
        self.assertEqual(_variance_pct(100, 0), 0.0)


class TestResolvePdfUrl(unittest.TestCase):
    def test_documents_block_preferred(self):
        row = {"documents": {"easyecom_invoice": "https://ee.example.com/a.pdf"}}
        self.assertEqual(_resolve_pdf_url(row), "https://ee.example.com/a.pdf")

    def test_invoice_documents_fallback(self):
        row = {"invoice_documents": {"easyecom_invoice": "https://ee.example.com/b.pdf"}}
        self.assertEqual(_resolve_pdf_url(row), "https://ee.example.com/b.pdf")

    def test_returns_none_when_both_missing(self):
        self.assertIsNone(_resolve_pdf_url({"documents": None}))
        self.assertIsNone(_resolve_pdf_url({}))


class TestParsePostingDate(unittest.TestCase):
    def test_parses_explicit_date(self):
        from datetime import date
        result = _parse_posting_date({"invoice_date": "2026-06-28"})
        self.assertEqual(result, str(date(2026, 6, 28)))

    def test_falls_back_to_today_when_empty(self):
        from frappe.utils import today
        result = _parse_posting_date({"invoice_date": ""})
        self.assertEqual(result, today())

    def test_falls_back_to_today_when_unparsable(self):
        from frappe.utils import today
        result = _parse_posting_date({"invoice_date": "garbage-string"})
        self.assertEqual(result, today())


class TestResolveLineItems(unittest.TestCase):
    def test_resolves_sku_via_item_map(self):
        row = _ee_row()
        with patch("frappe.db.get_value") as gv:
            # First call: Item Map lookup; second call: Item HSN
            gv.side_effect = ["ITEM-FOO-001", "39241090"]
            result = _resolve_line_items(row)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["item_code"], "ITEM-FOO-001")
        self.assertEqual(result[0]["qty"], 3)
        # 1105.7034 / 3 = 368.5678 → rounded 368.57
        self.assertEqual(result[0]["rate"], 368.57)
        self.assertEqual(result[0]["gst_hsn_code"], "39241090")

    def test_raises_when_sku_has_no_item_map(self):
        row = _ee_row()
        with patch("frappe.db.get_value", return_value=None):
            with self.assertRaises(InvoiceMirrorError) as ctx:
                _resolve_line_items(row)
            self.assertIn("FOO-001", str(ctx.exception))

    def test_raises_on_empty_items(self):
        with self.assertRaises(InvoiceMirrorError):
            _resolve_line_items({"order_items": []})

    def test_skips_zero_qty_lines(self):
        row = _ee_row(items=[
            {"sku": "FOO-001", "item_quantity": 0, "breakup_types": {}},
        ])
        with patch("frappe.db.get_value", return_value="ITEM-FOO-001"):
            result = _resolve_line_items(row)
        self.assertEqual(result, [])

    def test_falls_back_to_selling_price_when_no_breakup(self):
        """Defensive: some EE responses may omit breakup_types per line."""
        row = _ee_row(items=[
            {
                "sku": "FOO-001",
                "item_quantity": 3,
                "selling_price": "1304.73",
                "tax_rate": 18,
                # No breakup_types
            },
        ])
        with patch("frappe.db.get_value") as gv:
            gv.side_effect = ["ITEM-FOO-001", "39241090"]
            result = _resolve_line_items(row)
        # 1304.73 / 3 = 434.91 gross; / 1.18 = 368.57 net
        self.assertEqual(result[0]["rate"], 368.57)


class TestResolveWarehouse(unittest.TestCase):
    def test_resolves_via_ee_company_id(self):
        with patch("frappe.db.get_value", return_value="Mumbai WH - STC"):
            result = _resolve_warehouse({"warehouse_id": 53313})
        self.assertEqual(result, "Mumbai WH - STC")

    def test_returns_none_when_no_warehouse_id(self):
        self.assertIsNone(_resolve_warehouse({}))


class TestMirrorSiFromEeResponse(unittest.TestCase):
    """The headline integration of resolution + insertion + variance."""

    def test_creates_si_when_not_already_mirrored(self):
        row = _ee_row()
        map_doc = _fake_map()

        fake_si = MagicMock()
        fake_si.name = "ACC-SINV-2026-00001"
        fake_si.grand_total = 1304.73  # exact match, no variance

        with (
            patch("frappe.db.get_value") as gv,
            patch("frappe.new_doc", return_value=fake_si),
        ):
            # get_value calls: existing SI check (None), customer lookup,
            # then in _resolve_line_items: item_code, hsn, then warehouse
            gv.side_effect = [
                None,                  # no existing SI
                "Customer Acme Ltd",   # customer resolution
                "ITEM-FOO-001",        # item map
                "39241090",            # item hsn
                "Mumbai WH - STC",     # warehouse
            ]
            result = mirror_si_from_ee_response(map_doc=map_doc, ee_row=row)

        self.assertEqual(result["operation"], "created")
        self.assertEqual(result["sales_invoice"], "ACC-SINV-2026-00001")
        self.assertAlmostEqual(result["variance_pct"], 0.0)
        # Back-refs set on SI
        self.assertEqual(fake_si.ecs_easyecom_invoice_id, "176305783")
        self.assertEqual(fake_si.ecs_easyecom_invoice_number, "BMH1-2526-8")
        self.assertEqual(fake_si.ecs_easyecom_b2b_order_map, map_doc.name)
        fake_si.insert.assert_called_once()

    def test_returns_existing_si_when_already_mirrored(self):
        """Idempotency: re-run with same invoice_id returns the existing SI
        and skips insert."""
        row = _ee_row()
        map_doc = _fake_map()

        with patch("frappe.db.get_value") as gv:
            gv.side_effect = [
                "ACC-SINV-EXISTING",   # existing SI lookup hit
                1304.73,                # existing SI grand_total
            ]
            result = mirror_si_from_ee_response(map_doc=map_doc, ee_row=row)

        self.assertEqual(result["operation"], "already_exists")
        self.assertEqual(result["sales_invoice"], "ACC-SINV-EXISTING")

    def test_raises_variance_exception_when_si_diverges_over_threshold(self):
        """SI total 1500 vs EE 1304 = +15% variance, well above 1%."""
        row = _ee_row()
        map_doc = _fake_map()

        fake_si = MagicMock()
        fake_si.name = "ACC-SINV-2026-00002"
        fake_si.grand_total = 1500.0  # +15% variance

        with (
            patch("frappe.db.get_value") as gv,
            patch("frappe.new_doc", return_value=fake_si),
        ):
            gv.side_effect = [
                None, "Customer Acme Ltd",
                "ITEM-FOO-001", "39241090",
                "Mumbai WH - STC",
            ]
            with self.assertRaises(InvoiceMirrorVariance) as ctx:
                mirror_si_from_ee_response(map_doc=map_doc, ee_row=row)
            self.assertIn("variance", str(ctx.exception).lower())
            self.assertIn("ACC-SINV-2026-00002", str(ctx.exception))

    def test_raises_error_when_customer_map_missing(self):
        row = _ee_row()
        map_doc = _fake_map()

        with patch("frappe.db.get_value") as gv:
            gv.side_effect = [
                None,   # no existing SI
                None,   # customer NOT resolved
            ]
            with self.assertRaises(InvoiceMirrorError) as ctx:
                mirror_si_from_ee_response(map_doc=map_doc, ee_row=row)
            self.assertIn("Customer Map", str(ctx.exception))
            self.assertIn("342", str(ctx.exception))  # ee_c_id surfaced

    def test_raises_error_when_no_invoice_id(self):
        row = _ee_row(invoice_id=None)
        with self.assertRaises(InvoiceMirrorError) as ctx:
            mirror_si_from_ee_response(map_doc=_fake_map(), ee_row=row)
        self.assertIn("invoice_id", str(ctx.exception))


class TestExtractIrnFields(unittest.TestCase):
    """Defensive IRN capture — see invoice_mirror.py docstring.

    We probed Thuraya getOrderDetails with multiple include_* params
    on 2026-06-28 and found no IRN in any variant. But the user
    flagged that IRN might appear in some payloads. So the mirror
    scans defensively across candidate field names — these tests
    pin the candidate set so future EE response shape variations
    are caught."""

    def test_returns_empty_when_no_irn_fields_present(self):
        """The common case as of 2026-06-28: EE doesn't return IRN."""
        result = _extract_irn_fields({
            "invoice_id": 12345,
            "invoice_number": "INV-1",
            "marketplace_invoice_num": "M-1",  # NOT an einvoice/irn field
        })
        self.assertEqual(result, {})

    def test_captures_top_level_irn(self):
        result = _extract_irn_fields({
            "irn": "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4",
        })
        self.assertEqual(result["irn"], "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4")

    def test_captures_top_level_ack_fields(self):
        result = _extract_irn_fields({
            "ack_no": "162310987654321",
            "ack_dt": "2026-03-25T11:46:31Z",
        })
        self.assertEqual(result["ack_no"], "162310987654321")
        self.assertEqual(result["ack_dt"], "2026-03-25T11:46:31Z")

    def test_captures_pascal_case_variants(self):
        """Different EE API versions sometimes use AckNo/AckDt PascalCase."""
        result = _extract_irn_fields({
            "AckNo": "162310987654321",
            "AckDt": "2026-03-25T11:46:31Z",
            "IRN": "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4",
        })
        self.assertEqual(result["ack_no"], "162310987654321")
        self.assertEqual(result["irn"], "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4")

    def test_captures_from_nested_einvoice_block(self):
        """EE might put e-invoice fields inside a documents/einvoice block."""
        result = _extract_irn_fields({
            "invoice_id": 12345,
            "documents": {
                "easyecom_invoice": "https://ee.example.com/inv.pdf",
                "irn": "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4",
                "ack_no": "162310987654321",
            },
        })
        self.assertEqual(result["irn"], "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4")
        self.assertEqual(result["ack_no"], "162310987654321")

    def test_top_level_wins_over_nested(self):
        """If IRN is at both top level and nested, top wins (auth source)."""
        result = _extract_irn_fields({
            "irn": "TOP-LEVEL-IRN",
            "documents": {"irn": "NESTED-IRN"},
        })
        self.assertEqual(result["irn"], "TOP-LEVEL-IRN")

    def test_skips_empty_or_null_values(self):
        """irn=None should NOT land on the SI as None — skip it."""
        result = _extract_irn_fields({"irn": None, "ack_no": ""})
        self.assertEqual(result, {})

    def test_captures_signed_qr_code(self):
        result = _extract_irn_fields({
            "signed_qr_code": "base64-encoded-qr-string-here",
        })
        self.assertEqual(result["signed_qr_code"], "base64-encoded-qr-string-here")


if __name__ == "__main__":
    unittest.main()
