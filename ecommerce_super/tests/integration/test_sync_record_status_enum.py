"""gh#16 — SPEC §7.3 M1 binary state machine for Sync Record.status.

The per-record outcome is BINARY: a discrepancy on any line makes the
whole record Failed; there is NO 'Discrepancy', 'Partial', or
'Completed with Discrepancy' value. Drift/discrepancy context is
preserved in last_error so §22 alert routing can differentiate.

These tests freeze that contract on the field definition itself — any
future PR that re-adds a partial-success value to the options list
breaks here, not in production.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


_EXPECTED_OPTIONS = (
    "Pending",
    "Running",
    "Success",
    "Failed",
    "Cancelled",
    "AlreadySynced",
)

_DISALLOWED_OPTIONS = (
    "Discrepancy",
    "Partial",
    "Completed with Discrepancy",
)


class TestSyncRecordStatusEnum(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.meta = frappe.get_meta("EasyEcom Sync Record")
        cls.status_field = cls.meta.get_field("status")
        assert cls.status_field is not None, "status field missing on Sync Record"
        cls.options = [
            o.strip() for o in (cls.status_field.options or "").split("\n") if o.strip()
        ]

    def test_expected_options_present_in_order(self) -> None:
        self.assertEqual(self.options, list(_EXPECTED_OPTIONS))

    def test_disallowed_options_not_present(self) -> None:
        for bad in _DISALLOWED_OPTIONS:
            self.assertNotIn(
                bad,
                self.options,
                f"{bad!r} must NOT appear in status options (SPEC §7.3 M1 binary state)",
            )

    def test_constants_module_alias_resolves_to_failed(self) -> None:
        """The STATUS_DISCREPANCY constant in the flows module is kept as
        an intent alias for downstream callsites but must now resolve to
        "Failed" — otherwise existing callers would write a value the
        DocType rejects."""
        from ecommerce_super.easyecom.flows._item_sync_records import (
            STATUS_DISCREPANCY,
            STATUS_FAILED,
        )
        self.assertEqual(STATUS_DISCREPANCY, STATUS_FAILED)
        self.assertEqual(STATUS_DISCREPANCY, "Failed")
