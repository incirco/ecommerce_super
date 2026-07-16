"""Quick lifecycle view on B2B Order Map — the "at one place, see the
whole story" widget that renders above the field grid.

Verifies the shape + correctness of `get_lifecycle` for representative
scenarios: fresh push, EE-accepted, invoice-generated, mirrored,
minted. Also verifies the API-Call summary + permission gate.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.doctype.easyecom_b2b_order_map import (
    easyecom_b2b_order_map as mod,
)


def _mock_map(**overrides):
    """Minimal B2B Order Map doc for lifecycle inspection."""
    m = MagicMock()
    m.name = "ECS-B2B-SO-TEST"
    m.sales_order = "SO-TEST-001"
    m.module = "New B2B"
    m.status = "Pushed"
    m.ee_order_id = None
    m.ee_suborder_id = None
    m.invoice_id = None
    m.invoice_number = None
    m.sales_invoice = None
    m.creation = "2026-07-16 14:00:00"
    m.modified = "2026-07-16 14:00:00"
    m.get = lambda k, default=None: getattr(m, k, default)
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


def _mock_so_get_value(present=True, docstatus=1, grand_total=1050.0):
    """Return the value that `frappe.db.get_value("Sales Order", ...)`
    should produce for our mock SO."""
    if not present:
        return None
    return SimpleNamespace(
        creation="2026-07-16 13:55:00",
        docstatus=docstatus,
        grand_total=grand_total,
        currency="INR",
    )


def _mock_si_get_value(*, docstatus=1, grand_total=1050.0, irn=None, ewaybill=None):
    return SimpleNamespace(
        creation="2026-07-16 14:15:00",
        modified="2026-07-16 14:17:00",
        docstatus=docstatus,
        grand_total=grand_total,
        currency="INR",
        irn=irn,
        ewaybill=ewaybill,
    )


class TestGetLifecycleStageSequence(unittest.TestCase):
    """The stage list is always 6 entries in a fixed order — the widget
    renders them uniformly."""

    def _run(self, *, map_doc, so_val=None, si_val=None, api_summary=None):
        """Invoke get_lifecycle with all Frappe side-effects mocked."""

        def _exists(doctype, name=None):
            if doctype == "Sales Order":
                return so_val is not None
            if doctype == "Sales Invoice":
                return si_val is not None
            return False

        def _get_value(doctype, name=None, fields=None, **_kw):
            if doctype == "Sales Order":
                return so_val
            if doctype == "Sales Invoice":
                return si_val
            return None

        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(mod.frappe, "get_doc", return_value=map_doc),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "has_column", return_value=True),
            patch.object(
                mod, "_summarise_api_calls_for_map",
                return_value=api_summary or {"total": 0, "outbound": 0, "inbound": 0},
            ),
        ):
            return mod.get_lifecycle(map_doc.name)

    def test_always_returns_six_stages_in_fixed_order(self):
        stages = self._run(
            map_doc=_mock_map(),
            so_val=_mock_so_get_value(),
        )
        self.assertEqual(len(stages), 6)
        expected_order = [
            "SO Submitted",
            "Pushed to EE",
            "EE Accepted (IDs assigned)",
            "SI Mirrored",
            "IRN + Eway Minted",
            "API Calls",
        ]
        self.assertEqual([s["stage"] for s in stages], expected_order)

    def test_fresh_push_only_first_two_stages_ok(self):
        """Just pushed — SO submitted + Map created, everything else pending."""
        stages = self._run(
            map_doc=_mock_map(),
            so_val=_mock_so_get_value(),
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertTrue(by_stage["SO Submitted"]["ok"])
        self.assertTrue(by_stage["Pushed to EE"]["ok"])
        self.assertFalse(by_stage["EE Accepted (IDs assigned)"]["ok"])
        self.assertFalse(by_stage["SI Mirrored"]["ok"])
        self.assertFalse(by_stage["IRN + Eway Minted"]["ok"])

    def test_ee_accepted_stage_ok_when_order_id_present(self):
        stages = self._run(
            map_doc=_mock_map(ee_order_id="EE-ORD-999"),
            so_val=_mock_so_get_value(),
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertTrue(by_stage["EE Accepted (IDs assigned)"]["ok"])
        self.assertIn("EE-ORD-999", by_stage["EE Accepted (IDs assigned)"]["detail"])

    def test_ee_accepted_stage_ok_when_invoice_id_present_but_no_order_id(self):
        """Some New B2B paths land invoice_id before ee_order_id via
        polling backfill — either signal counts as 'EE Accepted'."""
        stages = self._run(
            map_doc=_mock_map(invoice_id="INV-176305783"),
            so_val=_mock_so_get_value(),
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertTrue(by_stage["EE Accepted (IDs assigned)"]["ok"])
        self.assertIn("176305783", by_stage["EE Accepted (IDs assigned)"]["detail"])

    def test_si_mirrored_stage_ok_when_sales_invoice_linked_and_exists(self):
        stages = self._run(
            map_doc=_mock_map(sales_invoice="SI-2603821"),
            so_val=_mock_so_get_value(),
            si_val=_mock_si_get_value(),
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertTrue(by_stage["SI Mirrored"]["ok"])
        self.assertEqual(by_stage["SI Mirrored"]["link_name"], "SI-2603821")

    def test_si_mirrored_stage_not_ok_when_link_present_but_si_deleted(self):
        """Map.sales_invoice is stale (SI was deleted) — treat as not
        mirrored so the FDE knows to investigate."""
        stages = self._run(
            map_doc=_mock_map(sales_invoice="SI-STALE"),
            so_val=_mock_so_get_value(),
            si_val=None,  # SI doesn't exist
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertFalse(by_stage["SI Mirrored"]["ok"])

    def test_irn_mint_reflected_when_present(self):
        stages = self._run(
            map_doc=_mock_map(sales_invoice="SI-001"),
            so_val=_mock_so_get_value(),
            si_val=_mock_si_get_value(irn="1234567890abcdef1234567890abcdef"),
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertTrue(by_stage["IRN + Eway Minted"]["ok"])
        self.assertIn("IRN", by_stage["IRN + Eway Minted"]["detail"])

    def test_eway_mint_reflected_when_present(self):
        stages = self._run(
            map_doc=_mock_map(sales_invoice="SI-001"),
            so_val=_mock_so_get_value(),
            si_val=_mock_si_get_value(ewaybill="EW-987654321"),
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertTrue(by_stage["IRN + Eway Minted"]["ok"])
        self.assertIn("Eway EW-987654321", by_stage["IRN + Eway Minted"]["detail"])

    def test_both_irn_and_eway_shown_together(self):
        stages = self._run(
            map_doc=_mock_map(sales_invoice="SI-001"),
            so_val=_mock_so_get_value(),
            si_val=_mock_si_get_value(irn="abc123", ewaybill="EW-111"),
        )
        by_stage = {s["stage"]: s for s in stages}
        detail = by_stage["IRN + Eway Minted"]["detail"]
        self.assertIn("IRN", detail)
        self.assertIn("Eway", detail)

    def test_orphaned_map_reports_missing_source_so(self):
        """The Map exists but its sales_order is stale/deleted."""
        stages = self._run(
            map_doc=_mock_map(sales_order="SO-DELETED"),
            so_val=None,  # SO doesn't exist
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertFalse(by_stage["SO Submitted"]["ok"])
        self.assertIn("orphaned", by_stage["SO Submitted"]["detail"].lower())

    def test_api_call_summary_reflects_counts(self):
        stages = self._run(
            map_doc=_mock_map(ee_order_id="EE-ORD-999"),
            so_val=_mock_so_get_value(),
            api_summary={
                "total": 5, "outbound": 3, "inbound": 2,
                "last_at": "2026-07-16 14:20:00",
                "last_endpoint": "/gettoken",
                "last_status": 200,
            },
        )
        by_stage = {s["stage"]: s for s in stages}
        api = by_stage["API Calls"]
        self.assertTrue(api["ok"])
        self.assertIn("5 total", api["detail"])
        self.assertIn("outbound", api["detail"])
        self.assertIn("/gettoken", api["detail"])

    def test_api_call_summary_zero_reports_zero(self):
        stages = self._run(
            map_doc=_mock_map(),
            so_val=_mock_so_get_value(),
            api_summary={"total": 0, "outbound": 0, "inbound": 0},
        )
        by_stage = {s["stage"]: s for s in stages}
        self.assertFalse(by_stage["API Calls"]["ok"])


class TestGetLifecyclePermissionGate(unittest.TestCase):
    def test_refuses_without_read_permission(self):
        with (
            patch.object(mod.frappe, "has_permission", return_value=False),
            self.assertRaises(Exception) as ctx,
        ):
            mod.get_lifecycle("ECS-B2B-SO-TEST")
        self.assertIn("Not permitted", str(ctx.exception))


class TestApiCallSummary(unittest.TestCase):
    """The helper is fault-tolerant — degrades to zeros on any error
    so the lifecycle widget never breaks the form even if the API
    Call log column shape is unexpected."""

    def test_no_search_anchors_returns_zeros(self):
        m = _mock_map(sales_order=None, ee_order_id=None, invoice_id=None)
        result = mod._summarise_api_calls_for_map(m)
        self.assertEqual(
            result, {"total": 0, "outbound": 0, "inbound": 0},
        )

    def test_get_all_error_degrades_gracefully(self):
        """If the API Call schema differs (site-specific customization),
        the summary returns zeros instead of throwing."""
        m = _mock_map(sales_order="SO-001")
        with patch.object(
            mod.frappe.db, "get_all",
            side_effect=Exception("column not found"),
        ):
            result = mod._summarise_api_calls_for_map(m)
        self.assertEqual(
            result, {"total": 0, "outbound": 0, "inbound": 0},
        )

    def test_counts_grouped_by_direction(self):
        m = _mock_map(sales_order="SO-001")
        rows = [
            SimpleNamespace(name="C1", direction="Outbound", endpoint="/gettoken",
                            http_status=200, creation="2026-07-16 14:03:00"),
            SimpleNamespace(name="C2", direction="Outbound", endpoint="/createOrder",
                            http_status=200, creation="2026-07-16 14:03:01"),
            SimpleNamespace(name="C3", direction="Inbound", endpoint="/einvoice/update",
                            http_status=200, creation="2026-07-16 14:15:00"),
        ]
        with patch.object(mod.frappe.db, "get_all", return_value=rows):
            result = mod._summarise_api_calls_for_map(m)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["outbound"], 2)
        self.assertEqual(result["inbound"], 1)
        # last_at reflects the first-returned row (query orders desc)
        self.assertEqual(result["last_endpoint"], "/gettoken")


if __name__ == "__main__":
    unittest.main()
