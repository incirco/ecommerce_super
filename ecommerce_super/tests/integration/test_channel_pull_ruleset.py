"""Integration tests for the EasyEcom-Channel-Pull Field Mapping ruleset
(§8b refactor — was assumed-payload; now reconciled to real
/current-channel-status).

Tests the ruleset's CONTRACT independent of the discovery flow.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.channel_discovery import CHANNEL_PULL_RULESET

REQUIRED_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {"marketplace_id", "marketplace_name", "is_active"}
)

# channel_type is FDE-classified via the workflow; the ruleset must
# NOT emit it (real payload has no such field; emitting one would
# bypass the classification workflow).
FORBIDDEN_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {"channel_type", "workflow_state", "reporting_parent"}
)


class TestChannelPullRulesetShipped(FrappeTestCase):
    def test_ruleset_exists(self) -> None:
        self.assertTrue(
            frappe.db.exists("EasyEcom Field Mapping", CHANNEL_PULL_RULESET)
        )

    def test_ruleset_is_active_and_pull_direction(self) -> None:
        doc = frappe.get_doc("EasyEcom Field Mapping", CHANNEL_PULL_RULESET)
        self.assertEqual(doc.active, 1)
        self.assertEqual(doc.direction, "Pull")
        self.assertEqual(doc.entity_type, "Channel")


class TestChannelPullOutputContract(FrappeTestCase):
    """Real /current-channel-status shape → Marketplace fields."""

    def setUp(self) -> None:
        self.executor = FieldMappingExecutor(CHANNEL_PULL_RULESET)

    def test_active_channel_maps_cleanly(self) -> None:
        row = {"marketplace_name": "Flipkart", "marketplace_id": 2, "status": "Active"}
        out = self.executor.pull(row)
        # marketplace_id is Int (§31.2.18) — identity transform, no coercion.
        self.assertEqual(out["marketplace_id"], 2)
        self.assertEqual(out["marketplace_name"], "Flipkart")
        self.assertEqual(out["is_active"], 1)

    def test_inactive_channel_maps_to_is_active_zero(self) -> None:
        row = {"marketplace_name": "TaTa Cliq", "marketplace_id": 60, "status": "Inactive"}
        out = self.executor.pull(row)
        self.assertEqual(out["is_active"], 0)

    def test_missing_status_defaults_to_inactive(self) -> None:
        row = {"marketplace_name": "X", "marketplace_id": 999}  # no status
        out = self.executor.pull(row)
        self.assertEqual(out["is_active"], 0)

    def test_required_fields_present(self) -> None:
        row = {"marketplace_name": "Meesho", "marketplace_id": 122, "status": "Active"}
        out = self.executor.pull(row)
        missing = REQUIRED_OUTPUT_FIELDS - set(out.keys())
        self.assertEqual(missing, set())

    def test_forbidden_fields_absent(self) -> None:
        """channel_type must NOT be emitted — FDE-classified via the
        workflow, not from the EE payload (which has no such field)."""
        row = {"marketplace_name": "Meesho", "marketplace_id": 122, "status": "Active"}
        out = self.executor.pull(row)
        leaked = FORBIDDEN_OUTPUT_FIELDS.intersection(set(out.keys()))
        self.assertEqual(leaked, set())

    def test_marketplace_id_preserved_as_int(self) -> None:
        """marketplace_id is Int per §31.2.18 — the ruleset's identity
        transform must preserve the int (no string coercion)."""
        row = {"marketplace_name": "Meesho", "marketplace_id": 122, "status": "Active"}
        out = self.executor.pull(row)
        self.assertEqual(out["marketplace_id"], 122)
        self.assertIsInstance(out["marketplace_id"], int)

    def test_marketplace_id_required(self) -> None:
        from ecommerce_super.easyecom.exceptions import (
            FieldMappingMissingRequiredError,
        )

        with self.assertRaises(FieldMappingMissingRequiredError):
            self.executor.pull({"marketplace_name": "no id", "status": "Active"})

    def test_marketplace_name_required(self) -> None:
        from ecommerce_super.easyecom.exceptions import (
            FieldMappingMissingRequiredError,
        )

        with self.assertRaises(FieldMappingMissingRequiredError):
            self.executor.pull({"marketplace_id": 999, "status": "Active"})
