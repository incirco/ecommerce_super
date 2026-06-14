"""gh#52 — Discover Product pull must not mutate stock_uom on an
existing Item, and Failed Sync Records must preserve the EE payload.

Reporter observed (mmpl16, 2026-06-13):
- ECS-SR-2026-06-09-121041 for Item FB15427-39 failed with
  `ValidationError: Default Unit of Measure for Item FB15427-39 cannot
  be changed directly because you have already made some transaction(s)
  with another UOM.`
- No EE API Call rows materialized — failure happened BEFORE the
  /Products/GetProductMaster call.
- Last Request Payload on the Sync Record was None — no EE context
  preserved, root-cause analysis impossible.

Two coordinated fixes:
- `_refresh_existing_item` no longer writes stock_uom (preserves the
  existing UOM; FDE-side correction is the right channel for UOM
  divergence on transacted Items).
- `_on_failure` passes the EE product dict through to
  `write_item_pull_sync_record(request_payload=product)`, which writes
  it to `last_request_payload`.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestRefreshExistingItemSkipsStockUom(unittest.TestCase):
    def test_stock_uom_not_in_update_payload(self) -> None:
        """The headline gh#52 fix — _refresh_existing_item must drop
        stock_uom from the update dict so ERPNext's transacted-Item
        validation never fires during a pull refresh."""
        from ecommerce_super.easyecom.flows.item_pull import _refresh_existing_item

        item = MagicMock()
        item.item_code = "FB15427-39"
        captured_updates: dict = {}
        item.update = lambda d: captured_updates.update(d)

        erpnext_fields = {
            "item_code": "FB15427-39",
            "item_name": "Updated name",
            "stock_uom": "Nos",  # The dirty fallback substitution.
            "description": "Refreshed description",
            "ecs_ee_product_id": "EE-PROD-9001",
        }

        _refresh_existing_item(item, erpnext_fields)

        # stock_uom and item_code must NOT appear in the update payload.
        self.assertNotIn("stock_uom", captured_updates)
        self.assertNotIn("item_code", captured_updates)
        # Other fields still flow through.
        self.assertEqual(captured_updates.get("item_name"), "Updated name")
        self.assertEqual(captured_updates.get("description"), "Refreshed description")
        self.assertEqual(captured_updates.get("ecs_ee_product_id"), "EE-PROD-9001")
        item.save.assert_called_once_with(ignore_permissions=True)

    def test_none_values_still_filtered(self) -> None:
        """Verify the existing None-skip semantic still works after the
        stock_uom drop. None values are an explicit EE-momentarily-
        dropped signal and must not NULL out existing item fields."""
        from ecommerce_super.easyecom.flows.item_pull import _refresh_existing_item

        item = MagicMock()
        captured_updates: dict = {}
        item.update = lambda d: captured_updates.update(d)

        _refresh_existing_item(
            item,
            {
                "item_name": "Real value",
                "description": None,  # dropped
                "stock_uom": "Nos",  # dropped by the gh#52 fix
            },
        )
        self.assertEqual(captured_updates.get("item_name"), "Real value")
        self.assertNotIn("description", captured_updates)
        self.assertNotIn("stock_uom", captured_updates)


class TestPayloadPreservation(unittest.TestCase):
    """gh#52 — `last_request_payload` must be populated on Failed
    Sync Records when the upstream failure point has access to the EE
    product dict."""

    def test_write_item_pull_sync_record_accepts_request_payload(self) -> None:
        """The signature change — caller now passes
        `request_payload=product` and the field flows through."""
        from ecommerce_super.easyecom.flows import _item_sync_records

        fake_sr = MagicMock()
        fake_sr.name = "ECS-SR-001"
        fake_sr.attempts = 0

        captured_updates: dict = {}

        def _fake_db_set(updates, **_kwargs):
            captured_updates.update(updates)

        fake_sr.db_set = _fake_db_set

        product = {
            "sku": "FB15427-39",
            "accounting_unit": "MTR",
            "product_type": "normal_product",
            "weight": 10,
        }

        with (
            patch("frappe.db.exists", return_value=True),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records.sync_record_mod.upsert",
                return_value=fake_sr,
            ),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records._company_for_item_sync",
                return_value="Test Co",
            ),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records._idempotency_key_for_op",
                return_value="idem-key",
            ),
            patch(
                "ecommerce_super.easyecom.utils.correlation.new_correlation_id",
                return_value="corr-id",
            ),
        ):
            _item_sync_records.write_item_pull_sync_record(
                entity_doctype="Item",
                entity_name="FB15427-39",
                sku="FB15427-39",
                status="Failed",
                last_error="ValidationError: UOM change blocked",
                request_payload=product,
            )

        # The EE payload landed on last_request_payload as JSON.
        self.assertIn("last_request_payload", captured_updates)
        preserved = captured_updates["last_request_payload"]
        # Must be JSON-serialised — re-parsable.
        import json
        parsed = json.loads(preserved)
        self.assertEqual(parsed["sku"], "FB15427-39")
        self.assertEqual(parsed["accounting_unit"], "MTR")
        # Other fields written too.
        self.assertEqual(captured_updates["status"], "Failed")
        self.assertIn("ValidationError", captured_updates["last_error"])

    def test_payload_absent_when_not_provided(self) -> None:
        """Back-compat — callers that don't supply request_payload
        must not get a last_request_payload key written (so existing
        success-path callers stay untouched)."""
        from ecommerce_super.easyecom.flows import _item_sync_records

        fake_sr = MagicMock()
        fake_sr.name = "ECS-SR-002"
        fake_sr.attempts = 0
        captured_updates: dict = {}

        def _fake_db_set(updates, **_kwargs):
            captured_updates.update(updates)

        fake_sr.db_set = _fake_db_set

        with (
            patch("frappe.db.exists", return_value=True),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records.sync_record_mod.upsert",
                return_value=fake_sr,
            ),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records._company_for_item_sync",
                return_value="Test Co",
            ),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records._idempotency_key_for_op",
                return_value="idem-key",
            ),
            patch(
                "ecommerce_super.easyecom.utils.correlation.new_correlation_id",
                return_value="corr-id",
            ),
        ):
            _item_sync_records.write_item_pull_sync_record(
                entity_doctype="Item",
                entity_name="FB15427-39",
                sku="FB15427-39",
                status="Success",
                last_error=None,
            )

        self.assertNotIn("last_request_payload", captured_updates)

    def test_non_serialisable_payload_falls_back_to_repr(self) -> None:
        """Defensive — if the payload contains an unserialisable
        object (e.g. a Frappe Document instance that snuck in), we
        still preserve SOMETHING rather than dropping it entirely."""
        from ecommerce_super.easyecom.flows import _item_sync_records

        fake_sr = MagicMock()
        fake_sr.name = "ECS-SR-003"
        fake_sr.attempts = 0
        captured_updates: dict = {}

        def _fake_db_set(updates, **_kwargs):
            captured_updates.update(updates)

        fake_sr.db_set = _fake_db_set

        class _NotJsonable:
            def __repr__(self) -> str:
                return "<NotJsonable instance>"

        payload = {"sku": "X1", "extra": _NotJsonable()}

        with (
            patch("frappe.db.exists", return_value=True),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records.sync_record_mod.upsert",
                return_value=fake_sr,
            ),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records._company_for_item_sync",
                return_value="Test Co",
            ),
            patch(
                "ecommerce_super.easyecom.flows._item_sync_records._idempotency_key_for_op",
                return_value="idem-key",
            ),
            patch(
                "ecommerce_super.easyecom.utils.correlation.new_correlation_id",
                return_value="corr-id",
            ),
            # Force frappe.as_json to fail so we exercise the fallback.
            patch("frappe.as_json", side_effect=Exception("unserialisable")),
        ):
            _item_sync_records.write_item_pull_sync_record(
                entity_doctype="Item",
                entity_name="X1",
                sku="X1",
                status="Failed",
                last_error="boom",
                request_payload=payload,
            )

        preserved = captured_updates.get("last_request_payload", "")
        # Fallback wrote SOMETHING — better than None.
        self.assertNotEqual(preserved, "")
        self.assertIn("NotJsonable", preserved)


if __name__ == "__main__":
    unittest.main()
