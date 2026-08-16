"""Unit tests for the Marketplace Order Map upsert helpers in
b2c_sales/invoice_builder.py.

Covers three concerns:
  1. `_derive_invoice_status_event_type` — status → event_type mapping
     (Sold / Sold(cancelled) / Returned / blank). Matches the EE tax
     export Invoice Status column semantics verified in Aug 2026
     Puresta reconciliation.

  2. `_build_shipment_row` — safe extraction of every EE field into the
     child dict, with defensible defaults when fields are missing.

  3. `_upsert_marketplace_order_map` — idempotency at both parent-key
     level (composite: marketplace + marketplace_order_id + company)
     and child level (skip if a Shipment row for this SI already
     exists). Split-shipment: appends rather than throws.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
    _build_shipment_row,
    _derive_invoice_status_event_type,
    _upsert_marketplace_order_map,
)


def _account(
    *,
    name="ECS-MA-Puresta-Shopify",
    marketplace="Shopify",
    company="Puresta Lifestyle Private Limited",
):
    return SimpleNamespace(
        name=name,
        marketplace=marketplace,
        company=company,
    )


def _si(name="SI-2026-00001"):
    return SimpleNamespace(name=name)


# ============================================================
# _derive_invoice_status_event_type
# ============================================================


class TestDeriveInvoiceStatusEventType(unittest.TestCase):

    def test_shipped_with_invoice_number_is_sold(self):
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Shipped",
                "invoice_number": "BHR52627-489",
            }),
            "Sold",
        )

    def test_confirmed_with_invoice_number_is_sold(self):
        # EE Aug 2026: 156 lines / 28 distinct invoice_numbers with
        # Order Status = Confirmed AND Invoice Status = Sold in the CSV.
        # Transient but legitimately Sold-classified.
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Confirmed",
                "invoice_number": "CHR62627-10245",
            }),
            "Sold",
        )

    def test_manifest_scanned_is_sold(self):
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Manifest Scanned",
                "invoice_number": "CHR62627-10300",
            }),
            "Sold",
        )

    def test_returned_maps_to_returned(self):
        """We surface Returned separately even though EE's Invoice
        Status lumps it into Sold — recon needs Forward vs Reverse
        distinction per RECON_SPEC §6.4."""
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Returned",
                "invoice_number": "BHR52627-500",
            }),
            "Returned",
        )

    def test_cancelled_with_invoice_number_is_sold_cancelled(self):
        # Cancelled putaway: invoice was raised then cancelled.
        # Distinct from cancelled-before-invoice (see next test).
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Cancelled",
                "invoice_number": "BHR52627-489",
            }),
            "Sold(cancelled)",
        )

    def test_cancelled_without_invoice_number_is_blank(self):
        # Cancelled BEFORE invoicing — never had an invoice_number,
        # doesn't appear in EE tax export. Empty event_type means
        # "not in scope of GST reporting."
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Cancelled",
                "invoice_number": "",
            }),
            "",
        )

    def test_shipped_without_invoice_number_is_blank(self):
        # Defensive — shouldn't happen in practice, but if EE returns
        # a shipped order with no invoice_number, don't classify it.
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "Shipped",
                "invoice_number": None,
            }),
            "",
        )

    def test_unknown_status_returns_blank(self):
        self.assertEqual(
            _derive_invoice_status_event_type({
                "order_status": "SomeFutureStatus",
                "invoice_number": "BHR52627-489",
            }),
            "",
        )


# ============================================================
# _build_shipment_row
# ============================================================


class TestBuildShipmentRow(unittest.TestCase):

    def test_full_populated_row(self):
        row = _build_shipment_row(
            si=_si("SI-2026-00042"),
            order_row={
                "invoice_number": "BHR52627-489",
                "manifest_date": "2026-07-31 13:12:48",
                "manifest_no": "MFT-001",
                "batch_id": "B-42",
                "batch_created_at": "2026-07-31 12:00:00",
                "sales_channel": "Marketplace B2C",
                "awb_number": "AWB123",
                "courier": "Delhivery",
                "invoice_currency_code": "INR",
                "total_amount": 2240.50,
                "total_tax": 340.75,
                "payment_gateway_name": "Gokwik UPI",
                "payment_gateway_transaction_number": "E26071212996HO",
                "irn": "abc123",
                "ack_no": "132628310721688",
                "ack_dt": "2026-07-31 15:00:00",
                "eway_bill_number": "342309571640",
                "eway_bill_date": "2026-07-31 15:30:00",
                "documents": {
                    "easyecom_invoice": (
                        "https://s3.example.com/inv.pdf?"
                        "request-content-type=application/force-download"
                    ),
                },
            },
            ee_invoice_id="687108535",
            event_type="Sold",
            original_sales_invoice=None,
        )
        self.assertEqual(row["sales_invoice"], "SI-2026-00042")
        self.assertEqual(row["invoice_id"], "687108535")
        self.assertEqual(row["invoice_number"], "BHR52627-489")
        self.assertEqual(row["ecs_easyecom_event_type"], "Sold")
        self.assertEqual(row["manifest_date"], "2026-07-31 13:12:48")
        self.assertEqual(row["invoice_currency_code"], "INR")
        self.assertEqual(row["total_amount_native"], 2240.50)
        self.assertEqual(row["total_tax_native"], 340.75)
        self.assertEqual(row["ack_no"], "132628310721688")
        # PDF URL had a `?request-content-type=...` query string —
        # stripped by the builder so the link opens inline in Chrome.
        self.assertEqual(
            row["easyecom_invoice_pdf_url"],
            "https://s3.example.com/inv.pdf",
        )

    def test_minimal_row_missing_optional_fields(self):
        """Only invoice_number populated — all other fields should
        gracefully default to None / defaults without raising."""
        row = _build_shipment_row(
            si=_si("SI-2026-00043"),
            order_row={"invoice_number": "SHOP-1001"},
            ee_invoice_id="500",
            event_type="Sold",
            original_sales_invoice=None,
        )
        self.assertEqual(row["invoice_number"], "SHOP-1001")
        self.assertEqual(row["invoice_currency_code"], "INR")
        self.assertEqual(row["conversion_rate"], 1)
        self.assertEqual(row["total_amount_native"], 0.0)
        self.assertIsNone(row["manifest_date"])
        self.assertIsNone(row["manifest_no"])
        self.assertIsNone(row["awb_number"])
        self.assertIsNone(row["irn"])

    def test_foreign_currency_captured(self):
        """Multi-currency case — native amounts + currency code preserved
        for audit; conversion_rate stays 1 (PR D wires ERPNext-side
        multi-currency conversion later)."""
        row = _build_shipment_row(
            si=_si(),
            order_row={
                "invoice_number": "BHR52627-489",
                "invoice_currency_code": "CAD",
                "total_amount": 2240.00,
                "total_tax": 0.0,
            },
            ee_invoice_id="687108535",
            event_type="Sold",
            original_sales_invoice=None,
        )
        self.assertEqual(row["invoice_currency_code"], "CAD")
        self.assertEqual(row["total_amount_native"], 2240.00)

    def test_credit_note_row_carries_original_si_link(self):
        row = _build_shipment_row(
            si=_si("SI-2026-00099-CN"),
            order_row={"invoice_number": "CHR62627-10245"},
            ee_invoice_id="CN-500",
            event_type="Sold(cancelled)",
            original_sales_invoice="SI-2026-00099",
        )
        self.assertEqual(row["original_sales_invoice"], "SI-2026-00099")
        self.assertEqual(row["ecs_easyecom_event_type"], "Sold(cancelled)")

    def test_foreign_currency_cn_row_auto_confirms(self):
        """Post-audit HIGH-2 fix: a Credit Note's Shipment child must
        NOT sit at awaiting_fx_confirmation. The CN inherits its rate
        from the original SI (already confirmed there); ops has
        nothing separate to verify on the CN.

        Without this behaviour, foreign-currency CNs would block the
        sweeper's cancel/return path forever."""
        row = _build_shipment_row(
            si=_si("SI-2026-00099-CN"),
            order_row={
                "invoice_number": "CHR62627-10245",
                "invoice_currency_code": "CAD",
            },
            ee_invoice_id="CN-500",
            event_type="Returned",
            original_sales_invoice="SI-2026-00099",  # CN back-ref present
        )
        # CAD → NOT INR, but the presence of original_sales_invoice
        # marks this as a CN → auto-confirm.
        self.assertEqual(row["invoice_currency_code"], "CAD")
        self.assertEqual(row["conversion_rate_confirmed"], 1)

    def test_foreign_currency_original_si_row_stays_unconfirmed(self):
        """Regression guard: a NON-CN foreign SI (original_sales_invoice
        is None) still lands unconfirmed — the sweeper's FX gate must
        still catch these for ops verification."""
        row = _build_shipment_row(
            si=_si("SI-2026-00100"),
            order_row={
                "invoice_number": "BHR52627-489",
                "invoice_currency_code": "CAD",
            },
            ee_invoice_id="687108535",
            event_type="Sold",
            original_sales_invoice=None,  # not a CN
        )
        self.assertEqual(row["invoice_currency_code"], "CAD")
        self.assertEqual(row["conversion_rate_confirmed"], 0)


