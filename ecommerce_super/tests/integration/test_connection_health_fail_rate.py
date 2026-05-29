"""gh#2 — connection_health fail-rate must exclude rate-limit cooldowns.

EE enforces a 60s cooldown on /access/token (§31.3.1). When the FDE clicks
Test Connection twice in quick succession, EE returns HTTP 403; we classify
that as EasyEcomRateLimitError and log it to API Call with
error_class="EasyEcomRateLimitError".

The per-minute `update_account_connection_status` cron must NOT count
those rows toward Degraded/Down — the connection is fine, the caller just
retried too soon. Counting them downgrades a healthy Connected status on a
slow Test Connection finger-mash and confuses the FDE.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.operational.connection_health import (
    update_account_connection_status,
)
from ecommerce_super.tests.factories import cleanup_easyecom_state, make_account


def _wipe_api_calls() -> None:
    for n in frappe.db.get_all("EasyEcom API Call", pluck="name"):
        try:
            frappe.delete_doc(
                "EasyEcom API Call", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _make_call(*, status: str, error_class: str | None) -> None:
    doc = frappe.new_doc("EasyEcom API Call")
    doc.update(
        {
            "endpoint": "/access/token",
            "http_method": "POST",
            "request_url": "https://api.easyecom.io/access/token",
            "status": status,
            "error_class": error_class,
            "attempted_at": frappe.utils.now_datetime(),
            "response_status": 403 if error_class == "EasyEcomRateLimitError" else 200,
            "is_foundational": 1,
        }
    )
    doc.insert(ignore_permissions=True)


class TestConnectionHealthExcludesRateLimit(FrappeTestCase):
    def setUp(self) -> None:
        cleanup_easyecom_state()
        _wipe_api_calls()
        self.account = make_account()

    def tearDown(self) -> None:
        _wipe_api_calls()
        cleanup_easyecom_state()

    def test_all_success_yields_connected(self) -> None:
        for _ in range(5):
            _make_call(status="Success", error_class=None)
        frappe.db.commit()
        self.assertEqual(update_account_connection_status(), "Connected")

    def test_rate_limit_failures_alone_stay_connected(self) -> None:
        """Five 403 cooldowns and nothing else — connection is fine."""
        _make_call(status="Success", error_class=None)
        for _ in range(5):
            _make_call(status="Failed", error_class="EasyEcomRateLimitError")
        frappe.db.commit()
        self.assertEqual(update_account_connection_status(), "Connected")

    def test_real_failure_still_downgrades(self) -> None:
        """Non-rate-limit failures still count — Down at >=20% fail rate."""
        for _ in range(2):
            _make_call(status="Success", error_class=None)
        for _ in range(8):
            _make_call(status="Failed", error_class="EasyEcomAPIError")
        frappe.db.commit()
        # 8/10 = 80% real failures → Down
        self.assertEqual(update_account_connection_status(), "Down")

    def test_mixed_real_and_rate_limit_only_counts_real(self) -> None:
        """1 real failure + many cooldowns + many successes → Degraded
        (since 1 real fail across the bucket of 10 non-cooldown rows is 10%)."""
        for _ in range(9):
            _make_call(status="Success", error_class=None)
        _make_call(status="Failed", error_class="EasyEcomAPIError")
        for _ in range(5):
            _make_call(status="Failed", error_class="EasyEcomRateLimitError")
        frappe.db.commit()
        # 1 real fail / 15 total = ~6.7% → Degraded
        self.assertEqual(update_account_connection_status(), "Degraded")
