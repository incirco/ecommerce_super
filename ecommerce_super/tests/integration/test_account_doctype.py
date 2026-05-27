"""§3.11 acceptance bar 1: Account config exists and is editable.

- Can create an EasyEcom Account with all mandatory fields.
- Credentials are stored encrypted (not readable in plain text from the
  desk or the DB).
- rate_limit_tier is mandatory with no preset default.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state


class TestAccountCreation(FrappeTestCase):
    def setUp(self) -> None:
        cleanup_easyecom_state()

    def tearDown(self) -> None:
        cleanup_easyecom_state()

    def test_can_create_account_with_all_mandatory_fields(self) -> None:
        doc = frappe.new_doc("EasyEcom Account")
        doc.update(
            {
                "account_name": "acc-1",
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.easyecom.io",
                "x_api_key": "key-xxxxxxx",
                "email": "ops@example.com",
                "password": "secretpw",
                "rate_limit_tier": "Silver",
                "webhook_enabled": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.name, "acc-1")
        self.assertEqual(doc.rate_limit_tier, "Silver")

    def test_rate_limit_tier_is_mandatory(self) -> None:
        """No preset default (§3.3.2). Creating an account without tier raises."""
        doc = frappe.new_doc("EasyEcom Account")
        doc.update(
            {
                "account_name": "acc-no-tier",
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.easyecom.io",
                "x_api_key": "k",
                "email": "e@x.com",
                "password": "p",
                "webhook_enabled": 0,
                # rate_limit_tier omitted on purpose
            }
        )
        with self.assertRaises((frappe.MandatoryError, frappe.ValidationError)):
            doc.insert(ignore_permissions=True)

    def test_default_tier_in_production_warns(self) -> None:
        """§3.10: 'tier still Default at go-live' is a blocking onboarding
        condition. The controller's _warn_if_default_tier_in_production
        surfaces it as a msgprint warning."""
        from frappe.utils import cstr

        # Capture msgprints by replacing frappe.msgprint temporarily.
        warnings = []
        original = frappe.msgprint

        def capture(msg, **kwargs):
            warnings.append(cstr(msg))

        try:
            frappe.msgprint = capture
            doc = frappe.new_doc("EasyEcom Account")
            doc.update(
                {
                    "account_name": "acc-prod-default",
                    "environment_badge": "Production",
                    "api_endpoint": "https://api.easyecom.io",
                    "x_api_key": "k",
                    "email": "e@x.com",
                    "password": "p",
                    "rate_limit_tier": "Default",
                    "webhook_enabled": 0,
                }
            )
            doc.insert(ignore_permissions=True)
        finally:
            frappe.msgprint = original

        self.assertTrue(
            any("Default tier" in w for w in warnings),
            f"Expected a Default-tier warning; got {warnings}",
        )

    def test_throughput_clamps_to_tier_ceiling(self) -> None:
        """§3.3.4: FDE may set max_throughput_per_sec lower than the tier
        ceiling, never above. Controller clamps."""
        warnings = []
        original = frappe.msgprint

        def capture(msg, **kwargs):
            from frappe.utils import cstr

            warnings.append(cstr(msg))

        try:
            frappe.msgprint = capture
            doc = frappe.new_doc("EasyEcom Account")
            doc.update(
                {
                    "account_name": "acc-clamp",
                    "environment_badge": "Sandbox",
                    "api_endpoint": "https://api.easyecom.io",
                    "x_api_key": "k",
                    "email": "e@x.com",
                    "password": "p",
                    "rate_limit_tier": "Silver",  # ceiling = 20 req/s
                    "max_throughput_per_sec": 100,  # over the ceiling
                    "webhook_enabled": 0,
                }
            )
            doc.insert(ignore_permissions=True)
        finally:
            frappe.msgprint = original

        self.assertEqual(doc.max_throughput_per_sec, 20)
        self.assertTrue(
            any("Clamping" in w or "Throughput Clamped" in w for w in warnings)
        )

    def test_invalid_api_endpoint_rejected(self) -> None:
        for bad in ("not-a-url", "ftp://example.com", "http://api.example.com"):
            doc = frappe.new_doc("EasyEcom Account")
            doc.update(
                {
                    "account_name": f"acc-bad-{hash(bad)}",
                    "environment_badge": "Sandbox",
                    "api_endpoint": bad,
                    "x_api_key": "k",
                    "email": "e@x.com",
                    "password": "p",
                    "rate_limit_tier": "Silver",
                }
            )
            with self.assertRaises(
                frappe.ValidationError, msg=f"Bad endpoint {bad} should be rejected"
            ):
                doc.insert(ignore_permissions=True)


class TestPasswordFieldEncryption(FrappeTestCase):
    """Regression for the cold-start blank-site finding 2026-05-27:
    Frappe's auto-encrypt-on-insert pass skips the `email` Password
    field (collides with reserved name convention), so programmatic
    creates (scripts / fixtures / factories / bench execute) end up
    with no `email` row in `__Auth`. Every subsequent EasyEcomClient
    call then fails with 'Password not found for EasyEcom Account ...
    email'. The after_insert hook on EasyEcomAccount closes the gap.

    Form-side saves on the desk work without the hook because
    Frappe's form-handler path encrypts all Password fields uniformly;
    only programmatic-create paths need the controller-level guard."""

    def setUp(self) -> None:
        cleanup_easyecom_state()

    def tearDown(self) -> None:
        cleanup_easyecom_state()

    def test_all_password_fields_round_trip_after_programmatic_insert(self) -> None:
        from frappe.utils.password import get_decrypted_password

        doc = frappe.new_doc("EasyEcom Account")
        doc.update(
            {
                "account_name": "acc-pwd-test",
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.example.com",
                "x_api_key": "xak-1234567890",
                "email": "test@example.com",
                "password": "supersecret",
                "rate_limit_tier": "Silver",
            }
        )
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        # ALL three Password fields must decrypt back to the plaintext
        # we sent — the after_insert hook re-encrypts whatever Frappe's
        # auto-pass skipped.
        self.assertEqual(
            get_decrypted_password("EasyEcom Account", "acc-pwd-test", "x_api_key"),
            "xak-1234567890",
        )
        self.assertEqual(
            get_decrypted_password("EasyEcom Account", "acc-pwd-test", "email"),
            "test@example.com",
            "email is the one Frappe's auto-encryption skips — the "
            "hook must close that gap or every programmatic create "
            "breaks the client layer",
        )
        self.assertEqual(
            get_decrypted_password("EasyEcom Account", "acc-pwd-test", "password"),
            "supersecret",
        )

    def test_hook_is_idempotent_on_resave(self) -> None:
        """Saving the doc again must not corrupt the existing encrypted
        values. The hook compares the stored value before re-writing."""
        from frappe.utils.password import get_decrypted_password

        doc = frappe.new_doc("EasyEcom Account")
        doc.update(
            {
                "account_name": "acc-pwd-idempotent",
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.example.com",
                "x_api_key": "xak-once",
                "email": "once@example.com",
                "password": "pwd-once",
                "rate_limit_tier": "Silver",
            }
        )
        doc.insert(ignore_permissions=True)
        # Save again with no field changes (form-side flow when FDE
        # opens + saves without editing). after_insert fires only on
        # insert, not on save, so this just verifies on_save doesn't
        # blow up; the prior round-trip values must survive.
        doc.reload()
        doc.save(ignore_permissions=True)

        self.assertEqual(
            get_decrypted_password("EasyEcom Account", "acc-pwd-idempotent", "email"),
            "once@example.com",
        )
        self.assertEqual(
            get_decrypted_password("EasyEcom Account", "acc-pwd-idempotent", "password"),
            "pwd-once",
        )
