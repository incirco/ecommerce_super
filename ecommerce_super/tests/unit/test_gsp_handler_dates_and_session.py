"""Tests for the GSP handler's SI-date healer (gh#161 v2) and the
Guest→integration-user elevation context manager (gh#166 / gh#167).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestGh161V2ReassertSiDates(unittest.TestCase):
    def test_sets_set_posting_time_when_zero(self):
        """SI drafted before the fix has set_posting_time=0; healer sets 1."""
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
            _reassert_si_dates_for_submit,
        )
        si = MagicMock()
        si.get.side_effect = lambda k, d=None: {
            "set_posting_time": 0,
            "payment_terms_template": "",
        }.get(k, d)
        si.set_posting_time = 0
        si.posting_date = "2026-07-11"
        si.due_date = "2026-07-11"
        # hasattr(si, "transaction_date") is True on MagicMock; guard applies
        si.transaction_date = None
        _reassert_si_dates_for_submit(si)
        si.db_set.assert_any_call("set_posting_time", 1, update_modified=False)

    def test_backfills_due_date_when_before_posting(self):
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
            _reassert_si_dates_for_submit,
        )
        si = MagicMock()
        si.get.side_effect = lambda k, d=None: {
            "set_posting_time": 1,
            "payment_terms_template": "",
        }.get(k, d)
        si.set_posting_time = 1
        si.posting_date = "2026-07-13"
        si.due_date = "2026-07-10"  # earlier — must be pushed forward
        si.transaction_date = "2026-07-13"
        _reassert_si_dates_for_submit(si)
        si.db_set.assert_any_call(
            "due_date", si.posting_date, update_modified=False
        )

    def test_no_op_when_already_healthy(self):
        """set_posting_time=1, dates aligned, no template → no writes."""
        from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
            _reassert_si_dates_for_submit,
        )
        si = MagicMock()
        si.get.side_effect = lambda k, d=None: {
            "set_posting_time": 1,
            "payment_terms_template": "",
        }.get(k, d)
        si.set_posting_time = 1
        si.posting_date = "2026-07-13"
        si.due_date = "2026-07-13"
        si.transaction_date = "2026-07-13"
        _reassert_si_dates_for_submit(si)
        si.db_set.assert_not_called()


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
