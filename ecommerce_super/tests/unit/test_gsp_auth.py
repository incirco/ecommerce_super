"""§11.5.1 Mode 1 — GSP auth helper tests.

Token mint + hash storage + Bearer validation + Basic auth verification.
Mock frappe DB calls so tests run without bench dependencies."""
from __future__ import annotations

import base64
import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
    EasyEcomGSPAuthError,
    TOKEN_TTL_SECONDS,
    _hash_token,
    issue_bearer,
    validate_basic_auth,
    validate_bearer,
)


def _basic_header(user: str, password: str) -> str:
    """Build an HTTP Basic auth header."""
    cred = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(cred).decode("ascii")


class TestHashToken(unittest.TestCase):
    def test_deterministic(self):
        a = _hash_token("abc123")
        b = _hash_token("abc123")
        self.assertEqual(a, b)

    def test_different_inputs_different_outputs(self):
        self.assertNotEqual(_hash_token("a"), _hash_token("b"))

    def test_hash_is_hex_64_chars(self):
        h = _hash_token("anything")
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class TestValidateBasicAuth(unittest.TestCase):

    def test_returns_account_on_matching_secret(self):
        with (
            patch("frappe.db.get_all", return_value=[
                {"name": "Thuraya Fashion"},
            ]),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_auth.get_decrypted_password",
                return_value="correct-secret",
            ),
        ):
            account = validate_basic_auth(_basic_header("anyone", "correct-secret"))
        self.assertEqual(account, "Thuraya Fashion")

    def test_raises_on_no_matching_secret(self):
        with (
            patch("frappe.db.get_all", return_value=[
                {"name": "Thuraya Fashion"},
            ]),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_auth.get_decrypted_password",
                return_value="actual-secret",
            ),
        ):
            with self.assertRaises(EasyEcomGSPAuthError) as ctx:
                validate_basic_auth(_basic_header("u", "wrong-secret"))
            self.assertIn("matched", str(ctx.exception))

    def test_raises_on_missing_header(self):
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            validate_basic_auth(None)
        self.assertIn("Missing", str(ctx.exception))

    def test_raises_on_non_basic_scheme(self):
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            validate_basic_auth("Bearer some-token-here")
        self.assertIn("Basic", str(ctx.exception))

    def test_raises_on_malformed_base64(self):
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            validate_basic_auth("Basic NOT-VALID-BASE64=!")
        self.assertIn("decode", str(ctx.exception).lower())

    def test_raises_on_missing_colon_in_decoded(self):
        # Decoded credential has no colon (no user:password split)
        bad = "Basic " + base64.b64encode(b"no-colon-here").decode("ascii")
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            validate_basic_auth(bad)
        self.assertIn("user:password", str(ctx.exception))

    def test_matches_against_multiple_accounts(self):
        """When multiple EE Accounts enabled, /gettoken should match
        against any of them whose secret matches."""
        def fake_decrypt(doctype, name, fieldname, raise_exception=False):
            return {"AccountA": "secret-a", "AccountB": "secret-b"}.get(name)

        with (
            patch("frappe.db.get_all", return_value=[
                {"name": "AccountA"}, {"name": "AccountB"},
            ]),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_auth.get_decrypted_password",
                side_effect=fake_decrypt,
            ),
        ):
            self.assertEqual(
                validate_basic_auth(_basic_header("u", "secret-b")),
                "AccountB",
            )

    def test_skips_accounts_with_no_secret_configured(self):
        """Account exists but gsp_basic_auth_secret blank → skip (Mode 1
        disabled for that account)."""
        def fake_decrypt(doctype, name, fieldname, raise_exception=False):
            return None if name == "DisabledAccount" else "active-secret"

        with (
            patch("frappe.db.get_all", return_value=[
                {"name": "DisabledAccount"},
                {"name": "ActiveAccount"},
            ]),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_auth.get_decrypted_password",
                side_effect=fake_decrypt,
            ),
        ):
            self.assertEqual(
                validate_basic_auth(_basic_header("u", "active-secret")),
                "ActiveAccount",
            )


class TestIssueBearer(unittest.TestCase):

    def test_returns_token_expires_in_one_hour(self):
        fake_doc = MagicMock()
        fake_doc.insert = MagicMock()
        with (
            patch("frappe.new_doc", return_value=fake_doc),
            patch("frappe.db.commit"),
        ):
            result = issue_bearer("Thuraya Fashion")

        self.assertIn("token", result)
        self.assertEqual(result["expires_in"], TOKEN_TTL_SECONDS)
        self.assertEqual(result["expires_in"], 3600)
        # Token is 64-char hex
        self.assertEqual(len(result["token"]), 64)

    def test_persists_hash_not_plaintext(self):
        fake_doc = MagicMock()
        with (
            patch("frappe.new_doc", return_value=fake_doc),
            patch("frappe.db.commit"),
        ):
            result = issue_bearer("Thuraya Fashion")

        # Persisted token_hash is SHA-256 of the plaintext token
        self.assertEqual(fake_doc.token_hash, _hash_token(result["token"]))
        self.assertNotEqual(fake_doc.token_hash, result["token"])

    def test_persists_account_link(self):
        fake_doc = MagicMock()
        with (
            patch("frappe.new_doc", return_value=fake_doc),
            patch("frappe.db.commit"),
        ):
            issue_bearer("Thuraya Fashion")

        self.assertEqual(fake_doc.easyecom_account, "Thuraya Fashion")


class TestValidateBearer(unittest.TestCase):

    def test_returns_account_on_valid_token(self):
        from frappe.utils import add_to_date, now_datetime
        future = add_to_date(now_datetime(), seconds=3600)
        with (
            patch("frappe.db.get_value", return_value={
                "name": "TOKEN-HASH-XYZ",
                "easyecom_account": "Thuraya Fashion",
                "expires_at": future,
            }),
            patch("frappe.db.set_value"),
            patch("frappe.db.commit"),
        ):
            account = validate_bearer("Bearer abc123token456")

        self.assertEqual(account, "Thuraya Fashion")

    def test_raises_on_missing_header(self):
        with self.assertRaises(EasyEcomGSPAuthError):
            validate_bearer(None)

    def test_raises_on_non_bearer_scheme(self):
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            validate_bearer("Basic some-base64-here")
        self.assertIn("Bearer", str(ctx.exception))

    def test_raises_on_unknown_token(self):
        with patch("frappe.db.get_value", return_value=None):
            with self.assertRaises(EasyEcomGSPAuthError) as ctx:
                validate_bearer("Bearer never-issued-token")
            self.assertIn("invalid", str(ctx.exception).lower())

    def test_raises_on_expired_token(self):
        from frappe.utils import add_to_date, now_datetime
        past = add_to_date(now_datetime(), seconds=-3600)  # 1hr ago
        with patch("frappe.db.get_value", return_value={
            "name": "TOKEN-HASH-EXPIRED",
            "easyecom_account": "Thuraya Fashion",
            "expires_at": past,
        }):
            with self.assertRaises(EasyEcomGSPAuthError) as ctx:
                validate_bearer("Bearer expired-token")
            self.assertIn("expired", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
