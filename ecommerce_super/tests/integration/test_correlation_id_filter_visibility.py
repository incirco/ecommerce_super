"""gh#18 — correlation_id must be filterable from the standard list
view filter dropdown on every log DocType.

SPEC §6.2 requires a single correlation_id to thread through API Call,
Sync Record, Queue Job, and Webhook Event for one logical operation.
The FDE-visible contract is "filter each log list by correlation_id to
trace end-to-end" — but the field had `search_index=1` (DB-fast) and
`in_standard_filter=0` (UI-invisible). The FDE could not see or
filter on correlation_id without manually adding a custom filter,
defeating the traceability promise.

These tests freeze the contract on the field meta — any future PR
that flips off the filter flag breaks here, not in production.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


_LOG_DOCTYPES = (
    "EasyEcom API Call",
    "EasyEcom Sync Record",
    "EasyEcom Queue Job",
    "EasyEcom Webhook Event",
)


class TestCorrelationIdFilterVisibility(FrappeTestCase):
    def test_correlation_id_in_standard_filter_on_every_log_doctype(self) -> None:
        for dt in _LOG_DOCTYPES:
            with self.subTest(doctype=dt):
                meta = frappe.get_meta(dt)
                field = meta.get_field("correlation_id")
                self.assertIsNotNone(
                    field, f"correlation_id missing on {dt}"
                )
                self.assertEqual(
                    field.in_standard_filter,
                    1,
                    f"correlation_id on {dt} must be in_standard_filter=1 "
                    f"so the FDE can filter from the list view",
                )

    def test_correlation_id_remains_indexed_for_query_speed(self) -> None:
        """The filter UX is only useful if filtering is fast — index check
        complements the filter check."""
        for dt in _LOG_DOCTYPES:
            with self.subTest(doctype=dt):
                meta = frappe.get_meta(dt)
                field = meta.get_field("correlation_id")
                self.assertEqual(
                    field.search_index,
                    1,
                    f"correlation_id on {dt} must keep search_index=1",
                )

    def test_api_call_correlation_id_visible_in_list_view(self) -> None:
        """API Call is the highest-volume log and the one the user cited
        explicitly — the column itself must be visible in the list so a
        correlation_id can be spotted without opening each row."""
        meta = frappe.get_meta("EasyEcom API Call")
        field = meta.get_field("correlation_id")
        self.assertEqual(field.in_list_view, 1)
