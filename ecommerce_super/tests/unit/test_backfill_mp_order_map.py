"""Unit tests for the PR F backfill patch — retroactively creating
Marketplace Order Map + Shipment child rows for pre-PR-#273 SIs.

Covers the two core concerns:
  1. Per-SI shape derivation — _build_shipment_from_si extracts
     everything derivable from SI fields, defaults the rest cleanly
  2. Idempotency + branching decisions in _process_one_si:
     - Skip if reference_code missing
     - Skip if Marketplace Account can't be resolved
     - Skip if Shipment already exists (re-run safety)
     - Create Map + Shipment when both missing
     - Append Shipment to existing Map (split-shipment case)

Real backfill happens in prod on thousands of rows — mock-heavy
unit tests exercise the decision paths without a live DB.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.patches.v0_1 import (
    backfill_marketplace_order_map_from_existing_sis as mod,
)


def _si_row(**overrides) -> dict:
    """Default SI row (as returned by _iter_backfillable_sis SQL)."""
    base = {
        "name": "SI-2026-00001",
        "company": "Puresta Lifestyle Private Limited",
        "currency": "INR",
        "conversion_rate": 1.0,
        "ecs_marketplace": "Shopify",
        "ecs_marketplace_order_id": "SQ-440390821",
        "ecs_easyecom_invoice_id": "687108535",
        "ecs_easyecom_invoice_number": "SHOP-1001",
        "ecs_awb_number": "AWB123",
        "ecs_courier": "Delhivery",
        "ecs_payment_mode": "COD",
    }
    base.update(overrides)
    return base


# ============================================================
# _build_shipment_from_si
# ============================================================


class TestBuildShipmentFromSi(unittest.TestCase):

    def test_inr_si_auto_confirms_conversion_rate(self):
        row = mod._build_shipment_from_si(_si_row(currency="INR"))
        self.assertEqual(row["invoice_currency_code"], "INR")
        self.assertEqual(row["conversion_rate"], 1.0)
        self.assertEqual(row["conversion_rate_confirmed"], 1)

    def test_foreign_currency_leaves_unconfirmed(self):
        """Rare in the pre-PR-#276 era, but if a legacy foreign-currency
        SI is being backfilled, confirmed=0 so the sweeper's FX gate
        picks it up if it's still a Draft (already-submitted SIs
        just carry the flag informationally)."""
        row = mod._build_shipment_from_si(
            _si_row(currency="CAD", conversion_rate=68.21)
        )
        self.assertEqual(row["invoice_currency_code"], "CAD")
        self.assertEqual(row["conversion_rate"], 68.21)
        self.assertEqual(row["conversion_rate_confirmed"], 0)

    def test_conversion_rate_source_tags_backfill(self):
        """Distinguishes backfilled Shipments from freshly-populated ones."""
        row = mod._build_shipment_from_si(_si_row())
        self.assertEqual(row["conversion_rate_source"], "Backfill from existing SI")

    def test_populates_ee_identifiers_from_si_custom_fields(self):
        row = mod._build_shipment_from_si(_si_row())
        self.assertEqual(row["sales_invoice"], "SI-2026-00001")
        self.assertEqual(row["invoice_id"], "687108535")
        self.assertEqual(row["invoice_number"], "SHOP-1001")
        self.assertEqual(row["awb_number"], "AWB123")
        self.assertEqual(row["courier"], "Delhivery")

    def test_blank_fields_default_to_none_not_empty_string(self):
        """SI has no manifest_date, batch_id, IRN etc. — should be
        None (not empty strings) so Frappe stores them as NULL, not ''.
        Matters for downstream queries and JSON API responses."""
        row = mod._build_shipment_from_si(_si_row())
        self.assertIsNone(row["manifest_date"])
        self.assertIsNone(row["batch_id"])
        self.assertIsNone(row["irn"])
        self.assertIsNone(row["eway_bill_number"])
        self.assertIsNone(row["ecs_easyecom_event_type"])
        self.assertIsNone(row["original_sales_invoice"])

    def test_missing_ee_identifiers_left_none(self):
        """Defensive — SI has no ecs_easyecom_invoice_id or number."""
        row = mod._build_shipment_from_si(_si_row(
            ecs_easyecom_invoice_id=None,
            ecs_easyecom_invoice_number=None,
        ))
        self.assertIsNone(row["invoice_id"])
        self.assertIsNone(row["invoice_number"])

    def test_numeric_invoice_id_is_stringified(self):
        """SI stores ecs_easyecom_invoice_id as Data (varchar); some
        legacy rows may have it as int. Should coerce to string so
        the Shipment child's Data field accepts it."""
        row = mod._build_shipment_from_si(_si_row(ecs_easyecom_invoice_id=687108535))
        self.assertEqual(row["invoice_id"], "687108535")


