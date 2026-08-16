"""Unit tests for §12 B2C pending-manifest sweeper branching logic.

Covers the four terminal transitions the sweeper implements:

  1. Manifest arrived (Shipped) → submit SI
  2. Manifest arrived + Returned → submit SI + create paired Credit Note
  3. Cancelled + Unregistered → submit SI + immediately cancel
  4. Cancelled + Registered Regular → submit SI + create paired Credit Note

Plus the two no-op cases:

  5. EE returns nothing → still_pending (retry next cycle)
  6. EE state still transient (Confirmed / Manifest Scanned without
     manifest_date) → still_pending

The sweeper's failure isolation (one bad SI never fails the batch) is
covered by the top-level sweep_pending_manifest_sis test — the branch
handlers just raise; the outer loop catches.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2c_sales import (
    pending_manifest_sweeper as mod,
)


def _row(**overrides) -> dict:
    """Minimal pending-shipment row shape (as returned by
    _find_pending_shipments), with sensible defaults."""
    base = {
        "sales_invoice": "SI-2026-00001",
        "gst_category": "Unregistered",
        "company": "Puresta Lifestyle Private Limited",
        "mp_order_map": "MOM-2026-00001",
        "marketplace_account": "ECS-MA-Puresta-Shopify",
        "marketplace_order_id": "SQ-440390821",
        "shipment_row": "shipment-row-name-1",
        "ee_invoice_id": "687108535",
        "invoice_number": "BHR52627-489",
    }
    base.update(overrides)
    return base


# ============================================================
# Branch: manifested (Shipped)
# ============================================================


class TestHandleManifestedShipped(unittest.TestCase):

    @patch.object(mod, "_update_shipment_manifest_date")
    @patch.object(mod, "frappe")
    def test_shipped_submits_si(self, mock_frappe, mock_update_ship):
        si = MagicMock()
        si.name = "SI-2026-00001"
        mock_frappe.get_doc.return_value = si

        outcome = mod._handle_manifested(
            row=_row(),
            ee_row={
                "order_status": "Shipped",
                "manifest_date": "2026-08-16 14:30:00",
            },
            status="Shipped",
            manifest_date="2026-08-16 14:30:00",
            dry_run=False,
        )

        self.assertEqual(outcome, "submitted")
        # SI submitted exactly once
        si.submit.assert_called_once_with()
        # Shipment child manifest_date updated
        mock_update_ship.assert_called_once_with(
            "shipment-row-name-1", "2026-08-16 14:30:00"
        )

    @patch.object(mod, "_update_shipment_manifest_date")
    @patch.object(mod, "frappe")
    def test_dry_run_shipped_does_not_submit(self, mock_frappe, mock_update_ship):
        outcome = mod._handle_manifested(
            row=_row(),
            ee_row={
                "order_status": "Shipped",
                "manifest_date": "2026-08-16 14:30:00",
            },
            status="Shipped",
            manifest_date="2026-08-16 14:30:00",
            dry_run=True,
        )
        self.assertEqual(outcome, "submitted")
        # Zero side effects in dry_run
        mock_frappe.get_doc.assert_not_called()
        mock_update_ship.assert_not_called()


# ============================================================
# Branch: manifested + Returned → SI + Credit Note
# ============================================================


class TestHandleManifestedReturned(unittest.TestCase):

    @patch.object(mod, "_create_paired_credit_note")
    @patch.object(mod, "_update_shipment_manifest_date")
    @patch.object(mod, "frappe")
    def test_returned_submits_then_creates_cn(
        self, mock_frappe, mock_update_ship, mock_cn
    ):
        si = MagicMock()
        si.name = "SI-2026-00042"
        mock_frappe.get_doc.return_value = si

        ee_row = {
            "order_status": "Returned",
            "manifest_date": "2026-08-14 10:00:00",
            "shipping_last_update_date": "2026-08-16 09:00:00",
        }
        outcome = mod._handle_manifested(
            row=_row(sales_invoice="SI-2026-00042"),
            ee_row=ee_row,
            status="Returned",
            manifest_date="2026-08-14 10:00:00",
            dry_run=False,
        )

        self.assertEqual(outcome, "credit_note")
        si.submit.assert_called_once_with()
        # CN created with the same EE row (payload) — CN builder derives
        # posting_date from shipping_last_update_date.
        mock_cn.assert_called_once_with(si, ee_row)


# ============================================================
# Branch: cancelled — Unregistered (URP)
# ============================================================


class TestHandleCancelledUnregistered(unittest.TestCase):

    @patch.object(mod, "_create_paired_credit_note")
    @patch.object(mod, "frappe")
    def test_urp_submits_then_cancels(self, mock_frappe, mock_cn):
        """URP + cancelled putaway → submit then cancel. No CN.
        Cancelled SI (docstatus=2) is excluded from GSTR-1 by India
        Compliance's docstatus=1 filter."""
        si = MagicMock()
        si.name = "SI-2026-00099"
        mock_frappe.get_doc.return_value = si

        outcome = mod._handle_cancelled(
            row=_row(gst_category="Unregistered"),
            ee_row={"order_status": "Cancelled"},
            dry_run=False,
        )

        self.assertEqual(outcome, "cancelled")
        # Submit then cancel in that order
        si.submit.assert_called_once_with()
        si.cancel.assert_called_once_with()
        # No CN — buyer had no ITC to reverse
        mock_cn.assert_not_called()

    @patch.object(mod, "_create_paired_credit_note")
    @patch.object(mod, "frappe")
    def test_blank_gst_category_treated_as_unregistered(
        self, mock_frappe, mock_cn
    ):
        """Defensive — some historical SIs have gst_category blank
        rather than 'Unregistered'. Treat blank as URP (same cancel-
        without-CN treatment). India Compliance sets 'Unregistered'
        during b2c invoice_builder now (post PR #263), so this is
        legacy safety."""
        si = MagicMock()
        mock_frappe.get_doc.return_value = si

        outcome = mod._handle_cancelled(
            row=_row(gst_category=""),
            ee_row={"order_status": "Cancelled"},
            dry_run=False,
        )

        self.assertEqual(outcome, "cancelled")
        si.submit.assert_called_once()
        si.cancel.assert_called_once()
        mock_cn.assert_not_called()


