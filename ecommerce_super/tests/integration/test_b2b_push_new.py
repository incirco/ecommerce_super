"""§11 Stage 2 — New B2B push integration test.

End-to-end state-propagation contract:
  1. Map row created with module="New B2B", status="Queued".
  2. ee_order_id / ee_suborder_id / ee_invoice_id are None at this
     stage (EE assigns later; Stage 3 polling populates).
  3. SO.ecs_b2b_order_map back-reference set on the persisted SO.

New B2B's "Successfully Queued" response shape per the §11 packet
has empty data.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.push import (
    push_b2b_order_async,
)


_NEW_B2B_RESPONSE_QUEUED = {
    "code": 200,
    "data": [],
    "message": "Successfully Queued",
}


def _make_so(name="SAL-ORD-T-NEW-001", company="_Test Company"):
    so = MagicMock()
    so.name = name
    so.company = company
    so.set_warehouse = "EE-WH-NEW-B2B"
    so.shipping_address_name = "ACME-Shipping"
    so.customer = "ACME"
    so.grand_total = 10000.0
    so.transaction_date = "2026-06-14"
    so.delivery_date = "2026-06-20"
    so.terms = ""
    so.discount_amount = 0
    so.taxes = []
    so.docstatus = 1
    item = MagicMock()
    item.item_code = "ITEM-X"
    item.item_name = "Test Item"
    item.qty = 5
    item.rate = 2000
    item.idx = 1
    item.warehouse = "EE-WH-NEW-B2B"
    item.discount_amount = 0
    so.items = [item]
    return so


def _make_account(name="Puresta Test", ecs_b2b_module="New B2B"):
    a = MagicMock()
    a.name = name
    a.ecs_b2b_module = ecs_b2b_module
    a.get = lambda k, default=None: {"ecs_b2b_module": ecs_b2b_module}.get(
        k, default
    )
    return a


def _make_customer(tax_id=None):  # URP customer to exercise that path
    c = MagicMock()
    c.name = "ACME"
    c.tax_id = tax_id
    c.customer_primary_address = "ACME-Billing"
    c.customer_name = "ACME Industries"
    c.mobile_no = "9000000000"
    c.email_id = "ops@acme.example"
    return c


def _make_address():
    a = MagicMock()
    a.address_line1 = "Plot 42"
    a.address_line2 = "Sector 7"
    a.pincode = "560001"
    a.city = "Bengaluru"
    a.state = "Karnataka"
    a.country = "India"
    a.phone = None
    a.email_id = None
    del a.ecs_latitude
    del a.ecs_longitude
    return a


class TestPushNewB2BPersistsStateEndToEnd(unittest.TestCase):
    def test_new_b2b_push_persists_queued_map_and_so_back_ref(self) -> None:
        so = _make_so()
        ee_account = _make_account()
        customer = _make_customer(tax_id=None)  # URP path
        addr = _make_address()

        captured_inserts: list[dict] = []
        captured_set_values: list[tuple] = []

        def _new_doc(doctype):
            inst = MagicMock()
            inst.name = f"ECS-B2B-{so.name}"

            def _update(d):
                captured_inserts.append({"doctype": doctype, **d})
                for k, v in d.items():
                    setattr(inst, k, v)
                return inst

            inst.update = _update
            inst.insert = MagicMock(return_value=None)
            return inst

        def _set_value(dt, name, fieldname_or_dict, value=None, **kw):
            if isinstance(fieldname_or_dict, dict):
                captured_set_values.append((dt, name, dict(fieldname_or_dict)))
            else:
                captured_set_values.append((dt, name, fieldname_or_dict, value))

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom Account":
                return ee_account
            if doctype == "Customer":
                return customer
            if doctype == "Address":
                return addr
            return MagicMock()

        client_post = MagicMock(return_value=_NEW_B2B_RESPONSE_QUEUED)
        client_mock = MagicMock()
        client_mock.post = client_post

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            patch.object(frappe, "new_doc", side_effect=_new_doc),
            patch.object(frappe.db, "set_value", side_effect=_set_value),
            patch.object(frappe.db, "commit"),
            patch.object(frappe.utils, "now", return_value="2026-06-14 10:00:00"),
            patch.object(frappe, "as_json", side_effect=lambda x: str(x)),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.push.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.push.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-002"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.push.get_ee_account_for_warehouse",
                return_value=ee_account,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.payload_builder.resolve_ee_sku",
                return_value="EE-SKU-X",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.customer_block.resolve_ee_customer_id",
                return_value="EE-CUST-9002",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.push._write_sync_record",
                return_value="ECS-SR-T2",
            ),
        ):
            outcome = push_b2b_order_async(
                sales_order=so.name,
                easyecom_account=ee_account.name,
            )

        # Outcome: Queued, no IDs.
        self.assertEqual(outcome["operation"], "queued")
        self.assertEqual(outcome["status"], "Queued")
        self.assertIsNone(outcome["ee_order_id"])
        self.assertIsNone(outcome["ee_suborder_id"])
        self.assertIsNone(outcome["ee_invoice_id"])

        # Map row was inserted with module=New B2B, status=Queued,
        # ee_*ids = None.
        map_inserts = [
            i for i in captured_inserts
            if i["doctype"] == "EasyEcom B2B Order Map"
        ]
        self.assertEqual(len(map_inserts), 1)
        m = map_inserts[0]
        self.assertEqual(m["module"], "New B2B")
        self.assertEqual(m["status"], "Queued")
        self.assertIsNone(m["ee_order_id"])
        self.assertIsNone(m["ee_suborder_id"])
        self.assertIsNone(m["ee_invoice_id"])

        # SO back-ref persisted via 4-arg set_value form.
        matched = [
            row for row in captured_set_values
            if row[0] == "Sales Order"
            and len(row) >= 4
            and row[2] == "ecs_b2b_order_map"
        ]
        self.assertTrue(matched, f"Got writes: {captured_set_values}")

        # Payload had taxIdentificationNumber = "URP" (URP fallback
        # for missing GSTIN, New B2B only).
        sent_payload = client_post.call_args.kwargs["payload"]
        self.assertEqual(sent_payload["taxIdentificationNumber"], "URP")
        self.assertEqual(sent_payload["queue"], 1)
        self.assertEqual(sent_payload["is_pricing_master"], False)


if __name__ == "__main__":
    unittest.main()
