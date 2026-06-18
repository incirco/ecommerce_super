"""Unit tests for the Internal Customer bootstrap helper.

The helper must produce a Customer that's both:
  (a) routable for §10 STN (is_internal_customer + represents_company
      + Allowed To Transact With), and
  (b) pushable to EE via §8e on the very first try (email + mobile +
      currency + gst_category + gstin + Billing & Shipping Addresses
      with state + pincode + country).

Idempotency is the central invariant: re-running for the same
(source, target) pair never produces duplicate Customers, never adds
a duplicate `Allowed To Transact With` row, and never creates
duplicate Addresses.
"""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.customer.internal_customer_bootstrap import (
    _check_permission,
    _slugify,
    _validate_inputs,
    bootstrap_internal_customer,
)


_BASE = "ecommerce_super.easyecom.customer.internal_customer_bootstrap"


class TestSlugify(unittest.TestCase):
    def test_basic_lowercase(self):
        self.assertEqual(_slugify("ACME"), "acme")

    def test_spaces_become_dashes(self):
        self.assertEqual(
            _slugify("Modern Marwar B2C"), "modern-marwar-b2c"
        )

    def test_special_chars_dropped(self):
        self.assertEqual(_slugify("Acme & Co., Ltd."), "acme-co-ltd")

    def test_no_leading_or_trailing_dash(self):
        self.assertEqual(_slugify("  Foo  "), "foo")

    def test_empty_input_falls_back(self):
        self.assertEqual(_slugify(""), "customer")

    def test_collapses_consecutive_separators(self):
        self.assertEqual(_slugify("A   B"), "a-b")


class TestValidateInputs(unittest.TestCase):
    def test_rejects_missing_source(self):
        with self.assertRaises(frappe.ValidationError):
            _validate_inputs("", "Acme")

    def test_rejects_missing_target(self):
        with self.assertRaises(frappe.ValidationError):
            _validate_inputs("Acme", "")

    def test_rejects_identical_pair(self):
        with patch.object(frappe.db, "exists", return_value=True):
            with self.assertRaises(frappe.ValidationError):
                _validate_inputs("Acme", "Acme")

    def test_rejects_nonexistent_company(self):
        with patch.object(frappe.db, "exists", return_value=False):
            with self.assertRaises(frappe.ValidationError):
                _validate_inputs("Acme", "Beta")

    def test_passes_for_valid_pair(self):
        with patch.object(frappe.db, "exists", return_value=True):
            _validate_inputs("Acme", "Beta")  # no raise


class TestCheckPermission(unittest.TestCase):
    """`frappe.session` is a dict-like, so attribute-level patching
    fails — use patch.dict / direct restoration."""

    def test_administrator_passes(self):
        with patch.dict(frappe.session, {"user": "Administrator"}):
            _check_permission()  # no raise

    def test_system_manager_passes(self):
        with patch.dict(
            frappe.session, {"user": "alice@example.com"}
        ):
            with patch(
                "frappe.get_roles",
                return_value=["System Manager", "Sales User"],
            ):
                _check_permission()  # no raise

    def test_other_role_rejected(self):
        with patch.dict(
            frappe.session, {"user": "alice@example.com"}
        ):
            with patch("frappe.get_roles", return_value=["Sales User"]):
                with self.assertRaises(frappe.PermissionError):
                    _check_permission()


def _baseline_profile(**overrides):
    profile = {
        "default_currency": "INR",
        "country": "India",
        "gst_category": "Registered Regular",
        "gstin": "27ABCDE1234F1Z5",
        "address": {
            "address_line1": "Plot 12, Andheri East",
            "city": "Mumbai",
            "state": "Maharashtra",
            "country": "India",
            "pincode": "400069",
        },
    }
    profile.update(overrides)
    return profile


