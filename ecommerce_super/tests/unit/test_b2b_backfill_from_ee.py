"""Unit tests for the generic B2B backfill flow.

Coverage:
  1. _as_bool — HTTP form receives strings, must coerce
  2. _resolve_customer — GSTIN → Customer, URP/blank/NA all return None
  3. _resolve_line_items — SKU → Item Map, blank/unmapped SKUs skipped
  4. _apply_resume_filter — resume-from cursor works correctly
  5. _extract_rows — payload shape handling (list vs dict.data vs
     dict.data.orders)
  6. _bump_counter — created_sis increments both counters
  7. _push_suppressed — flag set + restored around the block
  8. _si_exists_for_ee_invoice_id — idempotency lookup
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2b_sales import (
    backfill_from_ee as mod,
)


# ============================================================
# _as_bool
# ============================================================


class TestAsBool(unittest.TestCase):

    def test_python_bool_passthrough(self):
        self.assertTrue(mod._as_bool(True))
        self.assertFalse(mod._as_bool(False))

    def test_http_string_true(self):
        """HTTP form values arrive as strings 'true' / 'false'."""
        for v in ("true", "True", "TRUE", "1", "yes", "Yes"):
            self.assertTrue(mod._as_bool(v), f"expected True for {v!r}")

    def test_http_string_false(self):
        for v in ("false", "0", "no", "", "anything-else"):
            self.assertFalse(mod._as_bool(v), f"expected False for {v!r}")


# ============================================================
# _resolve_customer
# ============================================================


class TestResolveCustomer(unittest.TestCase):

    @patch.object(mod.frappe.db, "get_value", return_value="ACME Corp")
    def test_valid_gstin_returns_customer(self, _mock):
        ma = MagicMock()
        c = mod._resolve_customer(
            {"buyer_gst": "27AABCU9603R1ZM"}, ma,
        )
        self.assertEqual(c, "ACME Corp")

    def test_urp_returns_none(self):
        for v in ("URP", "urp", "NA", "N/A", "", None):
            c = mod._resolve_customer({"buyer_gst": v}, MagicMock())
            self.assertIsNone(c, f"URP-like {v!r} should return None")

    @patch.object(mod.frappe.db, "get_value", return_value=None)
    def test_unknown_gstin_returns_none(self, _mock):
        """GSTIN not on file — customer_pull didn't create it. Backfill
        will report skipped_no_customer for this invoice."""
        c = mod._resolve_customer(
            {"buyer_gst": "27FAKEGSTIN0000"}, MagicMock(),
        )
        self.assertIsNone(c)

    def test_whitespace_stripped(self):
        """EE payload sometimes has trailing spaces on GSTIN."""
        with patch.object(mod.frappe.db, "get_value", return_value="X"):
            c = mod._resolve_customer(
                {"buyer_gst": "  27AABCU9603R1ZM  "}, MagicMock(),
            )
            self.assertEqual(c, "X")


# ============================================================
# _resolve_line_items
# ============================================================


class TestResolveLineItems(unittest.TestCase):

    @patch.object(mod.frappe.db, "get_value")
    def test_maps_sku_via_item_map(self, mock_gv):
        mock_gv.return_value = "ITEM-Skinq-100ml"
        items = mod._resolve_line_items({
            "suborders": [
                {"sku": "8906133380823N", "item_quantity": 200,
                 "selling_price": 168000},
            ],
        })
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_code"], "ITEM-Skinq-100ml")
        self.assertEqual(items[0]["qty"], 200)
        self.assertEqual(items[0]["rate"], 840)  # 168000 / 200

    @patch.object(mod.frappe.db, "get_value", return_value=None)
    def test_unmapped_sku_is_skipped(self, _mock):
        """SKU has no Item Map row — skip. If ALL SKUs unmap, the
        caller returns skipped_no_items."""
        items = mod._resolve_line_items({
            "suborders": [
                {"sku": "UNKNOWN-SKU", "item_quantity": 1, "selling_price": 100},
            ],
        })
        self.assertEqual(items, [])

    @patch.object(mod.frappe.db, "get_value", return_value="ITEM-X")
    def test_zero_qty_skipped(self, _mock):
        items = mod._resolve_line_items({
            "suborders": [
                {"sku": "X", "item_quantity": 0, "selling_price": 100},
            ],
        })
        self.assertEqual(items, [])

    @patch.object(mod.frappe.db, "get_value", return_value="ITEM-X")
    def test_reads_order_items_key_alternate(self, _mock):
        """getOrderDetails uses `order_items`, getAllOrders uses
        `suborders`. Both paths must work."""
        items = mod._resolve_line_items({
            "order_items": [
                {"sku": "X", "item_quantity": 1, "selling_price": 50},
            ],
        })
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["rate"], 50)

    def test_empty_row_returns_empty_list(self):
        items = mod._resolve_line_items({"suborders": []})
        self.assertEqual(items, [])

    def test_no_line_key_returns_empty(self):
        items = mod._resolve_line_items({})
        self.assertEqual(items, [])


# ============================================================
# _apply_resume_filter
# ============================================================


class TestApplyResumeFilter(unittest.TestCase):

    def test_resumes_after_named_invoice_id(self):
        inv = [
            {"invoice_id": 1}, {"invoice_id": 2},
            {"invoice_id": 3}, {"invoice_id": 4},
        ]
        r = mod._apply_resume_filter(inv, "2")
        self.assertEqual([x["invoice_id"] for x in r], [3, 4])

    def test_resume_id_not_found_returns_all(self):
        """Defensive — if the caller passes a resume_id not in the
        batch, don't accidentally skip everything. Return the full
        list so ops sees the mismatch in counts."""
        inv = [{"invoice_id": 1}, {"invoice_id": 2}]
        r = mod._apply_resume_filter(inv, "999")
        self.assertEqual(len(r), 2)

    def test_resume_id_stringified(self):
        """invoice_id may be int in payload; user passes str."""
        inv = [{"invoice_id": 100}, {"invoice_id": 200}]
        r = mod._apply_resume_filter(inv, "100")
        self.assertEqual([x["invoice_id"] for x in r], [200])


# ============================================================
# _extract_rows
# ============================================================


class TestExtractRows(unittest.TestCase):

    def test_list_at_top_of_data(self):
        rows = mod._extract_rows({"data": [{"invoice_id": 1}]})
        self.assertEqual(len(rows), 1)

    def test_nested_orders_key(self):
        rows = mod._extract_rows({
            "data": {"orders": [{"invoice_id": 1}, {"invoice_id": 2}]},
        })
        self.assertEqual(len(rows), 2)

    def test_nested_rows_key(self):
        rows = mod._extract_rows({"data": {"rows": [{"x": 1}]}})
        self.assertEqual(len(rows), 1)

    def test_non_dict_response_returns_empty(self):
        self.assertEqual(mod._extract_rows(None), [])
        self.assertEqual(mod._extract_rows("string"), [])
        self.assertEqual(mod._extract_rows([]), [])


# ============================================================
# _bump_counter
# ============================================================


class TestBumpCounter(unittest.TestCase):

    def test_created_sis_bumps_both_so_and_si(self):
        """The typical happy-path outcome creates BOTH an SO and an SI —
        counter should reflect both increments."""
        r = {"created_sos": 0, "created_sis": 0}
        mod._bump_counter(r, "created_sis")
        self.assertEqual(r["created_sos"], 1)
        self.assertEqual(r["created_sis"], 1)

    def test_other_outcomes_bump_only_that_key(self):
        r = {"created_sos": 0, "created_sis": 0, "skipped_no_customer": 0}
        mod._bump_counter(r, "skipped_no_customer")
        self.assertEqual(r["created_sos"], 0)
        self.assertEqual(r["created_sis"], 0)
        self.assertEqual(r["skipped_no_customer"], 1)

    def test_new_outcome_key_auto_initialized(self):
        r = {"created_sos": 0, "created_sis": 0}
        mod._bump_counter(r, "some_new_outcome")
        self.assertEqual(r["some_new_outcome"], 1)


# ============================================================
# _push_suppressed
# ============================================================


class TestPushSuppressed(unittest.TestCase):

    def test_flag_set_inside_and_restored_outside(self):
        # Fresh flags — flag not previously set
        mod.frappe.local.flags = MagicMock()
        mod.frappe.local.flags.get = MagicMock(return_value=None)
        with mod._push_suppressed():
            # inside the block, flag is True
            self.assertTrue(mod.frappe.local.flags.easyecom_b2b_backfill_in_flight)
        # after the block, flag reset to False (was None → False)
        self.assertFalse(mod.frappe.local.flags.easyecom_b2b_backfill_in_flight)

    def test_previously_set_flag_restored_after(self):
        """Nested / re-entrant case — prior value must be restored,
        not blindly cleared to False."""
        mod.frappe.local.flags = MagicMock()
        mod.frappe.local.flags.get = MagicMock(return_value=True)
        with mod._push_suppressed():
            self.assertTrue(mod.frappe.local.flags.easyecom_b2b_backfill_in_flight)
        # was True before → restored to True after (not False)
        self.assertTrue(mod.frappe.local.flags.easyecom_b2b_backfill_in_flight)


# ============================================================
# _si_exists_for_ee_invoice_id
# ============================================================


class TestSiExistsForEeInvoiceId(unittest.TestCase):

    @patch.object(mod.frappe.db, "exists", return_value="SINV-2026-00042")
    def test_returns_true_when_si_exists(self, _mock):
        self.assertTrue(mod._si_exists_for_ee_invoice_id("687108535"))

    @patch.object(mod.frappe.db, "exists", return_value=None)
    def test_returns_false_when_absent(self, _mock):
        self.assertFalse(mod._si_exists_for_ee_invoice_id("999"))

    @patch.object(mod.frappe.db, "exists")
    def test_uses_ecs_easyecom_invoice_id_field(self, mock_exists):
        """Idempotency contract — must match on the exact field the
        invoice_builder/mirror stamps."""
        mock_exists.return_value = None
        mod._si_exists_for_ee_invoice_id("500")
        # Frappe's exists("Sales Invoice", {fieldname: value})
        args, _ = mock_exists.call_args
        self.assertEqual(args[0], "Sales Invoice")
        self.assertEqual(args[1], {"ecs_easyecom_invoice_id": "500"})


if __name__ == "__main__":
    unittest.main()
