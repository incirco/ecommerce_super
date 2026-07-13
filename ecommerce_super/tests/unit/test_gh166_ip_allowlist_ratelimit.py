"""gh#166 hardening — IP allowlist enforcement on Bearer usage +
inbound-endpoint rate limiter.

Locks:
  - Empty allowlist → no restriction (backwards-compat)
  - Non-str return from get_value → defensive no-op
  - Populated allowlist + matching IP → passes
  - Populated allowlist + non-matching IP → rejects
  - IPv4 CIDR support
  - No request_ip resolvable → rejects with clear message
  - Rate limit disabled (0) → no-op
  - Rate limit set, under budget → passes
  - Rate limit set, breached → raises EasyEcomGSPRateLimited
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestGh166IpAllowlist(unittest.TestCase):
    def _run(self, allowlist_value, request_ip):
        """Invoke _enforce_ip_allowlist with mocked config + request IP."""
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            _enforce_ip_allowlist,
        )
        # Mock frappe.local as SimpleNamespace so getattr(request_ip) works
        fake_local = SimpleNamespace(request_ip=request_ip)
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gsp_auth.frappe.db.get_value",
            return_value=allowlist_value,
        ), patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gsp_auth.frappe.local",
            new=fake_local,
        ):
            _enforce_ip_allowlist("test-account")

    def test_empty_allowlist_no_restriction(self):
        """Empty string → no restriction, no throw."""
        self._run("", "1.2.3.4")  # would throw if enforced
        self._run(None, "1.2.3.4")

    def test_non_str_return_defensive_no_op(self):
        """gh#166 followup: dict return from mocked get_value shouldn't
        crash with 'no source IP resolvable'."""
        self._run({"name": "x"}, "")
        self._run([1, 2, 3], "")

    def test_populated_allowlist_matching_ip_passes(self):
        self._run("54.203.10.5, 54.203.11.6", "54.203.10.5")

    def test_populated_allowlist_cidr_matches(self):
        self._run("54.203.0.0/16", "54.203.11.7")

    def test_populated_allowlist_non_matching_rejects(self):
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            EasyEcomGSPAuthError,
        )
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            self._run("54.203.10.5", "1.2.3.4")
        self.assertIn("not in the gsp_ip_allowlist", str(ctx.exception))

    def test_no_request_ip_rejects_when_allowlist_configured(self):
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            EasyEcomGSPAuthError,
        )
        with self.assertRaises(EasyEcomGSPAuthError) as ctx:
            self._run("54.203.10.5", "")
        self.assertIn("no source IP resolvable", str(ctx.exception))

    def test_malformed_allowlist_entries_are_skipped(self):
        """A typo entry mixed with valid ones — the valid ones still work."""
        # Valid IP matches → passes
        self._run("not-a-real-ip, 1.2.3.4", "1.2.3.4")
        # Only a garbage entry + non-matching IP → rejects (no valid match)
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
            EasyEcomGSPAuthError,
        )
        with self.assertRaises(EasyEcomGSPAuthError):
            self._run("not-a-real-ip", "1.2.3.4")


class TestGh166RateLimit(unittest.TestCase):
    def _run(self, limit_value, current_count, expected_after):
        """Invoke _enforce_gsp_rate_limit with mocked account limit +
        cache counter."""
        from ecommerce_super.easyecom.api.gsp import _enforce_gsp_rate_limit
        cache = MagicMock()
        cache.get_value.return_value = current_count
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.get_value",
            return_value=limit_value,
        ), patch(
            "ecommerce_super.easyecom.api.gsp.frappe.cache",
            return_value=cache,
        ):
            _enforce_gsp_rate_limit(
                endpoint="/einvoice/update",
                invoice_id="123",
                ee_account="test-account",
            )
        cache.set_value.assert_called_with(
            "ecs:gsp:ratelimit:/einvoice/update:123",
            expected_after,
            expires_in_sec=60,
        )

    def test_limit_zero_disables_enforcement(self):
        """gsp_rate_limit_per_min = 0 → no enforcement, no cache write."""
        from ecommerce_super.easyecom.api.gsp import _enforce_gsp_rate_limit
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.get_value",
            return_value=0,
        ), patch(
            "ecommerce_super.easyecom.api.gsp.frappe.cache"
        ) as cache:
            _enforce_gsp_rate_limit(
                endpoint="/einvoice/update",
                invoice_id="123",
                ee_account="test-account",
            )
            cache.assert_not_called()

    def test_under_limit_passes_and_increments(self):
        """Count 3, limit 6 → passes, increment to 4."""
        self._run(limit_value=6, current_count=3, expected_after=4)

    def test_at_limit_still_passes(self):
        """Count 5, limit 6 → increment to 6, still under."""
        self._run(limit_value=6, current_count=5, expected_after=6)

    def test_exceeds_limit_raises(self):
        """Count 6, limit 6 → increment to 7, exceeds → raise."""
        from ecommerce_super.easyecom.api.gsp import (
            _enforce_gsp_rate_limit,
            EasyEcomGSPRateLimited,
        )
        cache = MagicMock()
        cache.get_value.return_value = 6  # will increment to 7
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.get_value",
            return_value=6,
        ), patch(
            "ecommerce_super.easyecom.api.gsp.frappe.cache",
            return_value=cache,
        ):
            with self.assertRaises(EasyEcomGSPRateLimited) as ctx:
                _enforce_gsp_rate_limit(
                    endpoint="/einvoice/update",
                    invoice_id="123",
                    ee_account="test-account",
                )
            self.assertIn("Rate limit exceeded", str(ctx.exception))
            self.assertIn("7", str(ctx.exception))

    def test_missing_field_default_6(self):
        """gsp_rate_limit_per_min not migrated yet (None) → default 6."""
        cache = MagicMock()
        cache.get_value.return_value = None  # first call
        from ecommerce_super.easyecom.api.gsp import _enforce_gsp_rate_limit
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.get_value",
            return_value=None,
        ), patch(
            "ecommerce_super.easyecom.api.gsp.frappe.cache",
            return_value=cache,
        ):
            _enforce_gsp_rate_limit(
                endpoint="/einvoice/update",
                invoice_id="123",
                ee_account="test-account",
            )
        # First call increments count 0→1, sets in cache
        cache.set_value.assert_called_with(
            "ecs:gsp:ratelimit:/einvoice/update:123",
            1,
            expires_in_sec=60,
        )

    def test_field_lookup_failure_treated_as_disabled(self):
        """DB error on limit lookup → no enforcement, no crash."""
        from ecommerce_super.easyecom.api.gsp import _enforce_gsp_rate_limit
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.get_value",
            side_effect=Exception("db unavailable"),
        ):
            # Should not raise
            _enforce_gsp_rate_limit(
                endpoint="/einvoice/update",
                invoice_id="123",
                ee_account="test-account",
            )
