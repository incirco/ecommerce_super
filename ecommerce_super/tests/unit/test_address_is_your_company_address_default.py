"""gh#24 — Address creation must explicitly set is_your_company_address
so ERPNextAddress.validate_reference doesn't AttributeError on sites
where the IC / ERPNext custom-field migration hasn't run.

The bug: ERPNext extends Address via extend_doctype_class with
ERPNextAddress, whose validate() runs validate_reference() which reads
self.is_your_company_address. That field is a Custom Field seeded by
ERPNext's `accounts/custom/address.json`. On sites where the seeding
hasn't completed, the attribute is absent from the doc, and the
read raises AttributeError BEFORE update_company_address has a chance
to set it.

Our three Address-creation sites (supplier_pull, customer_pull,
warehouse_address_sync) now pass `is_your_company_address: 0`
explicitly. Tests stub frappe.new_doc to capture the payload these
sites pass to .update() / construction and assert the field is
present and set to 0.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class _RecordingAddress:
    """Drop-in for the doc returned by frappe.new_doc("Address"). Records
    every payload passed via .update() / .set() so tests can assert
    is_your_company_address is included."""

    def __init__(self):
        self.fields: dict = {}
        self.links: list = []
        self.gstin = None
        self.flags = type("F", (), {"ignore_permissions": False})()
        self.name = "ADDR-001"

    def append(self, _child, row):
        self.links.append(row)

    def update(self, payload):
        self.fields.update(payload)

    def set(self, key, value):
        self.fields[key] = value

    def insert(self, *args, **kwargs):  # noqa: ARG002
        pass

    def save(self, *args, **kwargs):  # noqa: ARG002
        pass


def _capture_address(target_module_path):
    """Helper — return a (frappe_mock, recorder) pair plus the
    recorder dict the caller can read after invoking the SUT."""
    captured: dict = {}

    def _new_doc(doctype):
        addr = _RecordingAddress()
        captured.setdefault("addresses", []).append(addr)
        return addr

    frappe_mock = MagicMock()
    frappe_mock.new_doc = MagicMock(side_effect=_new_doc)
    frappe_mock.get_doc = MagicMock()
    frappe_mock.db = MagicMock()
    return frappe_mock, captured


class TestCustomerPullAddressDefault(unittest.TestCase):
    def test_is_your_company_address_set_to_zero(self) -> None:
        from ecommerce_super.easyecom.flows import customer_pull

        frappe_mock, captured = _capture_address(
            "ecommerce_super.easyecom.flows.customer_pull"
        )

        with patch.object(customer_pull, "frappe", frappe_mock):
            customer_pull._create_address_strict(
                customer_docname="CUST-001",
                address_type="Billing",
                street="123 Test Street",
                city="Bengaluru",
                zipcode="560001",
                state_name="Karnataka",
                country_name="India",
                gstin=None,
            )

        self.assertEqual(len(captured["addresses"]), 1)
        addr = captured["addresses"][0]
        self.assertIn("is_your_company_address", addr.fields)
        self.assertEqual(addr.fields["is_your_company_address"], 0)


class TestSupplierPullAddressDefault(unittest.TestCase):
    def test_is_your_company_address_set_to_zero(self) -> None:
        from ecommerce_super.easyecom.flows import supplier_pull

        frappe_mock, captured = _capture_address(
            "ecommerce_super.easyecom.flows.supplier_pull"
        )

        with patch.object(supplier_pull, "frappe", frappe_mock):
            supplier_pull._create_address_strict(
                supplier_docname="SUPP-001",
                address_type="Billing",
                street="123 Test Street",
                city="Bengaluru",
                zipcode="560001",
                state_name="Karnataka",
                country_name="India",
                gstin=None,
            )

        self.assertEqual(len(captured["addresses"]), 1)
        addr = captured["addresses"][0]
        self.assertIn("is_your_company_address", addr.fields)
        self.assertEqual(addr.fields["is_your_company_address"], 0)


class TestWarehouseAddressSyncDefault(unittest.TestCase):
    def test_is_your_company_address_set_to_zero(self) -> None:
        from ecommerce_super.easyecom.flows import warehouse_address_sync

        frappe_mock, captured = _capture_address(
            "ecommerce_super.easyecom.flows.warehouse_address_sync"
        )
        # The sync also reads frappe.db.get_value / validate_gstin —
        # stub them out so the test only exercises the address build.
        frappe_mock.db.get_value = MagicMock(return_value=None)

        loc = MagicMock()
        loc.name = "ECS-LOC-test"
        loc.address_line = "456 Test Lane"
        loc.city = "Mumbai"
        loc.state = "Maharashtra"
        loc.country = "India"
        loc.pincode = "400001"
        loc.gstin = ""

        with patch.object(warehouse_address_sync, "frappe", frappe_mock):
            warehouse_address_sync._upsert_warehouse_address(
                loc=loc, warehouse="WH-001"
            )

        self.assertEqual(len(captured["addresses"]), 1)
        addr = captured["addresses"][0]
        self.assertIn("is_your_company_address", addr.fields)
        self.assertEqual(addr.fields["is_your_company_address"], 0)


if __name__ == "__main__":
    unittest.main()
