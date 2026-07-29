"""gh#236 — pull-side: trust the linked Item's known stock_uom instead
of re-flagging the UOM as dirty on every pull.

REGRESSION CONTEXT

Items pushed from ERPNext stayed perpetually **Created-Flagged** on the
UOM check (`dirty/unknown accounting_unit None; substituted default UOM
'PCS' — FDE: verify or correct.`). The pull maps EE's `accounting_unit`
to `stock_uom` (gh#38), but a live GetProductMaster read on mmpl
(2026-07-29) showed `accounting_unit` **null catalogue-wide, even
immediately after a successful UpdateMasterProduct** — i.e. EasyEcom does
not persist the field. So the push-side always-send fix (#238) is a
no-op: the pulled `stock_uom` is blank forever, the pull re-substitutes
the account default and re-raises the flag, and the row never clears.

THE FIX (issue #236 option 4)

`_known_stock_uom(sku)` resolves the linked ERPNext Item's stock_uom
(via the Item Map row -> Item, the Map row -> Product Bundle wrapper
Item, or a byte-equal item_code). When EE's UOM is blank/dirty but a
linked Item already carries a valid stock_uom, the pull ADOPTS it
silently — no flag — so a Created-Flagged row flips to Mapped. A genuine
first pull of a brand-new item (no linked Item yet) still substitutes the
account default AND flags, exactly as before.

These lock the resolver's decision table — the whole of the fix's
branching logic lives in `_known_stock_uom`.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows import item_pull as mod


# A small in-memory catalogue the fake frappe.db reads from.
_UOMS = {"Nos", "PCS", "Box", "Meter"}


class _KnownUomHarness(unittest.TestCase):
    """Drive `_known_stock_uom` against a fake frappe.db so the resolver's
    branching is exercised without a bench."""

    def _resolve(
        self,
        sku: str,
        *,
        item_maps: dict | None = None,   # {ee_sku: {"erpnext_doctype","erpnext_name"}}
        bundles: dict | None = None,     # {bundle_name: new_item_code}
        items: dict | None = None,       # {item_name: stock_uom}
    ) -> str | None:
        item_maps = item_maps or {}
        bundles = bundles or {}
        items = items or {}

        def _get_value(doctype, filters, fieldname, as_dict=False):
            if doctype == "EasyEcom Item Map":
                key = filters["ee_sku"] if isinstance(filters, dict) else filters
                row = item_maps.get(key)
                if not row:
                    return None
                if as_dict:
                    d = frappe._dict(row) if hasattr(frappe, "_dict") else _AttrDict(row)
                    return d
                return [row.get(f) for f in fieldname]
            if doctype == "Product Bundle":
                return bundles.get(filters)
            if doctype == "Item":
                return items.get(filters)
            return None

        def _exists(doctype, name):
            if doctype == "Item":
                return name in items
            if doctype == "UOM":
                return name in _UOMS
            return False

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
        ):
            return mod._known_stock_uom(sku)


class TestKnownStockUom(_KnownUomHarness):
    def test_mapped_item_returns_its_uom(self) -> None:
        # The reported case: existing mapped Item, stock_uom=PCS.
        self.assertEqual(
            self._resolve(
                "FG06600-4-L",
                item_maps={"FG06600-4-L": {"erpnext_doctype": "Item",
                                           "erpnext_name": "FG20108"}},
                items={"FG20108": "PCS"},
            ),
            "PCS",
        )

    def test_no_link_returns_none(self) -> None:
        # Genuine first pull — no map row, no Item by sku → None → the
        # caller substitutes + flags, exactly as legacy behaviour.
        self.assertIsNone(self._resolve("NEW-SKU"))

    def test_auto_map_candidate_by_item_code(self) -> None:
        # No map row, but a byte-equal item_code Item exists → adopt.
        self.assertEqual(
            self._resolve("AUTO-SKU", items={"AUTO-SKU": "Box"}),
            "Box",
        )

    def test_bundle_wrapper_item_uom(self) -> None:
        # Map row -> Product Bundle; the comparable stock_uom lives on the
        # wrapper Item (new_item_code).
        self.assertEqual(
            self._resolve(
                "BUN-SKU",
                item_maps={"BUN-SKU": {"erpnext_doctype": "Product Bundle",
                                       "erpnext_name": "PB-1"}},
                bundles={"PB-1": "WRAP-ITEM"},
                items={"WRAP-ITEM": "Meter"},
            ),
            "Meter",
        )

    def test_invalid_uom_on_linked_item_returns_none(self) -> None:
        # Defensive: a linked Item whose stock_uom is not a known UOM must
        # NOT be adopted — fall back to substitute + flag.
        self.assertIsNone(
            self._resolve(
                "BAD",
                item_maps={"BAD": {"erpnext_doctype": "Item",
                                   "erpnext_name": "BADITEM"}},
                items={"BADITEM": "GARBAGE"},
            )
        )

    def test_unattached_map_row_returns_none(self) -> None:
        # A Flagged-Not-Created row (erpnext_name blank) with no Item by
        # sku must not auto-clear — resolver returns None.
        self.assertIsNone(
            self._resolve(
                "UNATT",
                item_maps={"UNATT": {"erpnext_doctype": None,
                                     "erpnext_name": None}},
            )
        )

    def test_bundle_with_missing_wrapper_returns_none(self) -> None:
        # Map row -> Product Bundle, but the bundle row can't resolve a
        # wrapper item_code → None (no crash).
        self.assertIsNone(
            self._resolve(
                "BUN-BROKEN",
                item_maps={"BUN-BROKEN": {"erpnext_doctype": "Product Bundle",
                                          "erpnext_name": "PB-GONE"}},
                bundles={},  # PB-GONE resolves to None
            )
        )


class _AttrDict(dict):
    """Minimal frappe._dict stand-in for environments where frappe._dict
    is unavailable (attribute access over dict keys)."""

    def __getattr__(self, k):  # noqa: D401
        return self.get(k)


if __name__ == "__main__":
    unittest.main()
