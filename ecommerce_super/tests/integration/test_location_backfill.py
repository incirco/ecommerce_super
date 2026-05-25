"""Integration test for the §8a back-fill migration.

The patch ecommerce_super.patches.v0_1.backfill_location_workflow_state
puts pre-workflow Location rows into the right workflow state:
  - is_operational=1 + frappe_company set → Live
  - frappe_company set (not operational)    → Mapped but not Live
  - else                                    → To Map

The patch is idempotent — re-running on rows with workflow_state already
set is a no-op.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.patches.v0_1.backfill_location_workflow_state import execute as backfill
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


def _wipe_locations(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Location",
        filters={"location_key": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Location", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestBackfillLocationWorkflowState(FrappeTestCase):
    PREFIX = "BACKFILL-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        _wipe_locations(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_locations(self.PREFIX)

    def _insert_raw(
        self,
        key: str,
        *,
        is_operational: int,
        frappe_company: str | None,
    ) -> str:
        """Insert a Location with workflow_state explicitly EMPTY, so the
        back-fill is the thing under test. Use db.sql directly to bypass
        the field default and the workflow-state-derives-is_operational
        logic in validate()."""
        docname = f"ECS-LOC-{key}"
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(
            {
                "location_key": key,
                "location_name": f"Backfill {key}",
                "enabled": 1,
                "frappe_company": frappe_company,
                "workflow_state": "To Map",  # need SOMETHING to pass insert
            }
        )
        doc.insert(ignore_permissions=True)
        # Now clear workflow_state to simulate pre-workflow legacy state.
        frappe.db.set_value(
            "EasyEcom Location",
            docname,
            {"workflow_state": None, "is_operational": is_operational},
            update_modified=False,
        )
        frappe.db.commit()
        return docname

    def test_backfill_classifies_each_row_correctly(self) -> None:
        # Three legacy rows, each in a different real-world state.
        live_name = self._insert_raw(
            f"{self.PREFIX}LIVE", is_operational=1, frappe_company=self.company
        )
        mapped_name = self._insert_raw(
            f"{self.PREFIX}MAPPED", is_operational=0, frappe_company=self.company
        )
        tomap_name = self._insert_raw(
            f"{self.PREFIX}TOMAP", is_operational=0, frappe_company=None
        )

        backfill()
        frappe.db.commit()

        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", live_name, "workflow_state"),
            "Live",
        )
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", mapped_name, "workflow_state"),
            "Mapped but not Live",
        )
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", tomap_name, "workflow_state"),
            "To Map",
        )

    def test_backfill_is_idempotent(self) -> None:
        """Re-running the patch on rows already classified is a no-op."""
        live_name = self._insert_raw(
            f"{self.PREFIX}IDEM-LIVE",
            is_operational=1,
            frappe_company=self.company,
        )
        backfill()
        frappe.db.commit()
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", live_name, "workflow_state"),
            "Live",
        )
        # Run again — the patch's "only touch empty workflow_state" filter
        # should skip this row.
        backfill()
        frappe.db.commit()
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", live_name, "workflow_state"),
            "Live",
        )

    def test_backfill_leaves_already_set_rows_alone(self) -> None:
        """A row that ALREADY has workflow_state must not be reclassified
        even if the back-fill's rule would suggest a different state."""
        name = self._insert_raw(
            f"{self.PREFIX}SKIP",
            is_operational=0,
            frappe_company=self.company,
        )
        # Manually set workflow_state to Skipped (which the back-fill
        # rule would not have chosen — it would have said Mapped but not Live).
        frappe.db.set_value("EasyEcom Location", name, "workflow_state", "Skipped")
        frappe.db.commit()

        backfill()
        frappe.db.commit()

        # Still Skipped — back-fill didn't override an existing state.
        self.assertEqual(
            frappe.db.get_value("EasyEcom Location", name, "workflow_state"),
            "Skipped",
        )
