"""Handler shim: /einvoice/update no-ops when reference_code points
at a §10 internal-transfer DN (Transfer Map, not B2B Order Map).

Locks the behavior added on 2026-07-28 after investigating DL-261500-1
(same class as DL-261467). Same-GSTIN internal transfers don't need an
e-invoice under GST (intra-GSTIN movement is not a supply). Under the
current 422 behavior, EE's "Generate E-Invoice for B2B Order" toggle
fires against these DNs, burns fresh invoice_ids on each retry, and
spams our Error Log — despite there being nothing our side can or
should do.

Contract locked here:

  1. Flag OFF (default) → current 422 behavior preserved. No behavior
     change on any Company that hasn't opted in.
  2. Flag ON, reference_code matches a Transfer Map → GSPHandlerNoOp,
     endpoint returns HTTP 200 with 'no e-invoice required' body. No
     SI created. No Error Log entry.
  3. Flag ON, reference_code matches a B2B Order Map → normal path
     runs (mirror creates the SI, IRN gets minted). No regression.
  4. Flag ON, reference_code matches NEITHER → GSPHandlerError as
     before (unknown reference, 422). Preserves the anti-noise
     purpose without hiding real bugs.

The shim's `_should_noop_for_internal_transfer` helper is the safety
gate — flag-checked + defensive against every lookup failure so it
can never accidentally block the normal path.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows.b2b_sales import gsp_handler as mod


def _ee_row(reference_code="DL-261500-1", invoice_id="684121579"):
    return {
        "invoice_id": invoice_id,
        "reference_code": reference_code,
        "total_amount": 7680.53,
        "seller_gst": "08AAMCM6783B1Z6",
        "buyer_gst": "08AAMCM6783B1Z6",
    }


# ============================================================
# Helper: _should_noop_for_internal_transfer
# ============================================================


class TestShouldNoopHelper(unittest.TestCase):
    """The gate itself — flag OFF → False, flag ON + Transfer Map hit
    → True, defensive failure modes → False."""

    def _stub(self, *, flag_value, transfer_map_exists, company="MMPL"):
        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "EasyEcom Account":
                return company
            if doctype == "EasyEcom Company Settings":
                return flag_value
            return None
        def _exists(doctype, filters):
            if doctype == "EasyEcom Transfer Map":
                return transfer_map_exists
            return False
        return _get_value, _exists

    def _run(self, *, flag_value, transfer_map_exists, ref="DL-261500-1"):
        gv, ex = self._stub(
            flag_value=flag_value, transfer_map_exists=transfer_map_exists,
        )
        with (
            patch.object(mod.frappe.db, "get_value", side_effect=gv),
            patch.object(mod.frappe.db, "exists", side_effect=ex),
        ):
            return mod._should_noop_for_internal_transfer(
                ref, ee_account="Test Account",
            )

    def test_flag_off_never_fires_even_if_transfer_map_exists(self):
        """Backward-compat: no Company that hasn't opted in ever gets the
        new behavior. Default value of the flag is 0."""
        self.assertFalse(self._run(flag_value=0, transfer_map_exists=True))

    def test_flag_on_and_transfer_map_exists_fires(self):
        """The happy path this shim was built for."""
        self.assertTrue(self._run(flag_value=1, transfer_map_exists=True))

    def test_flag_on_but_no_transfer_map_does_not_fire(self):
        """reference_code isn't a §10 DN → let the normal path handle
        it (either it's a §11 SO or an unknown reference)."""
        self.assertFalse(self._run(flag_value=1, transfer_map_exists=False))

    def test_empty_reference_code_never_fires(self):
        """Defensive: never fire on empty/missing reference_code."""
        self.assertFalse(
            self._run(flag_value=1, transfer_map_exists=True, ref=""),
        )

    def test_no_ee_account_never_fires(self):
        """Defensive: without an ee_account we can't resolve the Company."""
        with (
            patch.object(mod.frappe.db, "get_value", return_value=None),
            patch.object(mod.frappe.db, "exists", return_value=True),
        ):
            self.assertFalse(
                mod._should_noop_for_internal_transfer(
                    "DL-X", ee_account="",
                )
            )

    def test_ee_account_has_no_company_never_fires(self):
        """Defensive: EE Account row exists but company field is empty."""
        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "EasyEcom Account":
                return None
            return None
        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
        ):
            self.assertFalse(
                mod._should_noop_for_internal_transfer(
                    "DL-X", ee_account="Test",
                )
            )

    def test_lookup_exception_never_fires(self):
        """Defensive: any DB exception during the flag check falls back
        to False so this shim can NEVER block the normal path."""
        with patch.object(mod.frappe.db, "get_value", side_effect=RuntimeError("db down")):
            self.assertFalse(
                mod._should_noop_for_internal_transfer(
                    "DL-X", ee_account="Test",
                )
            )


