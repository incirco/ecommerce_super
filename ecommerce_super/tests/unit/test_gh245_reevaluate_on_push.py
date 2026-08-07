"""gh#245 — after a successful update-push, if the map row is
Created-Flagged, enqueue re_evaluate_one_product so the flag clears
without waiting for the nightly Discover Products pull.

CONTEXT

Before this fix, update-push saved the payload snapshot but never
touched the map's status/flag_reason. A Created-Flagged item stayed
Created-Flagged until the next nightly pull re-ran the flag
evaluation. Client feedback (via MMPL) was that the FDE would fix
an Item, re-push, expect the row to move to Mapped immediately,
and it didn't.

THE FIX

_maybe_enqueue_reevaluate_if_flagged runs after the successful
UpdateMasterProduct + snapshot save. It:
  - No-ops when there's no map row (defensive).
  - No-ops when the row is not Created-Flagged (avoids queue tsunami
    on bulk pushes of already-Mapped items).
  - On Created-Flagged, enqueues re_evaluate_one_product on the
    'long' queue with enqueue_after_commit so it fires only if the
    push's DB write actually landed.

The wrapper re_evaluate_from_ee_by_item is a whitelisted convenience
for the ERPNext Item form's Re-evaluate button — resolves the map
row from item_code and delegates.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.item_push import (
    _maybe_enqueue_reevaluate_if_flagged,
)


class TestMaybeEnqueueReevaluateIfFlagged(unittest.TestCase):
    def test_no_map_is_noop(self) -> None:
        """No enqueue when the row wasn't found / never mapped."""
        with patch("frappe.enqueue") as enqueue:
            _maybe_enqueue_reevaluate_if_flagged(None)
        enqueue.assert_not_called()

    def test_map_without_name_is_noop(self) -> None:
        """No enqueue when the existing_map dict lacks a name."""
        with patch("frappe.enqueue") as enqueue:
            _maybe_enqueue_reevaluate_if_flagged({"ee_product_id": "1"})
        enqueue.assert_not_called()

    def test_mapped_status_is_noop(self) -> None:
        """No enqueue for Mapped rows — nothing to clear. This is the
        guard that keeps bulk pushes of already-Mapped items from
        tsunami-ing the queue."""
        with (
            patch("frappe.db.get_value", return_value="Mapped"),
            patch("frappe.enqueue") as enqueue,
        ):
            _maybe_enqueue_reevaluate_if_flagged(
                {"name": "ECS-ITEM-MAP-0001"}
            )
        enqueue.assert_not_called()

    def test_created_flagged_status_enqueues_reevaluate(self) -> None:
        """Created-Flagged is the case that needs the auto-clear. Enqueues
        on 'long' with enqueue_after_commit so the job fires only after
        the push's DB write actually landed."""
        with (
            patch("frappe.db.get_value", return_value="Created-Flagged"),
            patch("frappe.enqueue") as enqueue,
        ):
            _maybe_enqueue_reevaluate_if_flagged(
                {"name": "ECS-ITEM-MAP-0002"}
            )
        enqueue.assert_called_once()
        args, kwargs = enqueue.call_args
        self.assertEqual(
            args[0],
            "ecommerce_super.easyecom.flows.item_pull.re_evaluate_one_product",
        )
        self.assertEqual(kwargs["item_map_name"], "ECS-ITEM-MAP-0002")
        self.assertEqual(kwargs["queue"], "long")
        self.assertTrue(kwargs["enqueue_after_commit"])
        self.assertEqual(
            kwargs["job_name"], "gh245-reevaluate-ECS-ITEM-MAP-0002"
        )

    def test_enqueue_failure_never_raises(self) -> None:
        """A queue failure must degrade gracefully — the push already
        succeeded and returning a PushOutcome must not turn into a
        500 because the async cleanup couldn't be scheduled."""
        with (
            patch("frappe.db.get_value", return_value="Created-Flagged"),
            patch("frappe.enqueue", side_effect=RuntimeError("redis down")),
            patch("frappe.log_error") as log,
            patch("frappe.get_traceback", return_value="tb"),
        ):
            # Should not raise.
            _maybe_enqueue_reevaluate_if_flagged(
                {"name": "ECS-ITEM-MAP-0003"}
            )
        log.assert_called_once()


class TestReEvaluateFromEeByItem(unittest.TestCase):
    """gh#245 wrapper — whitelisted convenience for the Item form's
    Re-evaluate button. Resolves the map row from item_code and
    delegates to re_evaluate_one_product."""

    def test_empty_item_code_returns_error(self) -> None:
        from ecommerce_super.easyecom.flows.item_pull import (
            re_evaluate_from_ee_by_item,
        )

        result = re_evaluate_from_ee_by_item("")
        self.assertFalse(result["ok"])
        self.assertIn("item_code required", result["message"])

    def test_no_map_returns_actionable_message(self) -> None:
        from ecommerce_super.easyecom.flows.item_pull import (
            re_evaluate_from_ee_by_item,
        )

        with patch("frappe.db.get_value", return_value=None):
            result = re_evaluate_from_ee_by_item("ITEM-NEVER-PUSHED")
        self.assertFalse(result["ok"])
        self.assertIn("push the Item to EasyEcom first", result["message"])

    def test_map_found_delegates_to_re_evaluate_one_product(self) -> None:
        """Wrapper must delegate to re_evaluate_one_product using the
        resolved map name, and return its result verbatim."""
        from ecommerce_super.easyecom.flows import item_pull as mod

        with (
            patch("frappe.db.get_value", return_value="ECS-ITEM-MAP-0042"),
            patch.object(
                mod, "re_evaluate_one_product",
                return_value={"ok": True, "status": "Mapped"},
            ) as delegate,
        ):
            result = mod.re_evaluate_from_ee_by_item("ITEM-042")

        delegate.assert_called_once_with("ECS-ITEM-MAP-0042")
        self.assertEqual(result, {"ok": True, "status": "Mapped"})


if __name__ == "__main__":
    unittest.main()
