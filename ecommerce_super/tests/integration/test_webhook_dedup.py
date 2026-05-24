"""§6.4 / §3.11 bar 12 (auth) plus the composite UNIQUE dedup invariant.

The composite (company, event_type, ee_event_id) UNIQUE index installed by
after_install is the dedup key. The webhook receiver also short-circuits
via find_duplicate() before insert for the common case; the DB UNIQUE is
the source of truth for the race-condition case.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


def _existing_company() -> str:
    return frappe.db.get_value("Company", filters={}, fieldname="name")


class TestWebhookEventDedup(FrappeTestCase):
    def setUp(self) -> None:
        self.company = _existing_company()
        # Clean up any prior webhook events under this test class.
        for name in frappe.db.get_all(
            "EasyEcom Webhook Event",
            filters={"ee_event_id": ["like", "TEST-DEDUP-%"]},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Webhook Event", name, force=True, ignore_permissions=True
            )

    def tearDown(self) -> None:
        for name in frappe.db.get_all(
            "EasyEcom Webhook Event",
            filters={"ee_event_id": ["like", "TEST-DEDUP-%"]},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Webhook Event", name, force=True, ignore_permissions=True
            )

    def _insert_event(self, ee_event_id: str, event_type: str = "manifest") -> str:
        doc = frappe.new_doc("EasyEcom Webhook Event")
        doc.update(
            {
                "company": self.company,
                "event_type": event_type,
                "ee_event_id": ee_event_id,
                "received_at": frappe.utils.now_datetime(),
                "correlation_id": "test-corr-id-1",
                "auth_header_used": "Access-token",
                "token_verified": 1,
                "allowed_ip_check": "Skipped",
                "source_ip": "127.0.0.1",
                "http_method": "POST",
                "raw_payload": "{}",
                "payload_hash": "abc123",
                "processing_state": "Pending",
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_first_insert_succeeds(self) -> None:
        name = self._insert_event("TEST-DEDUP-001")
        self.assertTrue(name)

    def test_duplicate_blocked_by_db_unique(self) -> None:
        """The composite UNIQUE (company, event_type, ee_event_id) installed
        by after_install rejects the second insert."""
        self._insert_event("TEST-DEDUP-002", event_type="manifest")
        with self.assertRaises(Exception) as ctx:
            self._insert_event("TEST-DEDUP-002", event_type="manifest")
        # MariaDB throws an IntegrityError; Frappe usually translates to
        # frappe.UniqueValidationError but the exact class can vary.
        msg = str(ctx.exception).lower()
        self.assertTrue(
            "duplicate" in msg or "unique" in msg or "1062" in msg,
            f"Expected uniqueness violation; got: {ctx.exception}",
        )

    def test_dedup_is_scoped_to_event_type(self) -> None:
        """Same ee_event_id under different event_type IS allowed."""
        self._insert_event("TEST-DEDUP-003", event_type="manifest")
        # Different event_type — must succeed.
        self._insert_event("TEST-DEDUP-003", event_type="dispatch")

    def test_find_duplicate_helper(self) -> None:
        from ecommerce_super.easyecom.doctype.easyecom_webhook_event.easyecom_webhook_event import (
            find_duplicate,
        )

        name = self._insert_event("TEST-DEDUP-004")
        self.assertEqual(
            find_duplicate(
                company=self.company,
                event_type="manifest",
                ee_event_id="TEST-DEDUP-004",
            ),
            name,
        )
        self.assertIsNone(
            find_duplicate(
                company=self.company,
                event_type="manifest",
                ee_event_id="TEST-DEDUP-NEVER-EXISTED",
            )
        )
