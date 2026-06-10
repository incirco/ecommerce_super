"""gh#29 Section 4.3 — `_default_rejected_warehouse_for(company)` must
honor the EasyEcom Company Settings per-Company override before falling
back to the account-level default.

§3.3.6 contract:
- EasyEcom Account.default_rejected_warehouse — global default. Field
  description says "Resolved per receiving Company".
- EasyEcom Company Settings.default_rejected_warehouse_override —
  "Optional per-Company override of the account-level GRN policy
  default (§3.3.6)".

Prior implementation ignored the `company` arg entirely and only
returned the account-level field, so an FDE configuring the override
(common when two Companies on the same Account post rejected qty into
different warehouses) found their setting silently ignored — GRN line
build hit RejectedWarehouseMissingError or routed to the wrong
warehouse.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


class TestDefaultRejectedWarehouseResolution(unittest.TestCase):
    """The resolver must consult EasyEcom Company Settings override
    first, then fall back to the Account-level default."""

    def test_company_override_wins_when_set(self) -> None:
        from ecommerce_super.easyecom.flows.grn_pull import (
            _default_rejected_warehouse_for,
        )

        # Two different doctypes return different values; the override
        # should win.
        def _fake_get_value(doctype, _filters, fieldname):
            if doctype == "EasyEcom Company Settings":
                self.assertEqual(_filters, {"company": "Co A", "enabled": 1})
                self.assertEqual(fieldname, "default_rejected_warehouse_override")
                return "WH-COA-REJECTED - CA"
            if doctype == "EasyEcom Account":
                self.assertEqual(fieldname, "default_rejected_warehouse")
                return "WH-GLOBAL-REJECTED - X"
            return None

        with patch.object(frappe.db, "get_value", side_effect=_fake_get_value):
            result = _default_rejected_warehouse_for("Co A")

        self.assertEqual(result, "WH-COA-REJECTED - CA")

    def test_account_default_used_when_no_override(self) -> None:
        from ecommerce_super.easyecom.flows.grn_pull import (
            _default_rejected_warehouse_for,
        )

        def _fake_get_value(doctype, _filters, fieldname):
            if doctype == "EasyEcom Company Settings":
                return None  # no override configured
            if doctype == "EasyEcom Account":
                return "WH-GLOBAL-REJECTED - X"
            return None

        with patch.object(frappe.db, "get_value", side_effect=_fake_get_value):
            result = _default_rejected_warehouse_for("Co A")

        self.assertEqual(result, "WH-GLOBAL-REJECTED - X")

    def test_returns_none_when_neither_configured(self) -> None:
        from ecommerce_super.easyecom.flows.grn_pull import (
            _default_rejected_warehouse_for,
        )

        with patch.object(frappe.db, "get_value", return_value=None):
            result = _default_rejected_warehouse_for("Co A")

        self.assertIsNone(result)

    def test_falsy_company_skips_override_lookup(self) -> None:
        """If `company` is empty / None, we cannot resolve a Company
        Settings row — go directly to the Account default."""
        from ecommerce_super.easyecom.flows.grn_pull import (
            _default_rejected_warehouse_for,
        )

        calls: list[tuple] = []

        def _fake_get_value(doctype, _filters, fieldname):
            calls.append((doctype, fieldname))
            if doctype == "EasyEcom Account":
                return "WH-GLOBAL"
            return None

        with patch.object(frappe.db, "get_value", side_effect=_fake_get_value):
            result = _default_rejected_warehouse_for("")

        self.assertEqual(result, "WH-GLOBAL")
        # No Company Settings query when company is empty.
        self.assertNotIn(
            ("EasyEcom Company Settings", "default_rejected_warehouse_override"),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
