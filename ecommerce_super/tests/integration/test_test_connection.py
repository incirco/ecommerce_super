"""§3.11 bar 1 closure: Test Connection action works (success and failure cases).

The Test Connection button on the EasyEcom Account form calls the
whitelisted method `ecommerce_super.easyecom.api.test_connection.test_connection`.
That method should:
  - Succeed when EE accepts the credentials, returning ok=True with the
    location_key tested.
  - Fail clearly (ok=False, human-readable message) when credentials are
    bad — never surface a stack trace through the whitelist.
  - Refuse when the Account has no default_location_key configured.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.test_connection import test_connection
from ecommerce_super.tests.factories import (
    cleanup_easyecom_state,
    make_account,
    make_location,
)


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.headers: dict = {}
        self.content = b'{"x":1}'
        self.text = '{"x":1}'

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._json


class TestTestConnection(FrappeTestCase):
    def setUp(self) -> None:
        cleanup_easyecom_state()
        for key in (
            "easyecom:token-lockout:TEST-CONN-LOC",
            "easyecom:rate:test-account:TEST-CONN-LOC:tokens",
            "easyecom:rate:test-account:TEST-CONN-LOC:refilled",
        ):
            try:
                frappe.cache().delete_value(key)
            except Exception:
                pass
        self.account = make_account()
        self.location_name = make_location("TEST-CONN-LOC", is_primary=True)
        frappe.db.set_value(
            "EasyEcom Account",
            self.account,
            "default_location_key",
            self.location_name,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        cleanup_easyecom_state()
        try:
            frappe.cache().delete_value("easyecom:token-lockout:TEST-CONN-LOC")
        except Exception:
            pass

    def test_success_path_acquires_jwt(self) -> None:
        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_FakeResponse(
                200, {"jwt": "fresh.jwt.token", "expires_in": 90 * 24 * 3600}
            ),
        ):
            result = test_connection(account=self.account)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("location_key"), "TEST-CONN-LOC")
        self.assertTrue(result.get("jwt_acquired"))
        # Account connection_status updated to Connected on successful auth.
        self.assertEqual(
            frappe.db.get_value("EasyEcom Account", self.account, "connection_status"),
            "Connected",
        )

    def test_invalid_credentials_returns_clear_failure(self) -> None:
        """A 401 from /access/token must return ok=False with a human message,
        not a stack trace (§3.11 bar 1)."""
        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_FakeResponse(401, {"error": "invalid_credentials"}),
        ):
            result = test_connection(account=self.account)

        self.assertFalse(result.get("ok"))
        self.assertIn("Authentication failed", result.get("message", ""))
        self.assertEqual(result.get("error_code"), "ECS_API_AUTH_ERROR")
        # No stack trace fields.
        self.assertNotIn("traceback", str(result).lower())

    def test_missing_default_location_returns_clear_message(self) -> None:
        frappe.db.set_value(
            "EasyEcom Account", self.account, "default_location_key", None
        )
        frappe.db.commit()
        result = test_connection(account=self.account)
        self.assertFalse(result.get("ok"))
        self.assertIn("Default Location", result.get("message", ""))

    def test_nonexistent_account_returns_not_found(self) -> None:
        result = test_connection(account="does-not-exist")
        self.assertFalse(result.get("ok"))
        self.assertIn("not found", result.get("message", "").lower())
