"""§11.5.1 gh#151 — Contract tests for /einvoice/update.

The FIRST test in this module (test_object_orders_accepted) locks
gh#142 as a permanent regression guard: EE's live call sends `orders`
as a single object, our code must accept it.

Every fixture under fixtures/ that starts with `ee_einvoice_` is
exercised through this endpoint to lock the shape.

Downstream handler side-effects (SI insert, IRN mint) are mocked at
the handler boundary — this test suite verifies the CONTRACT
(request-parse + response-envelope), not the mirror machinery.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ecommerce_super.easyecom.api import gsp as gsp_mod
from ecommerce_super.tests.contract.gsp._helpers import (
    load_fixture,
    request_context,
)


class TestEinvoiceObjectOrdersShape(unittest.TestCase):
    """gh#142 regression guard: EE sends `orders` as a single object,
    not the array the contract doc originally specified. Contract tests
    lock this shape."""

    def test_object_orders_accepted(self):
        body = load_fixture("ee_einvoice_object_orders_so_2610382")
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_einvoice_handler",
                return_value={
                    "status": 200,
                    "message": "Invoice fetched successfully",
                    "reference_code": "SO-2610382",
                    "data": {"invoice_details": {
                        "invoice_id": "176305783",
                        "erp_invoice_num": "SI-2603821",
                        "irn": "abc" * 21,
                        "ack_number": "112010012345678",
                        "ack_date": "2026-07-16T14:15:00+05:30",
                        "invoice_pdf": "https://x/y.pdf",
                        "irn_qr": "",
                        "invoice_base64": "",
                    }},
                },
            ),
        ):
            gsp_mod.einvoice_update()
        response = cap.response
        # Contract: HTTP 200 + top-level status field
        self.assertEqual(response["status"], 200)
        self.assertIn("data", response)
        self.assertIn("invoice_details", response["data"])
        # Round-trip identifiers land in the response
        self.assertEqual(response["reference_code"], "SO-2610382")
        self.assertEqual(
            response["data"]["invoice_details"]["invoice_id"], "176305783"
        )

    def test_handler_receives_the_object_directly_not_wrapped(self):
        """Contract: the endpoint unwraps `orders` before passing to
        the handler. Handler always sees a flat ee_row dict."""
        body = load_fixture("ee_einvoice_object_orders_so_2610382")
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_einvoice_handler",
                return_value={"status": 200},
            ) as handler,
        ):
            gsp_mod.einvoice_update()
        # Handler was called with ee_row being the inner order object
        call_kwargs = handler.call_args.kwargs
        self.assertIn("ee_row", call_kwargs)
        ee_row = call_kwargs["ee_row"]
        self.assertEqual(ee_row["invoice_id"], 176305783)
        self.assertEqual(ee_row["reference_code"], "SO-2610382")


class TestEinvoiceArrayOrdersShape(unittest.TestCase):
    """Contract-doc's original array shape MUST still work. Even
    though EE currently fires the object shape, some EE modules or a
    future EE change could revert. Backward-compat guard."""

    def test_array_orders_accepted(self):
        body = load_fixture("ee_einvoice_array_orders_so_2610382")
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_einvoice_handler",
                return_value={"status": 200},
            ) as handler,
        ):
            gsp_mod.einvoice_update()
        response = cap.response
        self.assertEqual(response["status"], 200)
        # Same as above — handler receives the FIRST array element as ee_row
        ee_row = handler.call_args.kwargs["ee_row"]
        self.assertEqual(ee_row["invoice_id"], 176305783)


class TestEinvoiceInvalidShapes(unittest.TestCase):
    """Contract-side failure modes: what we return when the body is
    the wrong shape entirely. Post-#142 these all return 422 with a
    clear message (never a bare 500 or silent success)."""

    def test_missing_orders_returns_422(self):
        with request_context({"not_orders": "wrong"}) as cap:
            gsp_mod.einvoice_update()
        response = cap.response
        self.assertEqual(response["status"], 422)
        self.assertIn("orders", response["message"])

    def test_empty_orders_array_returns_422(self):
        with request_context({"orders": []}) as cap:
            gsp_mod.einvoice_update()
        response = cap.response
        self.assertEqual(response["status"], 422)
        self.assertIn("empty", response["message"])

    def test_orders_wrong_type_returns_422_naming_the_type(self):
        with request_context({"orders": "a string"}) as cap:
            gsp_mod.einvoice_update()
        response = cap.response
        self.assertEqual(response["status"], 422)
        # gh#142's error message names the received type for debug
        self.assertIn("str", response["message"])


class TestEinvoiceZeroTaxableValueFixture(unittest.TestCase):
    """gh#181 fixture: 100% promo → taxable_value=0. Contract must
    accept the payload (mirror throws deeper if problematic, but the
    endpoint layer should not gate on this)."""

    def test_zero_taxable_value_payload_reaches_handler(self):
        body = load_fixture("ee_einvoice_promo_zero_taxable_value")
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_einvoice_handler",
                return_value={"status": 200},
            ) as handler,
        ):
            gsp_mod.einvoice_update()
        ee_row = handler.call_args.kwargs["ee_row"]
        self.assertEqual(ee_row["invoice_id"], 176306012)
        # The zero taxable value is preserved end-to-end
        self.assertEqual(ee_row["order_items"][0]["taxable_value"], 0)


class TestEinvoiceAuthEnforcement(unittest.TestCase):
    """Contract: 401 shape must be stable — EE relies on the status
    field to route error handling."""

    def test_missing_bearer_returns_401(self):
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            EasyEcomGSPAuthError,
        )
        body = load_fixture("ee_einvoice_object_orders_so_2610382")
        with (
            request_context(body, bypass_auth=False) as cap,
            patch.object(
                gsp_mod, "_get_gsp_auth_header", return_value=None,
            ),
            patch.object(
                gsp_mod, "validate_bearer",
                side_effect=EasyEcomGSPAuthError(
                    "Missing or malformed Authorization header"
                ),
            ),
        ):
            gsp_mod.einvoice_update()
        response = cap.response
        self.assertEqual(response["status"], 401)
        self.assertIn("message", response)