# ============================================================
# _process_one_si — decision branches
# ============================================================


class TestProcessOneSi(unittest.TestCase):

    @patch.object(mod, "_shipment_exists_for_si")
    @patch.object(mod, "_resolve_marketplace_account")
    def test_skips_when_reference_code_missing(
        self, mock_ma, mock_ship
    ):
        outcome = mod._process_one_si(_si_row(ecs_marketplace_order_id=""))
        self.assertEqual(outcome, "skipped_no_reference_code")
        # Never even attempted MA / Shipment lookups
        mock_ma.assert_not_called()
        mock_ship.assert_not_called()

    @patch.object(mod, "_resolve_marketplace_account", return_value=None)
    def test_skips_when_marketplace_account_unresolvable(self, _mock):
        """Legacy SI where (marketplace, company) doesn't match any
        Marketplace Account — skip cleanly rather than fail. Ops can
        manually create the MA later + re-run the backfill."""
        outcome = mod._process_one_si(_si_row())
        self.assertEqual(outcome, "skipped_no_marketplace_account")

    @patch.object(mod, "_shipment_exists_for_si", return_value=True)
    @patch.object(mod, "_resolve_marketplace_account", return_value="ECS-MA-Test")
    def test_skips_when_already_backfilled(self, _ma, _ship):
        """Re-run safety — a Shipment row already exists for this SI."""
        outcome = mod._process_one_si(_si_row())
        self.assertEqual(outcome, "skipped_already_backfilled")

    @patch.object(mod, "_get_or_create_map")
    @patch.object(mod, "_shipment_exists_for_si", return_value=False)
    @patch.object(mod, "_resolve_marketplace_account", return_value="ECS-MA-Test")
    def test_creates_map_when_first_si_for_this_order(
        self, _ma, _ship_exists, mock_get_or_create
    ):
        """First SI for this marketplace_order_id → new Map created."""
        fake_map = MagicMock()
        fake_map.shipments = []
        fake_map.append = lambda field, row: fake_map.shipments.append(row)
        mock_get_or_create.return_value = (True, fake_map)  # was_created=True

        outcome = mod._process_one_si(_si_row())
        self.assertEqual(outcome, "created_maps")
        # Shipment child appended + Map saved
        self.assertEqual(len(fake_map.shipments), 1)
        fake_map.save.assert_called_once_with(ignore_permissions=True)

    @patch.object(mod, "_get_or_create_map")
    @patch.object(mod, "_shipment_exists_for_si", return_value=False)
    @patch.object(mod, "_resolve_marketplace_account", return_value="ECS-MA-Test")
    def test_appends_to_existing_map_for_split_shipment(
        self, _ma, _ship_exists, mock_get_or_create
    ):
        """Second SI for same marketplace_order_id (split shipment) →
        existing Map found, Shipment appended, not a new Map."""
        fake_map = MagicMock()
        # Pretend an earlier backfill iteration already added one shipment
        fake_map.shipments = [MagicMock(sales_invoice="SI-2026-00001-A")]
        fake_map.append = lambda field, row: fake_map.shipments.append(row)
        mock_get_or_create.return_value = (False, fake_map)  # was_created=False

        outcome = mod._process_one_si(_si_row(name="SI-2026-00001-B"))
        self.assertEqual(outcome, "created_shipments")
        # Now has 2 shipments — the pre-existing one + our append
        self.assertEqual(len(fake_map.shipments), 2)
        fake_map.save.assert_called_once_with(ignore_permissions=True)