# ============================================================
# Handler-level integration: find_or_create_si_for_gsp
# ============================================================


class TestFindOrCreateHandlerRaisesNoOp(unittest.TestCase):
    """When the shim gate fires, find_or_create_si_for_gsp raises
    GSPHandlerNoOp — distinct from GSPHandlerError so callers can
    treat it as HTTP 200, not 422."""

    def test_flag_on_transfer_map_ref_raises_noop_not_error(self):
        """DL-261500-1 scenario: EE hits us with an internal-transfer
        reference_code, flag is enabled, we raise GSPHandlerNoOp."""

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None  # step-1 idempotency miss
            if doctype == "EasyEcom Account":
                return "MMPL"
            if doctype == "EasyEcom Company Settings":
                return 1  # flag ON
            return None
        def _exists(doctype, filters):
            if doctype == "EasyEcom Transfer Map":
                return True  # ref matches a Transfer Map
            return False

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
        ):
            with self.assertRaises(mod.GSPHandlerNoOp) as ctx:
                mod.find_or_create_si_for_gsp(
                    ee_row=_ee_row(reference_code="DL-261500-1"),
                    ee_account="MMPL Account",
                )
        self.assertIn("DL-261500-1", str(ctx.exception))
        self.assertIn("internal-transfer", str(ctx.exception))

    def test_flag_off_falls_through_to_normal_gsp_handler_error(self):
        """Regression guard: with flag OFF, the handler behaves exactly
        as before — GSPHandlerError when no B2B Order Map found."""

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None
            if doctype == "EasyEcom Account":
                return "MMPL"
            if doctype == "EasyEcom Company Settings":
                return 0  # flag OFF
            if doctype == "EasyEcom B2B Order Map":
                return None  # not a B2B ref either
            return None

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", return_value=False),
        ):
            with self.assertRaises(mod.GSPHandlerError) as ctx:
                mod.find_or_create_si_for_gsp(
                    ee_row=_ee_row(reference_code="DL-261500-1"),
                    ee_account="MMPL Account",
                )
        self.assertIn("No EasyEcom B2B Order Map found", str(ctx.exception))

    def test_flag_on_but_b2b_order_map_ref_takes_normal_path(self):
        """When reference_code IS a §11 SO (has a B2B Order Map),
        the noop shim doesn't fire even with the flag on — the
        normal mirror-then-mint flow runs. Locks: the noop check
        precedes the B2B Order Map lookup, but is Transfer-Map-scoped,
        so B2B refs pass through untouched."""

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                return None  # step-1 miss
            if doctype == "EasyEcom Account":
                return "MMPL"
            if doctype == "EasyEcom Company Settings":
                return 1  # flag ON
            if doctype == "EasyEcom B2B Order Map":
                return "MAP-SO-VALID"  # ref IS a B2B SO
            return None
        def _exists(doctype, filters):
            if doctype == "EasyEcom Transfer Map":
                return False  # NOT a Transfer Map ref
            return False

        map_doc_stub = MagicMock()
        map_doc_stub.name = "MAP-SO-VALID"
        map_doc_stub.sales_order = "SO-VALID"

        mirror_stub = MagicMock(return_value={
            "sales_invoice": "SI-NEW-001",
            "operation": "created",
            "variance_pct": 0.0,
            "ee_total": 1000.0,
            "si_total": 1000.0,
        })

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
            patch.object(mod.frappe, "get_doc", return_value=map_doc_stub),
            patch.object(mod.frappe.db, "set_value"),
            patch.object(mod.frappe.db, "commit"),
            patch.object(mod, "now_datetime", return_value="2026-07-28 12:00:00"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                mirror_stub,
            ),
        ):
            result = mod.find_or_create_si_for_gsp(
                ee_row=_ee_row(reference_code="SO-VALID"),
                ee_account="MMPL Account",
            )
        self.assertEqual(result, "SI-NEW-001")
        mirror_stub.assert_called_once()


