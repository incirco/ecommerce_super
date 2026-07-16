"""§11.5.1 gh#151 — Contract tests for /ewaybill/update.

Same object-vs-array defensiveness as /einvoice/update. Locks the
response envelope's `data.eway_details` shape.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ecommerce_super.easyecom.api import gsp as gsp_mod
from ecommerce_super.tests.contract.gsp._helpers import (
    load_fixture,
    request_context,
)


class TestEwaybillObjectOrdersShape(unittest.TestCase):
    """/ewaybill/update mirrors /einvoice/update's envelope. Both
    shapes must be accepted (gh#142 pattern applies here too)."""

    def test_object_orders_accepted(self):
        body = load_fixture("ee_ewaybill_object_orders_so_2610382")
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_ewaybill_handler",
                return_value={
                    "status": 200,
                    "message": "Eway bill generated successfully",
                    "reference_code": "SO-2610382",
                    "data": {"eway_details": {
                        "invoice_id": "176305783",
                        "erp_invoice_num": "SI-2603821",
                        "eway_bill_number": "331000123456",
                        "eway_bill_date": "2026-07-16T14:20:00+05:30",
                        "valid_upto": "2026-07-17T14:20:00+05:30",
                        "eway_bill_base64": "",
                    }},
                },
            ),
        ):
            gsp_mod.ewaybill_update()
        response = cap.response
        self.assertEqual(response["status"], 200)
        self.assertIn("eway_details", response["data"])
        self.assertEqual(
            response["data"]["eway_details"]["eway_bill_number"],
            "331000123456",
        )

    def test_handler_receives_transport_fields_flat(self):
        body = load_fixture("ee_ewaybill_object_orders_so_2610382")
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_ewaybill_handler",
                return_value={"status": 200},
            ) as handler,
        ):
            gsp_mod.ewaybill_update()
        ee_row = handler.call_args.kwargs["ee_row"]
        self.assertEqual(ee_row["invoice_id"], 176305783)
        self.assertEqual(ee_row["vehicle_number"], "KA01AB1234")
        self.assertEqual(ee_row["mode_of_transport"], "Road")
        self.assertEqual(ee_row["distance"], 425)


class TestEwaybillArrayOrdersShape(unittest.TestCase):
    """Backward-compat with the original array shape."""

    def test_array_orders_accepted(self):
        body = {
            "orders": [
                load_fixture(
                    "ee_ewaybill_object_orders_so_2610382"
                )["orders"]
            ]
        }
        with (
            request_context(body) as cap,
            patch.object(
                gsp_mod, "_ewaybill_handler",
                return_value={"status": 200},
            ) as handler,
        ):
            gsp_mod.ewaybill_update()
        response = cap.response
        self.assertEqual(response["status"], 200)
        # Handler unwraps to first array element
        ee_row = handler.call_args.kwargs["ee_row"]
        self.assertEqual(ee_row["invoice_id"], 176305783)


class TestEwaybillInvalidShapes(unittest.TestCase):
    def test_missing_orders_returns_422(self):
        with request_context({"not_orders": "wrong"}) as cap:
            gsp_mod.ewaybill_update()
        response = cap.response
        self.assertEqual(response["status"], 422)
        self.assertIn("orders", response["message"])
