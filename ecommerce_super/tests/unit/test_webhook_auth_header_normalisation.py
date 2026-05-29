"""gh#1 — pre-auth header rewrite for the webhook endpoint.

Frappe's validate_auth() raises AuthenticationError on requests carrying a
2-part Authorization header when the resolved user is still Guest, even
on allow_guest endpoints. SPEC §3.8 mandates accepting webhook bearer
tokens via `Authorization: Bearer ...`, so we work around Frappe's
behavior with a `before_request` hook that moves the token to the
`Access-token` header before validate_auth fires.

These tests exercise the rewrite directly against a fake request object —
no Frappe / Werkzeug needed. Wiring (the hook registration in hooks.py)
is verified by integration tests against a live webhook POST.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from ecommerce_super.easyecom.api.webhook import (
    WEBHOOK_RECEIVE_PATH,
    normalise_webhook_auth_header,
)


class _FakeRequest:
    def __init__(self, *, path: str, environ: dict) -> None:
        self.path = path
        self.environ = environ


class TestNormaliseWebhookAuthHeader(unittest.TestCase):
    def _run_with_request(self, request: _FakeRequest | None) -> None:
        with patch.object(frappe.local, "request", request, create=True):
            normalise_webhook_auth_header()

    def test_no_request_is_a_noop(self) -> None:
        # Should not raise even when frappe.local has no request attached.
        self._run_with_request(None)

    def test_non_webhook_path_is_left_alone(self) -> None:
        env = {"HTTP_AUTHORIZATION": "Bearer abc123"}
        self._run_with_request(
            _FakeRequest(path="/api/method/some.other.endpoint", environ=env)
        )
        self.assertEqual(env["HTTP_AUTHORIZATION"], "Bearer abc123")
        self.assertNotIn("HTTP_ACCESS_TOKEN", env)

    def test_bearer_auth_on_webhook_shifts_to_access_token(self) -> None:
        env = {"HTTP_AUTHORIZATION": "Bearer wh_token_xyz"}
        self._run_with_request(
            _FakeRequest(path=WEBHOOK_RECEIVE_PATH, environ=env)
        )
        self.assertNotIn("HTTP_AUTHORIZATION", env)
        self.assertEqual(env["HTTP_ACCESS_TOKEN"], "wh_token_xyz")

    def test_case_insensitive_bearer_prefix(self) -> None:
        env = {"HTTP_AUTHORIZATION": "bearer wh_token_xyz"}
        self._run_with_request(
            _FakeRequest(path=WEBHOOK_RECEIVE_PATH, environ=env)
        )
        self.assertNotIn("HTTP_AUTHORIZATION", env)
        self.assertEqual(env["HTTP_ACCESS_TOKEN"], "wh_token_xyz")

    def test_existing_access_token_wins(self) -> None:
        env = {
            "HTTP_AUTHORIZATION": "Bearer from_bearer",
            "HTTP_ACCESS_TOKEN": "from_access",
        }
        self._run_with_request(
            _FakeRequest(path=WEBHOOK_RECEIVE_PATH, environ=env)
        )
        self.assertEqual(env["HTTP_ACCESS_TOKEN"], "from_access")
        # Authorization is still removed so Frappe's auth middleware
        # doesn't trip on it.
        self.assertNotIn("HTTP_AUTHORIZATION", env)

    def test_token_auth_is_left_alone(self) -> None:
        """`Authorization: Token api_key:secret` is Frappe's own API key
        form. Not a webhook auth shape — leave it for Frappe's middleware
        to handle (the receiver will reject it downstream anyway)."""
        env = {"HTTP_AUTHORIZATION": "Token apikey:secret"}
        self._run_with_request(
            _FakeRequest(path=WEBHOOK_RECEIVE_PATH, environ=env)
        )
        self.assertEqual(env["HTTP_AUTHORIZATION"], "Token apikey:secret")
        self.assertNotIn("HTTP_ACCESS_TOKEN", env)

    def test_empty_bearer_token_is_a_noop(self) -> None:
        env = {"HTTP_AUTHORIZATION": "Bearer "}
        self._run_with_request(
            _FakeRequest(path=WEBHOOK_RECEIVE_PATH, environ=env)
        )
        self.assertEqual(env["HTTP_AUTHORIZATION"], "Bearer ")
        self.assertNotIn("HTTP_ACCESS_TOKEN", env)

    def test_missing_authorization_is_a_noop(self) -> None:
        env: dict = {}
        self._run_with_request(
            _FakeRequest(path=WEBHOOK_RECEIVE_PATH, environ=env)
        )
        self.assertNotIn("HTTP_AUTHORIZATION", env)
        self.assertNotIn("HTTP_ACCESS_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