# ============================================================
# Branch: cancelled — Registered Regular (buyer has GSTIN)
# ============================================================


class TestHandleCancelledRegistered(unittest.TestCase):

    @patch.object(mod, "_create_paired_credit_note")
    @patch.object(mod, "frappe")
    def test_registered_submits_then_creates_cn(
        self, mock_frappe, mock_cn
    ):
        """GSTIN buyer + cancelled putaway → submit + CN. Both appear
        in GSTR-1: invoice under B2B sales, CN under credit notes.
        Buyer can use the CN to reverse their ITC claim."""
        si = MagicMock()
        si.name = "SI-2026-B2B-001"
        mock_frappe.get_doc.return_value = si
        ee_row = {"order_status": "Cancelled"}

        outcome = mod._handle_cancelled(
            row=_row(gst_category="Registered Regular"),
            ee_row=ee_row,
            dry_run=False,
        )

        self.assertEqual(outcome, "credit_note")
        # Submit but do NOT cancel — leave submitted for GSTR-1
        si.submit.assert_called_once()
        si.cancel.assert_not_called()
        # Credit Note created
        mock_cn.assert_called_once_with(si, ee_row)


# ============================================================
# Top-level dispatch: _process_one_pending
# ============================================================


class TestProcessOnePending(unittest.TestCase):

    @patch.object(mod, "_fetch_ee_order")
    def test_ee_returns_nothing_stays_pending(self, mock_fetch):
        """EE fetch returned None (network / auth blip) → sweeper
        doesn't touch the SI, retries next cycle."""
        mock_fetch.return_value = None
        outcome = mod._process_one_pending(_row(), dry_run=False)
        self.assertEqual(outcome, "still_pending")

    @patch.object(mod, "_fetch_ee_order")
    def test_transient_confirmed_stays_pending(self, mock_fetch):
        """EE says Confirmed but no manifest_date yet → in-progress,
        will pick up next cycle."""
        mock_fetch.return_value = {
            "order_status": "Confirmed",
            "manifest_date": "",
        }
        outcome = mod._process_one_pending(_row(), dry_run=False)
        self.assertEqual(outcome, "still_pending")

    @patch.object(mod, "_fetch_ee_order")
    def test_manifest_scanned_no_date_stays_pending(self, mock_fetch):
        """Manifest Scanned = mid-scan transient state; manifest_date
        may lag by seconds. Wait it out."""
        mock_fetch.return_value = {
            "order_status": "Manifest Scanned",
            "manifest_date": "",
        }
        outcome = mod._process_one_pending(_row(), dry_run=False)
        self.assertEqual(outcome, "still_pending")

    @patch.object(mod, "_handle_manifested")
    @patch.object(mod, "_fetch_ee_order")
    def test_dispatches_shipped_to_manifested_handler(
        self, mock_fetch, mock_handle
    ):
        mock_fetch.return_value = {
            "order_status": "Shipped",
            "manifest_date": "2026-08-16 14:30:00",
        }
        mock_handle.return_value = "submitted"
        outcome = mod._process_one_pending(_row(), dry_run=False)
        self.assertEqual(outcome, "submitted")
        mock_handle.assert_called_once()

    @patch.object(mod, "_handle_cancelled")
    @patch.object(mod, "_fetch_ee_order")
    def test_dispatches_cancelled_to_cancelled_handler(
        self, mock_fetch, mock_handle
    ):
        """Cancelled path taken regardless of manifest_date — cancelled
        putaway (no manifest) and post-manifest cancel both go through
        the GST-branched cancel handler."""
        mock_fetch.return_value = {
            "order_status": "Cancelled",
            "manifest_date": "",
        }
        mock_handle.return_value = "cancelled"
        outcome = mod._process_one_pending(_row(), dry_run=False)
        self.assertEqual(outcome, "cancelled")
        mock_handle.assert_called_once()


# ============================================================
# Top-level sweeper: failure isolation
# ============================================================


class TestSweepPendingManifestSisIsolation(unittest.TestCase):

    @patch.object(mod, "_process_one_pending")
    @patch.object(mod, "_find_pending_shipments")
    def test_one_failure_does_not_break_batch(
        self, mock_find, mock_process
    ):
        """Sweeper's outer loop must isolate per-row failures — one
        raising row shouldn't skip the rest of the batch."""
        mock_find.return_value = [
            _row(sales_invoice="SI-1"),
            _row(sales_invoice="SI-BAD"),
            _row(sales_invoice="SI-3"),
        ]

        def process(row, dry_run):
            if row["sales_invoice"] == "SI-BAD":
                raise RuntimeError("simulated failure")
            return "submitted"

        mock_process.side_effect = process
        result = mod.sweep_pending_manifest_sis()

        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["errors"], 1)


if __name__ == "__main__":
    unittest.main()
