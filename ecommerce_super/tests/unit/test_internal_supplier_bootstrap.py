"""Unit tests for the Internal Supplier bootstrap helper.

Mirror of test_internal_customer_bootstrap.py — the helper must
produce a Supplier that's both routable for §10 inbound STN AND
populated such that downstream tax / PR / Customs validators don't
flag.
"""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.customer.internal_supplier_bootstrap import (
    _check_permission,
    _slugify,
    _validate_inputs,
    bootstrap_internal_supplier,
)


_BASE = "ecommerce_super.easyecom.customer.internal_supplier_bootstrap"


class TestSlugify(unittest.TestCase):
    def test_basic_lowercase(self):
        self.assertEqual(_slugify("ACME"), "acme")

    def test_spaces_become_dashes(self):
        self.assertEqual(_slugify("Modern Marwar Pvt Ltd"),
                         "modern-marwar-pvt-ltd")

    def test_empty_input_falls_back_to_supplier(self):
        self.assertEqual(_slugify(""), "supplier")


class TestValidateInputs(unittest.TestCase):
    def test_rejects_missing_source(self):
        with self.assertRaises(frappe.ValidationError):
            _validate_inputs("", "Acme")

    def test_rejects_missing_target(self):
        with self.assertRaises(frappe.ValidationError):
            _validate_inputs("Acme", "")

    def test_accepts_identical_pair_for_single_company(self):
        """Single-Company deployments use one Internal Supplier."""
        with patch.object(frappe.db, "exists", return_value=True):
            _validate_inputs("Acme", "Acme")  # no raise

    def test_rejects_nonexistent_company(self):
        with patch.object(frappe.db, "exists", return_value=False):
            with self.assertRaises(frappe.ValidationError):
                _validate_inputs("Acme", "Beta")


class TestCheckPermission(unittest.TestCase):
    def test_administrator_passes(self):
        with patch.dict(frappe.session, {"user": "Administrator"}):
            _check_permission()

    def test_system_manager_passes(self):
        with patch.dict(frappe.session, {"user": "x@y.z"}):
            with patch("frappe.get_roles",
                       return_value=["System Manager"]):
                _check_permission()

    def test_other_role_rejected(self):
        with patch.dict(frappe.session, {"user": "x@y.z"}):
            with patch("frappe.get_roles",
                       return_value=["Sales User"]):
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
    def _stack(
        self,
        *,
        existing_supplier: str | None,
        profile: dict | None = None,
        added_atw: bool = True,
        ensured_billing: bool = True,
        ensured_shipping: bool = True,
    ):
        if profile is None:
            profile = _baseline_profile()
        return [
            patch(f"{_BASE}._check_permission", return_value=None),
            patch(f"{_BASE}._validate_inputs", return_value=None),
            patch(f"{_BASE}._read_company_profile", return_value=profile),
            patch(f"{_BASE}._find_existing",
                  return_value=existing_supplier),
            patch(f"{_BASE}._ensure_allowed_to_transact_with",
                  return_value=added_atw),
            patch(f"{_BASE}._ensure_addresses",
                  return_value=(ensured_billing, ensured_shipping)),
            patch(f"{_BASE}._describe", return_value={
                "represents_company": "Acme",
                "allowed_to_transact_with": ["Beta"],
                "email_id": "x@y.z",
                "mobile_no": "1234567890",
                "default_currency": "INR",
                "gst_category": "Registered Regular",
                "gstin": "27ABCDE1234F1Z5",
                "addresses": [],
            }),
            patch.object(frappe.db, "commit"),
        ]

    def test_new_supplier_created_with_source_profile_mirror(self):
        """represents_company points at SOURCE (not target — opposite
        of the Customer bootstrap); profile mirrors source Company."""
        fake_doc = MagicMock()
        fake_doc.name = "Internal Supplier - Acme"

        with ExitStack() as stack:
            for p in self._stack(existing_supplier=None):
                stack.enter_context(p)
            stack.enter_context(
                patch(f"{_BASE}._default_supplier_group",
                      return_value="All Supplier Groups"))
            new_doc_mock = stack.enter_context(
                patch("frappe.new_doc", return_value=fake_doc))
            result = bootstrap_internal_supplier(
                source_company="Acme", target_company="Beta")

        new_doc_mock.assert_called_once_with("Supplier")
        fake_doc.insert.assert_called_once()
        update_payload = fake_doc.update.call_args.args[0]
        self.assertEqual(update_payload["is_internal_supplier"], 1)
        self.assertEqual(update_payload["represents_company"], "Acme")
        self.assertEqual(update_payload["default_currency"], "INR")
        self.assertEqual(
            update_payload["gst_category"], "Registered Regular")
        self.assertEqual(update_payload["gstin"], "27ABCDE1234F1Z5")
        self.assertTrue(result["created"])
        self.assertTrue(result["added_atw_row"])
        self.assertTrue(result["added_billing_address"])
        self.assertTrue(result["added_shipping_address"])

    def test_existing_supplier_returns_with_no_writes_when_complete(self):
        with ExitStack() as stack:
            for p in self._stack(
                existing_supplier="Internal Supplier - Acme",
                added_atw=False,
                ensured_billing=False,
                ensured_shipping=False,
            ):
                stack.enter_context(p)
            result = bootstrap_internal_supplier(
                source_company="Acme", target_company="Beta")

        self.assertFalse(result["created"])
        self.assertFalse(result["added_atw_row"])
        self.assertFalse(result["added_billing_address"])
        self.assertFalse(result["added_shipping_address"])

    def test_existing_supplier_repaired_when_atw_missing(self):
        with ExitStack() as stack:
            for p in self._stack(
                existing_supplier="Internal Supplier - Acme",
                added_atw=True,
                ensured_billing=False,
                ensured_shipping=False,
            ):
                stack.enter_context(p)
            result = bootstrap_internal_supplier(
                source_company="Acme", target_company="Beta")
        self.assertTrue(result["added_atw_row"])
        self.assertFalse(result["created"])

    def test_unregistered_source_propagates(self):
        fake_doc = MagicMock()
        fake_doc.name = "Internal Supplier - Acme"
        unregistered = _baseline_profile(
            gst_category="Unregistered", gstin=None)
        with ExitStack() as stack:
            for p in self._stack(existing_supplier=None,
                                 profile=unregistered):
                stack.enter_context(p)
            stack.enter_context(
                patch(f"{_BASE}._default_supplier_group",
                      return_value="All Supplier Groups"))
            stack.enter_context(
                patch("frappe.new_doc", return_value=fake_doc))
            bootstrap_internal_supplier(
                source_company="Acme", target_company="Beta")
        update_payload = fake_doc.update.call_args.args[0]
        self.assertEqual(update_payload["gst_category"], "Unregistered")
        self.assertIsNone(update_payload["gstin"])


if __name__ == "__main__":
    unittest.main()
