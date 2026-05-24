"""Integration tests for §6.5.3 Cursor Rewind FDE surface — the
whitelisted action and the System Manager role gate."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_sync_cursor.easyecom_sync_cursor import (
    rewind_cursor,
)
from ecommerce_super.tests.factories import (
    cleanup_easyecom_state,
    make_location,
)


def _make_cursor(*, company: str, location_key: str, resource: str, value: str) -> str:
    """Build a Sync Cursor for the test. Uses a deterministic name
    that matches the autoname format so cleanup is straightforward."""
    doc = frappe.new_doc("EasyEcom Sync Cursor")
    doc.update(
        {
            "company": company,
            "location_key": location_key,
            "resource": resource,
            "cursor_value": value,
            "cursor_format": "ISO Datetime",
            "last_advanced_at": frappe.utils.now_datetime(),
            "last_advanced_by": "System",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _make_test_user(*, roles: list[str]) -> str:
    """Create a throw-away user with the given roles for permission tests."""
    email = f"rewind-{'-'.join(sorted(roles)).lower().replace(' ', '_')}@test.local"
    if frappe.db.exists("User", email):
        frappe.delete_doc("User", email, force=True, ignore_permissions=True)
    user = frappe.new_doc("User")
    user.update(
        {
            "email": email,
            "first_name": "Rewind",
            "last_name": "Tester",
            "send_welcome_email": 0,
            "enabled": 1,
        }
    )
    user.insert(ignore_permissions=True)
    for role in roles:
        user.append("roles", {"role": role})
    user.save(ignore_permissions=True)
    return email


class TestCursorRewindPermissionGate(FrappeTestCase):
    LOC_KEY = "TEST-REWIND-LOC"
    # make_location creates a doc named f"ECS-LOC-{location_key}"; the
    # Sync Cursor's `location_key` field is a Link to EasyEcom Location
    # validating by docname, so we use the docname here.
    LOC_DOCNAME = "ECS-LOC-TEST-REWIND-LOC"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cleanup_easyecom_state()
        make_location(location_key=cls.LOC_KEY)

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_easyecom_state()
        super().tearDownClass()

    def setUp(self) -> None:
        self._wipe_cursors()
        self._original_user = frappe.session.user

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        self._wipe_cursors()

    def _wipe_cursors(self) -> None:
        for n in frappe.db.get_all("EasyEcom Sync Cursor", pluck="name"):
            try:
                frappe.delete_doc(
                    "EasyEcom Sync Cursor", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        frappe.db.commit()

    def test_system_manager_can_rewind(self) -> None:
        cursor_name = _make_cursor(
            company="_Test Company",
            location_key=self.LOC_DOCNAME,
            resource="orders",
            value="2026-05-20T10:00:00",
        )
        # Administrator has System Manager.
        result = rewind_cursor(
            cursor_name=cursor_name,
            to_value="2026-05-15T00:00:00",
            reason="Test rewind for verifying SM gate.",
        )
        self.assertEqual(result["before_value"], "2026-05-20T10:00:00")
        self.assertEqual(result["after_value"], "2026-05-15T00:00:00")
        # Sync Cursor reflects new value.
        cur = frappe.get_doc("EasyEcom Sync Cursor", cursor_name)
        self.assertEqual(cur.cursor_value, "2026-05-15T00:00:00")
        self.assertEqual(cur.last_advanced_by, "FDE Rewind")

    def test_non_system_manager_rejected(self) -> None:
        cursor_name = _make_cursor(
            company="_Test Company",
            location_key=self.LOC_DOCNAME,
            resource="grns",
            value="2026-05-20T10:00:00",
        )
        non_sm_email = _make_test_user(roles=["EasyEcom FDE"])
        frappe.set_user(non_sm_email)
        try:
            with self.assertRaises(frappe.PermissionError):
                rewind_cursor(
                    cursor_name=cursor_name,
                    to_value="2026-05-15T00:00:00",
                    reason="should fail",
                )
        finally:
            frappe.set_user(self._original_user)
            if frappe.db.exists("User", non_sm_email):
                frappe.delete_doc(
                    "User", non_sm_email, force=True, ignore_permissions=True
                )

        # Cursor unchanged.
        cur = frappe.get_doc("EasyEcom Sync Cursor", cursor_name)
        self.assertEqual(cur.cursor_value, "2026-05-20T10:00:00")

    def test_empty_reason_rejected(self) -> None:
        """§2.7 no silent rewinds — reason is mandatory."""
        cursor_name = _make_cursor(
            company="_Test Company",
            location_key=self.LOC_DOCNAME,
            resource="returns",
            value="2026-05-20T10:00:00",
        )
        with self.assertRaises(frappe.ValidationError):
            rewind_cursor(
                cursor_name=cursor_name,
                to_value="2026-05-15T00:00:00",
                reason="",  # empty
            )

    def test_empty_to_value_rejected(self) -> None:
        cursor_name = _make_cursor(
            company="_Test Company",
            location_key=self.LOC_DOCNAME,
            resource="inventory",
            value="2026-05-20T10:00:00",
        )
        with self.assertRaises(frappe.ValidationError):
            rewind_cursor(
                cursor_name=cursor_name,
                to_value="",
                reason="real reason",
            )