# ============================================================
# Endpoint-level integration: /einvoice/update returns HTTP 200
# ============================================================


class TestEinvoiceEndpointReturnsHttp200OnNoOp(unittest.TestCase):
    """When the handler raises GSPHandlerNoOp, the endpoint MUST:
      - set frappe.response.http_status_code = 200
      - return a body with status=200 + a valid invoice_details shape
        (all IRN fields null so EE can tell there's nothing to store)
      - NOT log to Error Log (via _log_inbound_gsp_failure)
    """

    def _run_endpoint(self):
        from ecommerce_super.easyecom.api import gsp as gsp_api

        # Fake response object with http_status_code we can inspect
        fake_response = MagicMock()
        fake_response.http_status_code = None

        log_called = MagicMock()

        with (
            patch.object(gsp_api.frappe, "response", fake_response),
            patch.object(gsp_api, "_log_inbound_gsp_failure", log_called),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                "find_or_create_si_for_gsp",
                side_effect=mod.GSPHandlerNoOp(
                    "reference_code 'DL-261500-1' is a §10 internal-transfer "
                    "Delivery Note. Same-GSTIN internal transfers don't require "
                    "an e-invoice."
                ),
            ),
        ):
            result = gsp_api._einvoice_handler_impl(
                ee_row=_ee_row(reference_code="DL-261500-1"),
                ee_account="MMPL Account",
            )
        return result, fake_response, log_called

    def test_status_code_is_200(self):
        result, resp, _ = self._run_endpoint()
        self.assertEqual(resp.http_status_code, 200)
        self.assertEqual(result["status"], 200)

    def test_reference_code_echoed(self):
        result, _, _ = self._run_endpoint()
        self.assertEqual(result["reference_code"], "DL-261500-1")

    def test_message_carries_reason_from_exception(self):
        result, _, _ = self._run_endpoint()
        self.assertIn("internal-transfer", result["message"])
        self.assertIn("DL-261500-1", result["message"])

    def test_invoice_details_has_all_ee_expected_keys_but_null_irn(self):
        """EE's contract expects data.invoice_details to be present. On
        a no-op, all IRN-related fields are null so EE can distinguish
        from a real IRN response."""
        result, _, _ = self._run_endpoint()
        inv = result["data"]["invoice_details"]
        # invoice_id echoed from the request (EE uses this to correlate)
        self.assertEqual(inv["invoice_id"], "684121579")
        # ERPNext-side fields all null — no SI was created
        for k in ("erp_invoice_num", "irn", "ack_number", "ack_date",
                  "invoice_pdf", "irn_qr", "invoice_base64"):
            self.assertIsNone(inv[k], f"{k} must be null on no-op")

    def test_no_error_log_write(self):
        """Critical anti-noise property: no _log_inbound_gsp_failure call.
        The whole point of the no-op is to stop spamming Error Log for
        expected-behavior 200s."""
        _, _, log_called = self._run_endpoint()
        log_called.assert_not_called()


if __name__ == "__main__":
    unittest.main()
