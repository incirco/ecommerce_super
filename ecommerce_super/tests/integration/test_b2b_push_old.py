"""§11 Stage 2 — Old B2B push integration test.

End-to-end state-propagation contract (§10 SI back-link lesson
applied):
  1. Map row is created with the correct SO link.
  2. Map carries ee_order_id / ee_suborder_id / ee_invoice_id from
     the EE response data.
  3. Map status = "Pushed".
  4. SO.ecs_b2b_order_map = Map.name (back-reference persisted to
     DB, not just to the in-memory document).
  5. payload_hash matches the computed hash of the request.

These tests mock the EasyEcomClient.post call — assertions are about
the LOCAL persistence the integration drives, not the EE wire.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.payload_builder import (
    compute_payload_hash,
)
from ecommerce_super.easyecom.flows.b2b_sales.push import (
    push_b2b_order_async,
)


_OLD_B2B_RESPONSE_OK = {
    "code": 200,
    "data": {
        "OrderID": "OD-777001",
        "SuborderID": "SO-888001",
        "InvoiceID": "INV-999001",
    },
    "message": "Success",
}


def _make_so(name="SAL-ORD-T-OLD-001", company="_Test Company"):
    """Minimal SO mock — enough for the payload + persistence path."""
    so = MagicMock()
    so.name = name
    so.company = company
    so.set_warehouse = "EE-WH-OLD-B2B"
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
    item.warehouse = "EE-WH-OLD-B2B"
    item.discount_amount = 0
    so.items = [item]
    return so


def _make_account(name="Harmony", ecs_b2b_module="Old B2B"):
    a = MagicMock()
    a.name = name
    a.ecs_b2b_module = ecs_b2b_module
    a.get = lambda k, default=None: {"ecs_b2b_module": ecs_b2b_module}.get(
        k, default
    )
    return a


def _make_customer(
    tax_id="29ABCDE1234F1Z5",
    customer_primary_address="ACME-Billing",
    customer_name="ACME Industries",
    mobile_no="9000000000",
    email_id="ops@acme.example",
):
    c = MagicMock()
    c.name = "ACME"
    c.tax_id = tax_id
    c.customer_primary_address = customer_primary_address
    c.customer_name = customer_name
    c.mobile_no = mobile_no
    c.email_id = email_id
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


class TestPushOldB2BPersistsStateEndToEnd(unittest.TestCase):
    def test_old_b2b_push_persists_map_and_so_back_ref(self) -> None:
        """The headline state-propagation test: after push_b2b_order_async
        runs, the Map row exists with all EE IDs AND the SO.ecs_b2b_order_map
        back-reference points to it.
        """
        so = _make_so()
        ee_account = _make_account()
        customer = _make_customer()
        billing = _make_address()
        shipping = _make_address()

        # Use a stateful mock for new_doc so we can capture the inserted
        # Map row's name (autoname normally builds ECS-B2B-{sales_order}).
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
                # 4-arg form: (dt, name, fieldname, value)
                captured_set_values.append((dt, name, fieldname_or_dict, value))

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom Account":
                return ee_account
            if doctype == "Customer":
                return customer
            if doctype == "Address":
                return billing  # both billing and shipping use this mock
            return MagicMock()

        client_post = MagicMock(return_value=_OLD_B2B_RESPONSE_OK)
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
                return_value=MagicMock(location_key="ee-loc-001"),
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
                return_value="EE-CUST-9001",
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.push._write_sync_record",
                return_value="ECS-SR-T1",
            ),
        ):
            outcome = push_b2b_order_async(
                sales_order=so.name,
                easyecom_account=ee_account.name,
            )

        # 1. Outcome carries expected operation + status.
        self.assertEqual(outcome["operation"], "pushed")
        self.assertEqual(outcome["status"], "Pushed")
        self.assertEqual(outcome["ee_order_id"], "OD-777001")
        self.assertEqual(outcome["ee_suborder_id"], "SO-888001")
        self.assertEqual(outcome["ee_invoice_id"], "INV-999001")

        # 2. The Map row was inserted with the SO link + EE IDs.
        map_inserts = [
            i for i in captured_inserts
            if i["doctype"] == "EasyEcom B2B Order Map"
        ]
        self.assertEqual(len(map_inserts), 1)
        m = map_inserts[0]
        self.assertEqual(m["sales_order"], so.name)
        self.assertEqual(m["easyecom_account"], ee_account.name)
        self.assertEqual(m["module"], "Old B2B")
        self.assertEqual(m["status"], "Pushed")
        self.assertEqual(m["ee_order_id"], "OD-777001")
        self.assertEqual(m["ee_suborder_id"], "SO-888001")
        self.assertEqual(m["ee_invoice_id"], "INV-999001")

        # 3. SO back-reference was persisted via db.set_value (NOT
        # just set on the in-memory document — this is the §10 lesson).
        # set_value is called in 4-arg form: (dt, name, fieldname, value).
        so_back_ref_writes = [
            row for row in captured_set_values
            if row[0] == "Sales Order"
            and len(row) >= 4
            and row[2] == "ecs_b2b_order_map"
        ]
        self.assertTrue(
            so_back_ref_writes,
            f"Expected SO.ecs_b2b_order_map back-ref write; "
            f"got writes: {captured_set_values}",
        )
        # And the value points at the inserted Map row.
        _, _, _, value = so_back_ref_writes[0]
        self.assertEqual(value, f"ECS-B2B-{so.name}")

        # 4. payload_hash on the Map matches re-computation of the
        # request payload that was sent.
        sent_payload = client_post.call_args.kwargs["payload"]
        self.assertEqual(m["payload_hash"], compute_payload_hash(sent_payload))

        # 5. POST was made to CREATE_ORDER with the payload.
        self.assertTrue(client_post.called)


if __name__ == "__main__":
    unittest.main()
