"""gh#28 — workflow_state must be filterable from EasyEcom Location list.

The §8A W1 acceptance test (filter discovered Locations by
workflow_state=To Map to validate the unmapped-locations worklist) was
failing because the field was hidden:1 AND in_standard_filter was not
set — so the field didn't appear in the list-view filter dropdown at
all. The Workflow badge on each row was visible, but there was no UI
path to filter by it.

Fix exposes the field via `in_standard_filter:1` while keeping
`hidden:1` on the form (the form behavior — Actions menu drives
transitions, badge in title bar shows state — is unchanged).

This test freezes the contract so any future PR that drops the filter
flag breaks here, not in the W1 acceptance run.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestLocationWorkflowStateFilter(FrappeTestCase):
    def setUp(self) -> None:
        self.meta = frappe.get_meta("EasyEcom Location")
        self.field = self.meta.get_field("workflow_state")

    def test_workflow_state_field_exists(self) -> None:
        self.assertIsNotNone(
            self.field, "workflow_state field missing on EasyEcom Location"
        )

    def test_workflow_state_in_standard_filter(self) -> None:
        self.assertEqual(
            self.field.in_standard_filter,
            1,
            "workflow_state must be in_standard_filter=1 so it appears in "
            "the list-view filter dropdown (gh#28, §8A W1 acceptance)",
        )

    def test_workflow_state_indexed_for_filter_speed(self) -> None:
        self.assertEqual(
            self.field.search_index,
            1,
            "workflow_state filter is only useful if filtering is fast — "
            "index check complements the filter check",
        )

    def test_workflow_state_remains_hidden_on_form(self) -> None:
        """Form behavior is unchanged — the field is still hidden on the
        form (Workflow badge in title bar shows state; Actions menu drives
        transitions). Only the list-view filter dropdown was missing it."""
        self.assertEqual(self.field.hidden, 1)

    def test_workflow_state_visible_in_list_view_rows(self) -> None:
        """The status column shown on each row is independent of the
        filter exposure but related — keeping this asserted ensures the
        screenshot in the issue (workflow state badges visible per row)
        stays accurate."""
        self.assertEqual(self.field.in_list_view, 1)
