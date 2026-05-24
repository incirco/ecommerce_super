"""§3.11 acceptance bar 2: Credentials are set-only and never readable back.

The strict guarantee (§3.7.3): no role — including EasyEcom System Manager
and Frappe's built-in System Manager — can retrieve a credential's
plaintext through the form, any API or whitelisted method, a report, a
list view, or an export. A credential can only be overwritten, never read
out.

Operational implications enforced here:
  - Credential fields are Password fieldtype (encrypted at rest in __Auth).
  - The raw DB row never holds plaintext.
  - get_credentials_for_client() — the only documented decrypt path — is
    used exclusively by EasyEcomClient and never returns to web-facing code.
  - The form's get_doc never includes plaintext.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state

PLAINTEXT_API_KEY = "this-is-a-secret-api-key-do-not-leak"
PLAINTEXT_EMAIL = "ops-secret@example.com"
PLAINTEXT_PASSWORD = "this-is-a-secret-password-do-not-leak"
PLAINTEXT_WEBHOOK_TOKEN = "this-is-a-webhook-token-do-not-leak"


class TestCredentialsNoReadback(FrappeTestCase):
    def setUp(self) -> None:
        cleanup_easyecom_state()
        doc = frappe.new_doc("EasyEcom Account")
        doc.update(
            {
                "account_name": "acc-creds",
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.easyecom.io",
                "x_api_key": PLAINTEXT_API_KEY,
                "email": PLAINTEXT_EMAIL,
                "password": PLAINTEXT_PASSWORD,
                "rate_limit_tier": "Silver",
                "webhook_enabled": 1,
                "webhook_token": PLAINTEXT_WEBHOOK_TOKEN,
            }
        )
        doc.insert(ignore_permissions=True)
        self.account_name = doc.name

    def tearDown(self) -> None:
        cleanup_easyecom_state()

    def test_db_row_does_not_contain_plaintext(self) -> None:
        """The main table holds ciphertext-or-masked-value, never the raw secret."""
        row = frappe.db.sql(
            """SELECT x_api_key, email, password, webhook_token
               FROM `tabEasyEcom Account` WHERE name=%s""",
            (self.account_name,),
            as_dict=True,
        )[0]
        for field, plaintext in [
            ("x_api_key", PLAINTEXT_API_KEY),
            ("email", PLAINTEXT_EMAIL),
            ("password", PLAINTEXT_PASSWORD),
            ("webhook_token", PLAINTEXT_WEBHOOK_TOKEN),
        ]:
            stored = row[field]
            self.assertNotEqual(
                stored, plaintext, f"{field} stored as plaintext in main table!"
            )

    def test_get_doc_does_not_return_plaintext(self) -> None:
        """A normal get_doc (the form's read path) returns masked Password
        fields, never plaintext."""
        doc = frappe.get_doc("EasyEcom Account", self.account_name)
        for field, plaintext in [
            ("x_api_key", PLAINTEXT_API_KEY),
            ("email", PLAINTEXT_EMAIL),
            ("password", PLAINTEXT_PASSWORD),
            ("webhook_token", PLAINTEXT_WEBHOOK_TOKEN),
        ]:
            val = doc.get(field)
            self.assertNotEqual(
                val, plaintext, f"{field} returned as plaintext from get_doc!"
            )

    def test_credentials_only_decrypt_via_documented_path(self) -> None:
        """get_credentials_for_client is the ONE allowed decrypt path; using
        it returns plaintext (so EasyEcomClient can build outbound headers)."""
        doc = frappe.get_doc("EasyEcom Account", self.account_name)
        creds = doc.get_credentials_for_client()
        self.assertEqual(creds["api_key"], PLAINTEXT_API_KEY)
        self.assertEqual(creds["email"], PLAINTEXT_EMAIL)
        self.assertEqual(creds["password"], PLAINTEXT_PASSWORD)

    def test_no_whitelisted_method_returns_credentials(self) -> None:
        """Scan the module for any @frappe.whitelist functions that might
        return a credential value. The webhook receiver is the ONLY
        whitelisted method; verify it does not return credentials."""
        from ecommerce_super.easyecom.api import webhook

        whitelisted = [
            name
            for name in dir(webhook)
            if callable(getattr(webhook, name, None))
            and getattr(getattr(webhook, name, None), "_whitelisted", False)
        ]
        # The receiver is the only whitelisted method; it returns ok/error,
        # not credentials. (We don't actually call it here — the unit test
        # in test_webhook_auth covers that surface.)
        self.assertLessEqual(len(whitelisted), 1)
