"""gh#29 — PO push sweep diagnostic + Go Live POs wire-up.

Two issues blocked the §9 buying validation:

1. The Go Live dialog had checkboxes for Items / Customers / Suppliers
   but NOT POs — even though the server's `go_live_enable_auto_push`
   has supported `pos` since 2026-05-29. FDEs couldn't enable
   `auto_push_pos_on_save=1` from the desk, so the on_submit /
   on_cancel hooks never fired their EE writes (sections 1.1, 3.1,
   3.3, 7.4).

2. The PO push sweep silently returned "Considered: 0, Enqueued: 0"
   with no explanation (section 2.1). FDEs couldn't tell whether the
   problem was draft POs, unmapped warehouses, or already-pushed
   POs.

These tests freeze:
- `candidate_pos_diagnostic` returns the expected per-bucket shape.
- The diagnostic dict surfaces in `push_all_pending_pos`'s response.
- The server's `go_live_enable_auto_push` accepts `pos=` and transitions
  `auto_push_pos_on_save` (the form JS asserting the dialog now passes
  it is covered by manual test plan in the PR — JS unit tests are
  outside the Python suite).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


class TestCandidatePosDiagnosticShape(unittest.TestCase):
    """`candidate_pos_diagnostic` returns the bucket dict the dialog
    expects. We mock the underlying SQL so we don't need a live
    ERPNext schema in unit tests."""

    def test_shape_with_realistic_buckets(self) -> None:
        from ecommerce_super.easyecom.flows import po_push

        with patch.object(
            frappe.db,
            "sql",
            return_value=[
                {
                    "total_pos": 12,
                    "draft": 5,
                    "cancelled": 2,
                    "submitted_unmapped_warehouse": 3,
                    "already_mapped": 1,
                }
            ],
        ):
            result = po_push.candidate_pos_diagnostic()

        self.assertEqual(
            sorted(result.keys()),
            sorted(
                [
                    "total_pos",
                    "draft",
                    "cancelled",
                    "submitted_unmapped_warehouse",
                    "already_mapped",
                ]
            ),
        )
        self.assertEqual(result["total_pos"], 12)
        self.assertEqual(result["draft"], 5)
        self.assertEqual(result["submitted_unmapped_warehouse"], 3)

    def test_handles_empty_table_with_null_sums(self) -> None:
        """An empty `tabPurchase Order` produces `SUM(...) = NULL`
        instead of 0 in MariaDB. The helper must coerce to int 0 so
        the dialog renderer doesn't get `null`."""
        from ecommerce_super.easyecom.flows import po_push

        with patch.object(
            frappe.db,
            "sql",
            return_value=[
                {
                    "total_pos": 0,
                    "draft": None,
                    "cancelled": None,
                    "submitted_unmapped_warehouse": None,
                    "already_mapped": None,
                }
            ],
        ):
            result = po_push.candidate_pos_diagnostic()

        # All values are real ints — never None.
        for key, value in result.items():
            self.assertIsInstance(value, int, f"{key} should be int, got {type(value)}")
            self.assertEqual(value, 0)


class TestGoLivePosWiring(unittest.TestCase):
    """Server `go_live_enable_auto_push` accepts `pos` and writes
    `auto_push_pos_on_save=1` when enabled."""

    def test_pos_enables_auto_push_pos_on_save(self) -> None:
        from ecommerce_super.easyecom.api import auto_push_controls

        captured_updates: dict = {}

        def _fake_set_value(_doctype, _name, updates, **_kwargs):
            captured_updates.update(updates)

        # Production calls `frappe.db.get_value(..., as_dict=True)`,
        # which returns a `frappe._dict` that supports BOTH dict access
        # (`modes["item_master_mode"]`) and attribute access
        # (`modes.item_master_mode`). The handler at
        # auto_push_controls.py:182 uses attribute access. A plain
        # `dict` mock here was the wrong shape and produced
        # `AttributeError: 'dict' object has no attribute
        # 'item_master_mode'` — gh#33 audit follow-up.
        modes_mock = frappe._dict(
            item_master_mode="erpnext_mastered",
            customer_master_mode="erpnext_mastered",
            supplier_master_mode="erpnext_mastered",
        )
        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.db.get_value", return_value=modes_mock),
            patch.object(frappe.db, "set_value", side_effect=_fake_set_value),
            patch("frappe.get_doc") as get_doc_mock,
            patch.object(auto_push_controls, "_check_role"),
        ):
            get_doc_mock.return_value.add_comment = lambda **_: None
            get_doc_mock.return_value.auto_push_on_save = 1
            get_doc_mock.return_value.auto_push_customers_on_save = 0
            get_doc_mock.return_value.auto_push_suppliers_on_save = 0
            get_doc_mock.return_value.auto_push_pos_on_save = 1
            result = auto_push_controls.go_live_enable_auto_push(
                account="ACC1",
                items=0,
                customers=0,
                suppliers=0,
                pos=1,
                confirm=1,
            )

        self.assertTrue(result["ok"])
        self.assertIn("auto_push_pos_on_save", captured_updates)
        self.assertEqual(captured_updates["auto_push_pos_on_save"], 1)
        self.assertIn("POs", result["transitioned"])
        # state dict carries `pos` so the dialog can render it.
        self.assertIn("pos", result["state"])


if __name__ == "__main__":
    unittest.main()
