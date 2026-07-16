"""Tests for the Guest→integration-user elevation context manager
(gh#166 / gh#167) and gh#205 regression guards.

Historical note: this module used to test the `_reassert_si_dates_for_submit`
healer (gh#161 v2). That healer was deleted in gh#205 part 2 (2026-07-16)
after the one-shot migration patch `heal_gh205_pre_fix_draft_si_dates`
took over the same responsibility. Its behavioral tests were removed
alongside; the gh#205 regression guard (never set `transaction_date` on
SI, static-source inspection) is retained below.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestGh205NoTransactionDateOnSi(unittest.TestCase):
    """gh#205 regression — Sales Invoice does NOT have a native
    transaction_date field (that field belongs to Sales Order). The
    prior code set `si.transaction_date = si.posting_date` in the
    mirror, which was either a silent no-op on standard sites or a
    shadow field with no ERPNext-level effect on sites where someone
    had added the custom field. Locked below via static-source check
    so a future contributor doesn't copy-paste from SO code and
    reintroduce the fake field.
    """

    def test_mirror_does_not_set_transaction_date_on_si(self):
        """Read the mirror source directly and assert no
        `si.transaction_date =` assignment exists. Static-check style
        because instantiating the full mirror requires a whole SO +
        Customer + Item Map graph that's overkill for this guard."""
        import inspect
        from ecommerce_super.easyecom.flows.b2b_sales import invoice_mirror
        src = inspect.getsource(invoice_mirror.mirror_si_from_ee_response)
        # Look for assignment patterns that would reintroduce the bug.
        # `si.transaction_date =` (with any whitespace) is the smell.
        import re
        pattern = re.compile(r"\bsi\.transaction_date\s*=")
        matches = pattern.findall(src)
        self.assertEqual(
            matches, [],
            f"gh#205 regression: found {len(matches)} assignment(s) to "
            f"si.transaction_date in mirror_si_from_ee_response. That field "
            f"is not native to Sales Invoice (belongs to Sales Order). "
            f"Use `set_posting_time = 1` + `payment_terms_template = ''` "
            f"instead."
        )


class TestGh166ElevatedSession(unittest.TestCase):
    def test_guest_gets_elevated_to_integration_user(self):
        from ecommerce_super.easyecom.api.gsp import _elevated_session
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.session"
        ) as sess, patch(
            "ecommerce_super.easyecom.api.gsp.frappe.set_user"
        ) as set_user, patch(
            "ecommerce_super.easyecom.api.gsp._resolve_elevation_target",
            return_value="easyecom-integration@internal.local",
        ):
            sess.user = "Guest"
            with _elevated_session():
                pass
            set_user.assert_any_call("easyecom-integration@internal.local")
            # Restore back to Guest in finally
            set_user.assert_any_call("Guest")

    def test_non_guest_session_not_elevated(self):
        """API-key-authed smoke test as System Manager → no swap."""
        from ecommerce_super.easyecom.api.gsp import _elevated_session
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.session"
        ) as sess, patch(
            "ecommerce_super.easyecom.api.gsp.frappe.set_user"
        ) as set_user:
            sess.user = "admin@example.com"
            with _elevated_session():
                pass
            set_user.assert_not_called()

    def test_falls_back_to_administrator_when_integration_user_missing(self):
        """On sites where the patch hasn't run yet → Administrator."""
        from ecommerce_super.easyecom.api.gsp import _resolve_elevation_target
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.exists",
            return_value=False,
        ):
            self.assertEqual(_resolve_elevation_target(), "Administrator")

    def test_prefers_integration_user_when_available(self):
        from ecommerce_super.easyecom.api.gsp import _resolve_elevation_target
        with patch(
            "ecommerce_super.easyecom.api.gsp.frappe.db.exists",
            return_value=True,
        ):
            self.assertEqual(
                _resolve_elevation_target(),
                "easyecom-integration@internal.local",
            )
