"""§8e Customer Push — Shipping-address fallback (gh#60).

`_gather_customer_payload_dict` used to require BOTH a Billing-typed
Address AND a Shipping-typed Address linked to the Customer. If only a
Billing address existed, the four `dispatch_*` keys came back empty and
the EE CreateCustomer gate flagged with
    "missing dispatchState name for CreateCustomer"
    "missing or non-numeric dispatchPostalCode"
even though the Billing address had a perfectly valid state and pincode.

This is the common SME / B2B-large case: customers ship to the same
address they bill to and only carry one Address row. The §10 Internal
Customer bootstrap (PR #68) sidesteps this by minting BOTH address rows
during onboarding, but customers the FDE didn't create that way still
hit the gate.

Fix: when no Shipping-typed address is linked, fall back to the
Billing-typed address for the `dispatch_*` fields. Tests pin the four
shapes that matter.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows import customer_push


def _exercise_gather(
    *,
    billing: dict | None,
    shipping: dict | None,
) -> dict:
    """Run `_gather_customer_payload_dict` against a synthetic Customer
    and stubbed `_find_address`. Return the flat payload dict the
    ruleset would consume."""
    customer = MagicMock()
    customer.name = "TEST-CUST-DISPATCH-FALLBACK"
    customer.customer_name = "Test Customer Co"
    customer.email_id = "ops@test.local"
    customer.mobile_no = "9999999999"
    customer.gstin = "29ABCDE1234F1Z5"
    customer.gst_category = "Registered Regular"
    customer.default_currency = "INR"

    def fake_find_address(customer_docname, *, address_type):
        if address_type == "Billing":
            return billing
        if address_type == "Shipping":
            return shipping
        return None

    with patch.object(
        customer_push, "_find_address", side_effect=fake_find_address,
    ):
        return customer_push._gather_customer_payload_dict(customer)


class TestDispatchAddressFallback(unittest.TestCase):
    """gh#60 — dispatch_* fields must be populated even when the
    Customer only has a Billing-typed Address linked."""

    _BILLING = {
        "address_line1": "12 MG Road",
        "city": "Bangalore",
        "pincode": "560066",
        "state": "Karnataka",
        "country": "India",
    }

    _SHIPPING = {
        "address_line1": "Plot 7, Whitefield",
        "city": "Bangalore",
        "pincode": "560048",
        "state": "Karnataka",
        "country": "India",
    }

    def test_billing_only_falls_back_to_billing_for_dispatch(self) -> None:
        """gh#60 headline — the bug scenario. Customer has Billing only
        (the common SME case). The dispatch_* fields must be populated
        from the Billing address, not left empty."""
        out = _exercise_gather(billing=self._BILLING, shipping=None)

        self.assertEqual(out["billing_state_name"], "Karnataka")
        self.assertEqual(out["billing_postal_code"], "560066")
        # The gh#60 assertion — dispatch_* fall back to billing, not "".
        self.assertEqual(out["dispatch_state_name"], "Karnataka")
        self.assertEqual(out["dispatch_postal_code"], "560066")
        self.assertEqual(out["dispatch_street"], "12 MG Road")
        self.assertEqual(out["dispatch_city"], "Bangalore")

    def test_shipping_present_overrides_billing(self) -> None:
        """When a Shipping-typed address exists, it wins for dispatch_*
        even if Billing is also present. The fallback only kicks in
        when Shipping is missing."""
        out = _exercise_gather(
            billing=self._BILLING, shipping=self._SHIPPING,
        )

        self.assertEqual(out["billing_state_name"], "Karnataka")
        self.assertEqual(out["billing_postal_code"], "560066")
        self.assertEqual(out["billing_street"], "12 MG Road")
        # Shipping wins for the dispatch side.
        self.assertEqual(out["dispatch_postal_code"], "560048")
        self.assertEqual(out["dispatch_street"], "Plot 7, Whitefield")

    def test_no_addresses_leaves_dispatch_empty(self) -> None:
        """Defensive — a Customer with no linked Address at all still
        gets a dict with empty strings (the gate then flags both
        billing and dispatch). The fallback doesn't invent data."""
        out = _exercise_gather(billing=None, shipping=None)

        self.assertEqual(out["billing_state_name"], "")
        self.assertEqual(out["billing_postal_code"], "")
        self.assertEqual(out["dispatch_state_name"], "")
        self.assertEqual(out["dispatch_postal_code"], "")

    def test_shipping_only_does_not_synthesize_billing(self) -> None:
        """The fallback is one-directional: dispatch_* falls back to
        billing, NOT the other way. A Customer with only a
        Shipping-typed Address still gets empty billing_* (and the
        gate flags billingState / billingPostalCode appropriately)."""
        out = _exercise_gather(billing=None, shipping=self._SHIPPING)

        self.assertEqual(out["billing_state_name"], "")
        self.assertEqual(out["billing_postal_code"], "")
        self.assertEqual(out["dispatch_state_name"], "Karnataka")
        self.assertEqual(out["dispatch_postal_code"], "560048")


if __name__ == "__main__":
    unittest.main()
