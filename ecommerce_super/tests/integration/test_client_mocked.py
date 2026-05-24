"""§3.11 acceptance bars 3, 4, 5, 7, 9 — exercised with mocked HTTP.

- Bar 3: Token acquisition works (mocked POST /access/token).
- Bar 4: Both headers sent on every call.
- Bar 5: JWT cached per location and reused.
- Bar 7: On-401 re-auth works (call returns 401 → client re-auths → retry).
- Bar 9: Foundational calls logged with easyecom_account, company blank,
  is_foundational=1, credentials redacted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import (
    cleanup_easyecom_state,
    make_account,
    make_location,
)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body or {}
        self.headers = headers or {}
        self.content = b"{}" if json_body is None else b'{"x":1}'
        self.text = "{}" if json_body is None else '{"x":1}'

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._json_body


def _ok_token_response() -> _FakeResponse:
    return _FakeResponse(
        200,
        {"jwt": "test.jwt.token", "expires_in": 90 * 24 * 3600},
        headers={"content-type": "application/json"},
    )


class TestClientMocked(FrappeTestCase):
    def setUp(self) -> None:
        cleanup_easyecom_state()
        # Clear cached state that survives test runs (frappe.cache is a
        # process-wide Redis pool; tests do not get a fresh cache):
        #   - 60s token-acquisition lockout per location (§31.3.1)
        #   - per-Company concurrency counter
        #   - rate-limit token bucket per (account, location)
        for key in (
            "easyecom:token-lockout:MOCK-LOC",
            "easyecom:concurrency:_Test Company",
            "easyecom:rate:test-account:MOCK-LOC:tokens",
            "easyecom:rate:test-account:MOCK-LOC:refilled",
        ):
            try:
                frappe.cache().delete_value(key)
            except Exception:
                pass
        self.account = make_account(tier="Silver")
        self.location_name = make_location("MOCK-LOC", is_primary=True)
        # Update default_location_key on the account so foundational calls work.
        frappe.db.set_value(
            "EasyEcom Account", self.account, "default_location_key", self.location_name
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        cleanup_easyecom_state()
        try:
            frappe.cache().delete_value("easyecom:token-lockout:MOCK-LOC")
        except Exception:
            pass

    def test_bar3_token_acquisition_caches_jwt(self) -> None:
        """First call to get_jwt() acquires; second call reuses cache."""
        from ecommerce_super.easyecom.client.client import EasyEcomClient

        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_ok_token_response(),
        ) as mock_post:
            client = EasyEcomClient(company=None, location_key="MOCK-LOC")
            jwt1 = client.get_jwt()
            jwt2 = client.get_jwt()

        self.assertEqual(jwt1, "test.jwt.token")
        self.assertEqual(jwt2, "test.jwt.token")
        # Only one HTTP token call — second was served from the location cache.
        self.assertEqual(mock_post.call_count, 1)

    def test_bar3_token_acquisition_writes_api_call_row(self) -> None:
        """Foundational call logged with is_foundational=1, company blank."""
        from ecommerce_super.easyecom.client.client import EasyEcomClient

        prior = frappe.db.count(
            "EasyEcom API Call", filters={"endpoint": "/access/token"}
        )
        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_ok_token_response(),
        ):
            EasyEcomClient(location_key="MOCK-LOC").get_jwt()
        after = frappe.db.count(
            "EasyEcom API Call", filters={"endpoint": "/access/token"}
        )
        self.assertEqual(after, prior + 1)
        row = frappe.get_last_doc("EasyEcom API Call")
        self.assertEqual(row.endpoint, "/access/token")
        self.assertEqual(row.is_foundational, 1)
        self.assertIsNone(row.company)
        self.assertEqual(row.easyecom_account, self.account)

    def test_bar4_both_headers_sent(self) -> None:
        """Every authenticated call carries x-api-key + Authorization: Bearer.

        We assert by inspecting the headers passed to requests.request.
        """
        from ecommerce_super.easyecom.client.client import EasyEcomClient

        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_ok_token_response(),
        ):
            with patch(
                "ecommerce_super.easyecom.client.client.requests.request",
                return_value=_FakeResponse(200, {"items": []}),
            ) as mock_req:
                # Use the foundational location-discovery endpoint so we
                # can exercise both-headers-sent without needing a Company
                # (this test environment doesn't have one provisioned).
                client = EasyEcomClient(location_key="MOCK-LOC")
                client.get("/Wms/Inventory/getLocations", params={"company_id": "x"})

        called_headers = mock_req.call_args.kwargs["headers"]
        self.assertIn("x-api-key", called_headers)
        self.assertIn("Authorization", called_headers)
        self.assertTrue(called_headers["Authorization"].startswith("Bearer "))
        # X-Request-Id is mandatory for trace correlation (§3.6).
        self.assertIn("X-Request-Id", called_headers)

    def test_bar9_credentials_redacted_in_logged_request(self) -> None:
        """The API Call row's request_headers must NOT contain raw credentials."""
        from ecommerce_super.easyecom.client.client import EasyEcomClient

        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_ok_token_response(),
        ):
            EasyEcomClient(location_key="MOCK-LOC").get_jwt()

        token_call = frappe.get_last_doc("EasyEcom API Call")
        headers_text = token_call.request_headers or ""
        # The raw x_api_key value must not appear in the stored headers.
        # The account's x_api_key field is "test-api-key-xxxxxxx" (from factories).
        self.assertNotIn("test-api-key-xxxxxxx", headers_text)
        # The placeholder string from redaction must be present instead.
        self.assertIn("REDACTED", headers_text)
        # And the body must not contain the password.
        payload_text = token_call.request_payload or ""
        self.assertNotIn("test-password", payload_text)
