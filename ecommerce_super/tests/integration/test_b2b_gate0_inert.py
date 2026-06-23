"""§11 Stage 2 — Gate 0 inert path.

When SO.set_warehouse is empty or maps to no Live EE Location, the
integration must be silently inert: SO submits normally, no Map row,
no queue job, no Sync Record.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.gating import (
    is_section_11_gated,
)
from ecommerce_super.easyecom.flows.b2b_sales.push import (
    on_submit_push,
    validate_pre_push,
)


class TestGate0Inert(unittest.TestCase):
    def test_empty_set_warehouse_is_not_gated(self) -> None:
        so = MagicMock()
        so.set_warehouse = None
        self.assertFalse(is_section_11_gated(so))

    def test_warehouse_with_no_ee_location_is_not_gated(self) -> None:
        so = MagicMock()
        so.set_warehouse = "Non-EE-Warehouse - TC"
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gating.get_ee_location_for_warehouse",
            return_value=None,
        ):
            self.assertFalse(is_section_11_gated(so))

    def test_warehouse_with_live_ee_location_is_gated(self) -> None:
        so = MagicMock()
        so.set_warehouse = "EE-Mapped-WH"
        location = MagicMock()
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gating.get_ee_location_for_warehouse",
            return_value=location,
        ):
            self.assertTrue(is_section_11_gated(so))


class TestValidatePrePushInertOnNonGated(unittest.TestCase):
    def test_returns_silently_on_non_sales_order(self) -> None:
        doc = MagicMock()
        doc.doctype = "Purchase Order"
        validate_pre_push(doc)  # no throw

    def test_returns_silently_on_empty_set_warehouse(self) -> None:
        doc = MagicMock()
        doc.doctype = "Sales Order"
        doc.set_warehouse = None
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gating.get_ee_location_for_warehouse",
            return_value=None,
        ):
            validate_pre_push(doc)  # no throw, no precondition walk

    def test_returns_silently_on_non_ee_warehouse(self) -> None:
        doc = MagicMock()
        doc.doctype = "Sales Order"
        doc.set_warehouse = "Other - TC"
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gating.get_ee_location_for_warehouse",
            return_value=None,
        ):
            validate_pre_push(doc)


class TestOnSubmitPushInertOnNonGated(unittest.TestCase):
    def test_does_not_enqueue_when_non_gated(self) -> None:
        doc = MagicMock()
        doc.doctype = "Sales Order"
        doc.set_warehouse = None
        enqueue = MagicMock()
        with (
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gating.get_ee_location_for_warehouse",
                return_value=None,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.push.enqueue_easyecom_job",
                enqueue,
            ),
        ):
            on_submit_push(doc)
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
