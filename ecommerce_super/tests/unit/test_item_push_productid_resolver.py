"""Unit tests for the Update-path productId resolver.

Background — live finding 2026-06-16:
  EE's UpdateMasterProduct rejected pushes with
  "product/sku field missing" because the payload carried
  `productId: null`. Root cause: routing chose Update (Item Map had
  `ee_product_id` populated), but `_do_update` read
  `item.ecs_ee_cp_id` which was empty (either because gh#48's
  audit-framework self-defeat left the column unmaterialized, or
  because the item was pulled from EE first and only the Map row
  carried `ee_cp_id`). The fix introduces `_resolve_update_write_id`
  with a four-step fallback chain.
"""
from __future__ import annotations

import unittest

from ecommerce_super.easyecom.flows.item_push import (
    _resolve_update_write_id,
)


class _FakeItem:
    """Minimal duck-type stand-in for a Frappe Item doc. Supports
    .get() and attribute access — enough for `_resolve_update_write_id`
    which only reads via .get()."""

    def __init__(self, **fields):
        self._fields = fields

    def get(self, key, default=None):
        return self._fields.get(key, default)


class TestResolveUpdateWriteId(unittest.TestCase):
    """The fallback chain: Item.cp_id → Map.cp_id → Item.pid → Map.pid."""

    def test_item_cp_id_wins(self):
        item = _FakeItem(
            ecs_ee_cp_id="111",
            ecs_ee_product_id="222",
        )
        existing_map = {"ee_cp_id": "333", "ee_product_id": "444"}
        self.assertEqual(
            _resolve_update_write_id(item, existing_map), "111"
        )

    def test_falls_back_to_map_cp_id_when_item_cp_id_missing(self):
        item = _FakeItem(ecs_ee_cp_id=None, ecs_ee_product_id="222")
        existing_map = {"ee_cp_id": "333", "ee_product_id": "444"}
        self.assertEqual(
            _resolve_update_write_id(item, existing_map), "333"
        )

    def test_falls_back_to_item_product_id_when_no_cp_id_anywhere(self):
        """gh#48 scenario: §8d columns existed on EE side and Map.pid was
        stamped but cp_id was never captured on either side."""
        item = _FakeItem(ecs_ee_cp_id=None, ecs_ee_product_id="222")
        existing_map = {"ee_cp_id": None, "ee_product_id": "444"}
        self.assertEqual(
            _resolve_update_write_id(item, existing_map), "222"
        )

    def test_falls_back_to_map_product_id_as_last_resort(self):
        item = _FakeItem(ecs_ee_cp_id=None, ecs_ee_product_id=None)
        existing_map = {"ee_cp_id": None, "ee_product_id": "444"}
        self.assertEqual(
            _resolve_update_write_id(item, existing_map), "444"
        )

    def test_returns_none_when_everything_empty(self):
        """Caller must flag-and-stop rather than send productId=null."""
        item = _FakeItem(ecs_ee_cp_id=None, ecs_ee_product_id=None)
        existing_map = {"ee_cp_id": None, "ee_product_id": None}
        self.assertIsNone(
            _resolve_update_write_id(item, existing_map)
        )

    def test_returns_none_when_map_is_none(self):
        item = _FakeItem(ecs_ee_cp_id=None, ecs_ee_product_id=None)
        self.assertIsNone(_resolve_update_write_id(item, None))

    def test_string_id_normalized_to_string(self):
        """Numeric IDs come through as int from MySQL — resolver returns
        a string so the caller can `int(...)` it without surprises on
        already-string values."""
        item = _FakeItem(ecs_ee_cp_id=125293829)
        self.assertEqual(
            _resolve_update_write_id(item, None), "125293829"
        )

    def test_zero_and_zero_string_treated_as_empty(self):
        """EE's product IDs are positive ints — 0 / "0" indicate
        uninitialized columns, not legitimate identifiers. Treat as
        empty so the resolver falls through to the next candidate."""
        item = _FakeItem(ecs_ee_cp_id=0, ecs_ee_product_id="0")
        existing_map = {"ee_cp_id": "0", "ee_product_id": "444"}
        self.assertEqual(
            _resolve_update_write_id(item, existing_map), "444"
        )

    def test_empty_string_treated_as_empty(self):
        item = _FakeItem(ecs_ee_cp_id="", ecs_ee_product_id="")
        existing_map = {"ee_cp_id": "", "ee_product_id": "444"}
        self.assertEqual(
            _resolve_update_write_id(item, existing_map), "444"
        )


if __name__ == "__main__":
    unittest.main()