class TestBootstrap(unittest.TestCase):
    """Behavioural tests for bootstrap_internal_customer."""

    def _stack(
        self,
        *,
        existing_customer: str | None,
        profile: dict | None = None,
        added_atw: bool = True,
        ensured_billing: bool = True,
        ensured_shipping: bool = True,
    ):
        """Common patch stack for bootstrap calls."""
        if profile is None:
            profile = _baseline_profile()
        return [
            patch(f"{_BASE}._check_permission", return_value=None),
            patch(f"{_BASE}._validate_inputs", return_value=None),
            patch(
                f"{_BASE}._read_company_profile", return_value=profile,
            ),
            patch(
                f"{_BASE}._find_existing", return_value=existing_customer,
            ),
            patch(
                f"{_BASE}._ensure_allowed_to_transact_with",
                return_value=added_atw,
            ),
            patch(
                f"{_BASE}._ensure_addresses",
                return_value=(ensured_billing, ensured_shipping),
            ),
            patch(
                f"{_BASE}._describe",
                return_value={
                    "represents_company": "Beta",
                    "allowed_to_transact_with": ["Acme"],
                    "email_id": "x@y.z",
                    "mobile_no": "1234567890",
                    "default_currency": "INR",
                    "gst_category": "Registered Regular",
                    "gstin": "27ABCDE1234F1Z5",
                    "addresses": [],
                },
            ),
            patch.object(frappe.db, "commit"),
        ]

    def test_new_customer_created_with_full_profile_mirror(self):
        """When the Customer doesn't exist, bootstrap creates one with
        currency, gst_category, gstin all mirrored from the target
        Company's profile, AND both Addresses materialized."""
        fake_doc = MagicMock()
        fake_doc.name = "Internal - Beta"

        with ExitStack() as stack:
            for p in self._stack(existing_customer=None):
                stack.enter_context(p)
            stack.enter_context(
                patch(
                    f"{_BASE}._default_customer_group",
                    return_value="All Customer Groups",
                )
            )
            stack.enter_context(
                patch(
                    f"{_BASE}._default_territory",
                    return_value="All Territories",
                )
            )
            new_doc_mock = stack.enter_context(
                patch("frappe.new_doc", return_value=fake_doc)
            )
            result = bootstrap_internal_customer(
                source_company="Acme", target_company="Beta"
            )

        new_doc_mock.assert_called_once_with("Customer")
        fake_doc.insert.assert_called_once()
        update_payload = fake_doc.update.call_args.args[0]
        self.assertEqual(update_payload["is_internal_customer"], 1)
        self.assertEqual(update_payload["represents_company"], "Beta")
        self.assertEqual(update_payload["default_currency"], "INR")
        self.assertEqual(
            update_payload["gst_category"], "Registered Regular"
        )
        self.assertEqual(
            update_payload["gstin"], "27ABCDE1234F1Z5"
        )
        self.assertTrue(result["created"])
        self.assertTrue(result["added_atw_row"])
        self.assertTrue(result["added_billing_address"])
        self.assertTrue(result["added_shipping_address"])

    def test_existing_customer_returns_with_no_writes_when_complete(self):
        """When everything is already configured, bootstrap is a pure
        observer — no Customer created, no ATW row added, no
        Addresses created."""
        with ExitStack() as stack:
            for p in self._stack(
                existing_customer="Internal - Beta",
                added_atw=False,
                ensured_billing=False,
                ensured_shipping=False,
            ):
                stack.enter_context(p)
            result = bootstrap_internal_customer(
                source_company="Acme", target_company="Beta"
            )

        self.assertFalse(result["created"])
        self.assertFalse(result["added_atw_row"])
        self.assertFalse(result["added_billing_address"])
        self.assertFalse(result["added_shipping_address"])

    def test_existing_customer_gets_repaired_when_atw_missing(self):
        with ExitStack() as stack:
            for p in self._stack(
                existing_customer="Internal - Beta",
                added_atw=True,
                ensured_billing=False,
                ensured_shipping=False,
            ):
                stack.enter_context(p)
            result = bootstrap_internal_customer(
                source_company="Acme", target_company="Beta"
            )
        self.assertTrue(result["added_atw_row"])
        self.assertFalse(result["created"])

    def test_existing_customer_gets_repaired_when_address_missing(self):
        with ExitStack() as stack:
            for p in self._stack(
                existing_customer="Internal - Beta",
                added_atw=False,
                ensured_billing=True,
                ensured_shipping=False,
            ):
                stack.enter_context(p)
            result = bootstrap_internal_customer(
                source_company="Acme", target_company="Beta"
            )
        self.assertTrue(result["added_billing_address"])
        self.assertFalse(result["added_shipping_address"])
        self.assertFalse(result["created"])

    def test_unregistered_company_yields_unregistered_customer(self):
        """When target Company has no GSTIN (gst_category=Unregistered),
        the Customer mirrors that — §8e then substitutes URP for
        taxIdentificationNumber rather than flagging."""
        fake_doc = MagicMock()
        fake_doc.name = "Internal - Beta"
        unregistered_profile = _baseline_profile(
            gst_category="Unregistered", gstin=None,
        )
        with ExitStack() as stack:
            for p in self._stack(
                existing_customer=None, profile=unregistered_profile,
            ):
                stack.enter_context(p)
            stack.enter_context(
                patch(
                    f"{_BASE}._default_customer_group",
                    return_value="All Customer Groups",
                )
            )
            stack.enter_context(
                patch(
                    f"{_BASE}._default_territory",
                    return_value="All Territories",
                )
            )
            stack.enter_context(
                patch("frappe.new_doc", return_value=fake_doc)
            )
            bootstrap_internal_customer(
                source_company="Acme", target_company="Beta"
            )
        update_payload = fake_doc.update.call_args.args[0]
        self.assertEqual(
            update_payload["gst_category"], "Unregistered"
        )
        self.assertIsNone(update_payload["gstin"])


if __name__ == "__main__":
    unittest.main()
