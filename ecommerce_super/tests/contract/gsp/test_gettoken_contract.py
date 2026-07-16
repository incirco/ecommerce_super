"""§11.5.1 gh#151 — Contract tests for /gettoken.

Locks the FLAT response envelope (top-level `status`/`access_token`/
`token_type`/`expires_in` — no `{message: {...}}` wrapper). EE parses
`.access_token` directly; any envelope drift breaks their integration.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ecommerce_super.easyecom.api import gsp as gsp_mod
from ecommerce_super.tests.contract.gsp._helpers import request_context


class TestGettokenSuccessShape(unittest.TestCase):
    """Success response shape must be stable — EE reads
    `.access_token` and `.expires_in` directly."""

    def test_success_returns_flat_access_token_and_expires_in(self):
        with (
            request_context({}) as cap,  # no body needed
            patch.object(
                gsp_mod, "issue_bearer",
                return_value={"token": "eyJTEST...", "expires_in": 900},
            ),
        ):
            gsp_mod.gettoken()
        response = cap.response
        # Contract-locked keys: EE parses these directly
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["access_token"], "eyJTEST...")
        self.assertEqual(response["token_type"], "Bearer")
        self.assertEqual(response["expires_in"], 900)
        # NO wrapper — must be flat
        self.assertNotIn("message", response)  # only present on failure
        self.assertNotIn("data", response)

    def test_expires_in_reflects_current_ttl(self):
        """gh#166 set TTL to 900s (was 3600s). Regression guard: any
        code path that mutates the TTL to a wildly different value
        (e.g. accidentally 60s or 86400s) should be caught here."""
        with (
            request_context({}) as cap,
            patch.object(
                gsp_mod, "issue_bearer",
                return_value={"token": "x", "expires_in": 900},
            ),
        ):
            gsp_mod.gettoken()
        response = cap.response
        # 900 == 15 min (current post-#166 TTL). Update this test AND
        # the docs/custom_gsp_contract.md §4 if the TTL changes.
        self.assertEqual(response["expires_in"], 900)


class TestGettokenAuthFailure(unittest.TestCase):
    """Contract: on auth failure, the endpoint returns 401 with a
    flat `{status, message}` shape. EE routes on status==401."""

    def test_bad_secret_returns_401_flat(self):
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            EasyEcomGSPAuthError,
        )
        with (
            request_context({}, bypass_auth=False) as cap,
            patch.object(
                gsp_mod, "validate_basic_auth",
                side_effect=EasyEcomGSPAuthError(
                    "Basic auth secret does not match"
                ),
            ),
        ):
            gsp_mod.gettoken()
        response = cap.response
        self.assertEqual(response["status"], 401)
        self.assertIn("secret", response["message"].lower())

    def test_missing_header_returns_401_flat(self):
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            EasyEcomGSPAuthError,
        )
        with (
            request_context({}, bypass_auth=False) as cap,
            patch.object(
                gsp_mod, "_get_gsp_auth_header", return_value=None,
            ),
            patch.object(
                gsp_mod, "validate_basic_auth",
                side_effect=EasyEcomGSPAuthError(
                    "Missing or malformed Authorization header"
                ),
            ),
        ):
            gsp_mod.gettoken()
        response = cap.response
        self.assertEqual(response["status"], 401)


class TestGettokenMintFailureShape(unittest.TestCase):
    """When the mint itself fails (Redis down, key missing, whatever),
    the endpoint returns 500 with a stable shape — EE should NOT see
    an unhandled 500 with a stack trace."""

    def test_mint_exception_returns_500_flat(self):
        with (
            request_context({}) as cap,
            patch.object(
                gsp_mod, "issue_bearer",
                side_effect=RuntimeError("Redis down"),
            ),
            patch.object(gsp_mod.frappe, "log_error"),
        ):
            gsp_mod.gettoken()
        response = cap.response
        self.assertEqual(response["status"], 500)
        self.assertEqual(response["message"], "Token mint failed.")