# ============================================================
# _resolve_marketplace_account
# ============================================================


class TestResolveMarketplaceAccount(unittest.TestCase):

    @patch.object(
        mod.frappe.db, "get_all",
        return_value=[{"name": "ECS-MA-Puresta-Shopify"}],
    )
    def test_returns_unique_match(self, _mock):
        result = mod._resolve_marketplace_account(
            marketplace="Shopify",
            company="Puresta Lifestyle Private Limited",
        )
        self.assertEqual(result, "ECS-MA-Puresta-Shopify")

    @patch.object(mod.frappe.db, "get_all", return_value=[])
    def test_returns_none_when_no_match(self, _mock):
        result = mod._resolve_marketplace_account(
            marketplace="Shopify", company="Ghost Company",
        )
        self.assertIsNone(result)

    @patch.object(
        mod.frappe.db, "get_all",
        return_value=[
            {"name": "ECS-MA-First"},
            {"name": "ECS-MA-Second"},
        ],
    )
    def test_picks_first_when_multiple_and_warns(self, _mock):
        """Two Marketplace Accounts for the same (marketplace, company).
        Pick the first — ops can correct via manual edit on the Map."""
        result = mod._resolve_marketplace_account(
            marketplace="Shopify",
            company="Puresta Lifestyle Private Limited",
        )
        self.assertEqual(result, "ECS-MA-First")

    def test_returns_none_for_blank_marketplace(self):
        """Defensive — legacy SI with no ecs_marketplace populated."""
        self.assertIsNone(
            mod._resolve_marketplace_account(marketplace="", company="X")
        )


# ============================================================
# Post-audit HIGH-1: backfill query must exclude Credit Notes
# ============================================================


class TestBackfillSelectOptionExists(unittest.TestCase):
    """Post-audit BLOCKER-1: `_build_shipment_from_si` writes
    conversion_rate_source = "Backfill from existing SI". That value
    MUST be listed in the Select options on the DocType JSON, else
    Frappe's field validation rejects on save and the per-SI
    try/except silently no-ops the backfill on every row."""

    def test_backfill_source_value_is_in_doctype_select_options(self):
        import json
        import pathlib
        # mod.__file__ = <app>/ecommerce_super/patches/v0_1/<this>.py
        #   parents[2] = <app>/ecommerce_super/ (inner module root)
        json_path = pathlib.Path(mod.__file__).parents[2] / (
            "easyecom/doctype/easyecom_order_map_shipment/"
            "easyecom_order_map_shipment.json"
        )
        doctype = json.load(json_path.open())
        source_field = next(
            f for f in doctype["fields"]
            if f["fieldname"] == "conversion_rate_source"
        )
        options = source_field["options"].split("\n")
        # The value the backfill patch writes
        self.assertIn(
            "Backfill from existing SI", options,
            f"'Backfill from existing SI' missing from Select options — "
            f"backfill patch will silently reject every row. "
            f"Current options: {options}"
        )

    def test_backfill_actually_writes_the_source_value(self):
        """Sanity check: the constant the backfill code writes hasn't
        drifted from what the test above checks."""
        row = mod._build_shipment_from_si(_si_row())
        self.assertEqual(
            row["conversion_rate_source"],
            "Backfill from existing SI"
        )


class TestBackfillExcludesCreditNotes(unittest.TestCase):
    """The backfill SQL query must include `AND is_return = 0` so
    Draft CNs from prior backfill_mode runs don't get Shipment rows
    (their invoice_id is prefixed "CN-<n>" which the sweeper's
    int() cast rejects; CNs are already handled by the live-flow
    _upsert_marketplace_order_map at CN insert time)."""

    def test_query_string_filters_out_credit_notes(self):
        """Read the SQL string directly and assert the guard exists.
        A regression here would silently reintroduce the HIGH-1 bug."""
        import inspect
        src = inspect.getsource(mod._iter_backfillable_sis)
        # The guard must be present exactly like this
        self.assertIn("is_return = 0", src)
        self.assertIn("ecs_marketplace_order_id IS NOT NULL", src)


if __name__ == "__main__":
    unittest.main()
