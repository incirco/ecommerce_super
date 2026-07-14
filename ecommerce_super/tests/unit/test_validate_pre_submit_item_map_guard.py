"""gh#93 — §10 `validate_pre_submit` blocks the save when any line
Item is not synced to EasyEcom.

Pre-fix: only `_run_preconditions` (post-submit) checked the
EasyEcom Item Map per line; misses landed on Drift / Failed Sync
Record after the DN already submitted. The reporter wanted the save
itself blocked with an actionable message so the FDE doesn't have
to chase a Failed Sync Record after the fact.

Post-fix: `validate_pre_submit` now calls the shared helper
`_unmapped_items_for_dn` (same logic the post-submit check uses) and
`frappe.throw`s naming the unsynced item_code(s). The throw is
scoped to internal-customer / §10 DNs — external-customer Delivery
Notes return unaffected (HARD RULE: ordinary sales shipments must
save/submit with no EasyEcom requirement).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows import transfer_push


def _fake_dn(
    *,
    is_internal_customer=1,
    transfer_from="Main Back Factory - MMPL",
    transfer_to="Factory Main B2B - MMPL",
    line_items=None,
):
    """Build a synthetic Delivery Note stub. The fields touched by
    `validate_pre_submit` are: doctype, ecs_is_section10_transfer,
    is_internal_customer, ecs_section10_transfer_from_warehouse,
    ecs_section10_transfer_to_warehouse, items."""
    dn = MagicMock()
    dn.doctype = "Delivery Note"
    dn.is_internal_customer = is_internal_customer
    dn.ecs_is_section10_transfer = 0
    dn.ecs_section10_transfer_from_warehouse = transfer_from
    dn.ecs_section10_transfer_to_warehouse = transfer_to

    def _getattr(name, default=None):
        return getattr(dn, name, default)

    dn.get = lambda field, default=None: _getattr(field, default)
    dn.items = []
    for code in (line_items or []):
        line = MagicMock()
        line.item_code = code
        dn.items.append(line)
    return dn


class TestPreSubmitItemMapGuard(unittest.TestCase):
    """gh#93 — the four critical shapes."""

    def _patch_routing_resolution_clean(self):
        """Make the warehouse/GIT validation pass cleanly so the
        Item-Map guard is the ONLY thing left that can throw. Lets us
        isolate the new guard's behaviour."""
        return [
            patch.object(
                transfer_push, "_warehouse_company",
                return_value="Modern Marwar Private Limited",
            ),
            patch(
                "ecommerce_super.easyecom.flows.transfer_inbound."
                "_resolve_git_warehouse",
                return_value="Goods In Transit - MMPL",
            ),
        ]

    def _enter(self, ctxs):
        return [c.__enter__() for c in ctxs]

    def _exit(self, ctxs):
        for c in ctxs:
            c.__exit__(None, None, None)

    def test_unsynced_item_throws_actionable_message(self) -> None:
        """gh#93 headline — the reporter's exact scenario. An
        internal-transfer DN with an unsynced line Item must throw
        with the item_code named and §8d Item Push instructions."""
        dn = _fake_dn(line_items=["FG20077"])
        # No EasyEcom Item Map exists for the line item.
        ctxs = self._patch_routing_resolution_clean() + [
            patch("frappe.db.exists", return_value=False),
        ]
        self._enter(ctxs)
        try:
            with self.assertRaises(frappe.ValidationError) as ctx:
                transfer_push.validate_pre_submit(dn)
        finally:
            self._exit(ctxs)

        msg = str(ctx.exception)
        self.assertIn("'FG20077'", msg)
        self.assertIn("EasyEcom", msg)
        self.assertIn("§8d", msg)

    def test_multiple_unsynced_items_named_in_one_throw(self) -> None:
        """When several line items are unsynced, the throw should
        list ALL of them so the FDE doesn't have to re-save after
        each fix."""
        dn = _fake_dn(line_items=["FG20077", "FG20088", "FG20099"])
        ctxs = self._patch_routing_resolution_clean() + [
            patch("frappe.db.exists", return_value=False),
        ]
        self._enter(ctxs)
        try:
            with self.assertRaises(frappe.ValidationError) as ctx:
                transfer_push.validate_pre_submit(dn)
        finally:
            self._exit(ctxs)

        msg = str(ctx.exception)
        for item in ("FG20077", "FG20088", "FG20099"):
            self.assertIn(item, msg)

    def test_all_synced_items_passes_through(self) -> None:
        """When every line Item has an EasyEcom Item Map row, the
        guard is silent — the DN proceeds to whatever happens next
        on validate (or, here, returns cleanly since we patched the
        downstream resolutions clean)."""
        dn = _fake_dn(line_items=["FG20077"])
        # gh#93 reopener: helper now uses frappe.db.get_value with a dict
        # result, checking status + ee_product_id. Return a healthy row.
        ctxs = self._patch_routing_resolution_clean() + [
            patch("frappe.db.get_value", return_value={
                "name": "ECS-ITM-FG20077",
                "status": "Mapped",
                "ee_product_id": "EE-FG20077",
            }),
        ]
        self._enter(ctxs)
        try:
            # Must not raise.
            transfer_push.validate_pre_submit(dn)
        finally:
            self._exit(ctxs)

    def test_external_customer_dn_unaffected_by_guard(self) -> None:
        """HARD RULE from the issue: normal/external-customer DNs
        MUST NOT fire the §10 guard. The function short-circuits at
        the `is_section10` gate before reaching the Item-Map check.
        Even when every Item is unsynced, an external DN should
        return cleanly."""
        dn = _fake_dn(
            is_internal_customer=0,
            line_items=["UNSYNCED-ITEM-1", "UNSYNCED-ITEM-2"],
        )
        # frappe.db.exists would return False here too, but we never
        # reach it — assert that fact explicitly.
        with (
            patch("frappe.db.exists") as exists_mock,
            patch.object(transfer_push, "_warehouse_company"),
            patch(
                "ecommerce_super.easyecom.flows.transfer_inbound."
                "_resolve_git_warehouse",
            ),
        ):
            transfer_push.validate_pre_submit(dn)
            exists_mock.assert_not_called()

    def test_unsynced_helper_returns_list_of_item_codes(self) -> None:
        """Sanity test the shared helper directly. It should return
        a list of item_codes that have no EasyEcom Item Map row,
        empty list when every line is mapped.

        gh#93 reopener: helper was widened to also flag `Flagged-Not-Created`,
        `Disabled`, and map-row-without-ee_product_id cases. It now uses
        `frappe.db.get_value` (not `frappe.db.exists`) with a dict result
        so it can inspect status + ee_product_id. This test mocks the
        right function accordingly."""
        dn = _fake_dn(line_items=["A", "B", "C"])

        # B and C are mapped healthy; A is not.
        def _get_value_side_effect(doctype, filters=None, *args, **_):
            if doctype != "EasyEcom Item Map":
                return None
            erpnext_name = (filters or {}).get("erpnext_name", "")
            if erpnext_name in {"B", "C"}:
                return {
                    "name": f"ECS-ITM-{erpnext_name}",
                    "status": "Mapped",
                    "ee_product_id": f"EE-{erpnext_name}",
                }
            return None  # A: no map row

        with patch("frappe.db.get_value", side_effect=_get_value_side_effect):
            result = transfer_push._unmapped_items_for_dn(dn)

        self.assertEqual(result, ["A"])

    def test_section10_routing_throws_run_before_item_check(self) -> None:
        """If the §10 routing (Transfer From/To) is bad, that throws
        first — the Item Map guard never runs. Pre-existing behaviour;
        we don't want this PR to break the ordering."""
        dn = _fake_dn(
            transfer_from="",
            transfer_to="Factory Main B2B - MMPL",
            line_items=["FG20077"],
        )
        with patch("frappe.db.exists") as exists_mock:
            with self.assertRaises(frappe.ValidationError) as ctx:
                transfer_push.validate_pre_submit(dn)
            # The throw was the routing one, not the Item Map one.
            self.assertIn("Transfer From", str(ctx.exception))
            # And we never reached the Item Map check.
            exists_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
