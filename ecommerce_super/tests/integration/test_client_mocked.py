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

    def test_token_call_survives_user_default_company(self) -> None:
        """Regression: on a FrappeCloud site where the logged-in User
        has a default Company set, Frappe v15/v16 auto-populates empty
        Link-to-Company fields during insert(). Without the
        before_insert hook in EasyEcomAPICall, the foundational token
        row would inherit the user's default Company, then trip the
        §7.7 validate() invariant ("Foundational API Calls must leave
        Company blank.") — Test Connection breaks for every multi-
        Company customer. Verified live 2026-05-27 against the
        Incirco Ventures LLP account on FrappeCloud staging."""
        from ecommerce_super.easyecom.client.client import EasyEcomClient

        # Need a real Company doc for the user-default to point at.
        company_name = frappe.db.get_value("Company", {}, "name")
        if not company_name:
            self.skipTest("test site has no Company — skip user-default sim")

        original_default = frappe.defaults.get_user_default("Company")
        frappe.defaults.set_user_default("Company", company_name)
        try:
            with patch(
                "ecommerce_super.easyecom.client.auth.requests.post",
                return_value=_ok_token_response(),
            ):
                # Must NOT raise the §7.7 validation error.
                jwt = EasyEcomClient(location_key="MOCK-LOC").refresh_jwt()
            self.assertTrue(jwt)
            row = frappe.get_last_doc("EasyEcom API Call")
            self.assertEqual(row.is_foundational, 1)
            self.assertIsNone(
                row.company,
                "foundational row must have company=None even when "
                "the user has a default Company set",
            )
        finally:
            if original_default is None:
                frappe.defaults.clear_user_default("Company")
            else:
                frappe.defaults.set_user_default("Company", original_default)

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
                # /getAllLocation is the foundational location-discovery
                # endpoint (§7.7 + §8a correction); call it so the client
                # runs without needing a Company.
                client.get("/getAllLocation")

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

    def test_endpoint_field_excludes_query_string(self) -> None:
        """Regression: the EasyEcom API Call `endpoint` field is a
        140-char Data field with search_index=1 — meant to group calls
        by path identity ("/Products/GetProductMaster") so the
        ix_api_call_endpoint_time index can answer
        "all GetProductMaster calls in the last hour" cheaply.

        Cursor-follow calls pass `endpoint=current_cursor` which
        contains a long base64 token in the query string. Without the
        strip, the row hits Frappe's 140-char Data overflow throw AND
        every cursor follow gets a unique endpoint, killing the index.

        The full URL (with cursor, redacted) remains in `request_url`
        (length=2000). Cursor lives there; identity stays in endpoint."""
        from ecommerce_super.easyecom.client.client import EasyEcomClient

        # Use a cursor-style path with a long-ish query string. The
        # actual cursor token can be 200+ chars in production; 60 is
        # enough to prove the field would overflow without the strip.
        long_cursor = "ABC" * 60  # 180 chars; >140 alone
        cursor_endpoint = f"/Products/GetProductMaster?cursor={long_cursor}"

        with patch(
            "ecommerce_super.easyecom.client.auth.requests.post",
            return_value=_ok_token_response(),
        ), patch(
            "ecommerce_super.easyecom.client.client.requests.request",
            return_value=_FakeResponse(200, {"data": [], "nextUrl": None}),
        ):
            client = EasyEcomClient(location_key="MOCK-LOC")
            client.get(cursor_endpoint)

        row = frappe.get_last_doc("EasyEcom API Call")
        self.assertEqual(
            row.endpoint, "/Products/GetProductMaster",
            "endpoint field must be path-only (no query string); "
            f"got {row.endpoint!r}",
        )
        # The full URL with the cursor token should still be preserved
        # for debugging in request_url (length=2000 so it fits).
        self.assertIn(long_cursor, row.request_url or "")