# ============================================================
# _upsert_marketplace_order_map
# ============================================================


class TestUpsertMarketplaceOrderMap(unittest.TestCase):

    @patch("ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe")
    def test_creates_new_map_when_none_exists(self, mock_frappe):
        mock_frappe.db.get_value.return_value = None
        # frappe.get_doc({...}) returns a doc-like mock
        new_map = MagicMock()
        new_map.name = "MOM-2026-00001"
        new_map.shipments = []
        new_map.append = lambda field, row: new_map.shipments.append(row)
        mock_frappe.get_doc.return_value = new_map

        result = _upsert_marketplace_order_map(
            si=_si("SI-2026-00001"),
            marketplace_account=_account(),
            order_row={
                "reference_code": "SQ-440390821",
                "invoice_number": "SHOP-1001",
            },
            ee_order_id="12345",
            ee_invoice_id="500",
            event_type="Sold",
            original_sales_invoice=None,
        )

        self.assertEqual(result, "MOM-2026-00001")
        # get_doc called with a dict to build the new Map
        mock_frappe.get_doc.assert_called_once()
        call_arg = mock_frappe.get_doc.call_args[0][0]
        self.assertEqual(call_arg["doctype"], "EasyEcom Marketplace Order Map")
        self.assertEqual(call_arg["marketplace_order_id"], "SQ-440390821")
        self.assertEqual(call_arg["marketplace"], "Shopify")
        # Insert (not save) because it's a new doc
        new_map.insert.assert_called_once_with(ignore_permissions=True)
        new_map.save.assert_not_called()
        # Exactly one Shipment row appended
        self.assertEqual(len(new_map.shipments), 1)
        self.assertEqual(new_map.shipments[0]["sales_invoice"], "SI-2026-00001")

    @patch("ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe")
    def test_appends_shipment_when_map_exists_split_shipment_case(
        self, mock_frappe
    ):
        """Split-shipment: two SIs for the same marketplace_order_id.
        The second call must find the existing Map and append a new
        child row rather than creating a new Map."""
        mock_frappe.db.get_value.return_value = "MOM-2026-00001"
        existing_map = MagicMock()
        existing_map.name = "MOM-2026-00001"
        # First shipment already there from an earlier call
        existing_map.shipments = [
            SimpleNamespace(sales_invoice="SI-2026-00001"),
        ]
        existing_map.append = lambda field, row: existing_map.shipments.append(row)
        mock_frappe.get_doc.return_value = existing_map

        result = _upsert_marketplace_order_map(
            si=_si("SI-2026-00002"),  # Different SI, same marketplace order
            marketplace_account=_account(),
            order_row={
                "reference_code": "SQ-440390821",
                "invoice_number": "SHOP-1002",
            },
            ee_order_id="12345",
            ee_invoice_id="501",
            event_type="Sold",
            original_sales_invoice=None,
        )

        self.assertEqual(result, "MOM-2026-00001")
        # Save, not insert
        existing_map.save.assert_called_once_with(ignore_permissions=True)
        existing_map.insert.assert_not_called()
        # Now has two shipments
        self.assertEqual(len(existing_map.shipments), 2)

    @patch("ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe")
    def test_idempotent_when_shipment_row_already_exists(self, mock_frappe):
        """Re-running the poll for the same SI must not duplicate the
        Shipment child row. Idempotency at child level via sales_invoice
        match."""
        mock_frappe.db.get_value.return_value = "MOM-2026-00001"
        existing_map = MagicMock()
        existing_map.name = "MOM-2026-00001"
        existing_map.shipments = [
            SimpleNamespace(sales_invoice="SI-2026-00001"),
        ]
        existing_map.append = lambda field, row: existing_map.shipments.append(row)
        mock_frappe.get_doc.return_value = existing_map

        _upsert_marketplace_order_map(
            si=_si("SI-2026-00001"),  # Same SI as already in Map
            marketplace_account=_account(),
            order_row={
                "reference_code": "SQ-440390821",
                "invoice_number": "SHOP-1001",
            },
            ee_order_id="12345",
            ee_invoice_id="500",
            event_type="Sold",
            original_sales_invoice=None,
        )
        # No new child row appended — idempotent
        self.assertEqual(len(existing_map.shipments), 1)
        # But save still called to catch any parent-level updates
        existing_map.save.assert_called_once_with(ignore_permissions=True)

    @patch("ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe")
    def test_returns_none_when_reference_code_missing(self, mock_frappe):
        """Defensive — no marketplace_order_id means we can't upsert
        anything. Log a warning and no-op rather than raising."""
        result = _upsert_marketplace_order_map(
            si=_si(),
            marketplace_account=_account(),
            order_row={"invoice_number": "SHOP-1001"},  # No reference_code
            ee_order_id="12345",
            ee_invoice_id="500",
            event_type="Sold",
            original_sales_invoice=None,
        )
        self.assertIsNone(result)
        # Never queried the DB
        mock_frappe.db.get_value.assert_not_called()
        mock_frappe.get_doc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
