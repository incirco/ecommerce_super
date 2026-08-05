"""gh#252 follow-up — a DRAFT §10 IPR must NOT be reported "Receipted".

REGRESSION CONTEXT

PR #253 (per-GRN isolation + rollback-safe recording) unblocks the pull,
but it makes the failed submit's Draft Purchase Receipt PERSIST — the PR is
inserted BEFORE the submit savepoint, so rolling back the failed submit
leaves the Draft behind. The gh#234 look-back then re-pulls the failing GRN
on the next sweep.

Pre-this-fix, `process_inbound_grn`'s idempotency guard matched that Draft
(`_find_existing_ipr_for_grn` selected `docstatus IN (0, 1)`) and
unconditionally upserted the GRN Map to "Receipted" — a silent false
success: the Map flipped Failed -> Receipted while nothing was actually
received into the destination warehouse.

THE FIX

`_find_existing_ipr_for_grn` now returns ``{name, docstatus}``. The caller
reports "Receipted" ONLY for a SUBMITTED IPR (docstatus=1); for a DRAFT
(docstatus=0) it PRESERVES the existing Map status (Failed / Pending) so the
FDE signal survives. Re-submitting is owned elsewhere (the SI-submit hook
for different-GSTIN, or PR-B / manual for same-GSTIN) — never this re-pull.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows import transfer_inbound as mod


class TestDraftIprNotReceipted(unittest.TestCase):
    def _run(self, *, docstatus, existing_map_status):
        """Drive process_inbound_grn to its idempotency early-return with a
        found IPR of the given docstatus, capturing the Map upsert kwargs."""
        captured: dict = {}

        def _upsert(**kwargs):
            captured.update(kwargs)

        with (
            patch.object(mod.frappe, "get_doc", return_value=MagicMock()),
            patch.object(
                mod,
                "_find_existing_ipr_for_grn",
                return_value={"name": "PR-DRAFT-1", "docstatus": docstatus},
            ),
            patch.object(
                mod.frappe.db, "get_value", return_value=existing_map_status
            ),
            patch.object(
                mod, "_upsert_grn_map_for_transfer", side_effect=_upsert
            ),
        ):
            outcome = mod.process_inbound_grn(
                grn_row={"grn_id": 2280859},
                ee_grn_id=2280859,
                inwarded_wh_c_id=263433,
                vendor_c_id=263433,
                location_row={"mapped_warehouse": "WH - X"},
                company="MMPL",
                transfer_map_name="ECS-XFER-DL-1",
            )
        return outcome, captured

    def test_draft_ipr_preserves_failed_not_receipted(self) -> None:
        # The headline: a Draft IPR left by a failed submit must NOT flip the
        # Map to Receipted; the "Failed" signal is preserved.
        outcome, captured = self._run(docstatus=0, existing_map_status="Failed")
        self.assertEqual(outcome.grn_map_status, "Failed")
        self.assertNotEqual(outcome.grn_map_status, "Receipted")
        self.assertEqual(captured.get("status"), "Failed")
        self.assertEqual(outcome.purchase_receipt, "PR-DRAFT-1")

    def test_draft_ipr_pending_preserved(self) -> None:
        # Different-GSTIN SI-pending Draft stays "Pending", not "Receipted".
        outcome, captured = self._run(
            docstatus=0, existing_map_status="Pending"
        )
        self.assertEqual(outcome.grn_map_status, "Pending")
        self.assertEqual(captured.get("status"), "Pending")

    def test_draft_ipr_missing_map_defaults_pending(self) -> None:
        # Defensive: no Map row yet (get_value -> None) → "Pending", never
        # "Receipted".
        outcome, _ = self._run(docstatus=0, existing_map_status=None)
        self.assertEqual(outcome.grn_map_status, "Pending")

    def test_submitted_ipr_still_receipted(self) -> None:
        # Unchanged behaviour: a SUBMITTED IPR on re-pull is a genuine
        # Receipted no-op.
        outcome, captured = self._run(
            docstatus=1, existing_map_status="Receipted"
        )
        self.assertEqual(outcome.grn_map_status, "Receipted")
        self.assertEqual(captured.get("status"), "Receipted")


if __name__ == "__main__":
    unittest.main()
