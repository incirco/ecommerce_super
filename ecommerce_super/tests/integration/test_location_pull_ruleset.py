"""Integration tests for the EasyEcom-Location-Pull Field Mapping ruleset.

The §8a refactor moved payload→field translation from the hardcoded
_ee_supplied_fields() mapper into a shipped ruleset. These tests pin
the ruleset's CONTRACT — independently of the flow that drives it —
so an unrelated edit to the ruleset that drops a field or breaks the
stockHandle derivation gets caught at the ruleset level, not via a
downstream flow failure.
"""

from __future__ import annotations

import json
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.location_discovery import LOCATION_PULL_RULESET

# Fields the ruleset MUST produce in its output for a complete EE row.
# These are the EE-supplied fields per §31.2.2; FDE-owned fields
# (frappe_company, mapped_warehouse, gstin, is_primary, workflow_state)
# are deliberately absent.
REQUIRED_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {
        "location_key",
        "location_name",
        "ee_company_id",
        "is_store",
        "copy_master_from_primary",
        "city",
        "state",
        "country",
        "pincode",
        "address_line",
        "billing_street",
        "billing_state",
        "billing_zipcode",
        "billing_country",
        "pickup_street",
        "pickup_state",
        "pickup_zipcode",
        "pickup_country",
        "is_wms_location",
    }
)

# Fields the ruleset MUST NOT produce — credential-shaped or
# operationally-irrelevant.
FORBIDDEN_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {
        "api_token",  # credential-shaped (§7.7) — must never be mapped
        "userId",
        "phone number",
        "phone_number",
        "frappe_company",  # FDE-owned
        "gstin",
        "is_primary",
        "workflow_state",
        "is_operational",
    }
)


def _load_fixture() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "sample_payloads", "getAllLocation.json")
    with open(path) as f:
        return json.load(f)


class TestRulesetShipped(FrappeTestCase):
    """The ruleset is installed by the fixture and the orphaned
    EasyEcom-Warehouse-Pull row was dropped by the v0_1 patch."""

    def test_location_pull_ruleset_exists(self) -> None:
        self.assertTrue(
            frappe.db.exists("EasyEcom Field Mapping", LOCATION_PULL_RULESET),
            f"{LOCATION_PULL_RULESET} ruleset not installed (fixture missing?)",
        )

    def test_old_warehouse_pull_ruleset_dropped(self) -> None:
        """The §8a refactor renamed Warehouse-Pull → Location-Pull. The
        patch (drop_warehouse_pull_ruleset) removes the orphaned name on
        sites that received the old fixture."""
        self.assertFalse(
            frappe.db.exists("EasyEcom Field Mapping", "EasyEcom-Warehouse-Pull"),
            "Orphaned EasyEcom-Warehouse-Pull ruleset still present — the "
            "drop_warehouse_pull_ruleset patch should have removed it.",
        )

    def test_ruleset_is_active_and_pull_direction(self) -> None:
        doc = frappe.get_doc("EasyEcom Field Mapping", LOCATION_PULL_RULESET)
        self.assertEqual(doc.active, 1)
        self.assertEqual(doc.direction, "Pull")
        self.assertEqual(doc.entity_type, "Warehouse")


class TestRulesetOutputContract(FrappeTestCase):
    """Run the real /getAllLocation fixture through the engine and pin
    every required field, plus assert credential-shaped / FDE-owned
    fields are absent."""

    def setUp(self) -> None:
        self.executor = FieldMappingExecutor(LOCATION_PULL_RULESET)
        self.fixture = _load_fixture()

    def test_required_output_fields_present(self) -> None:
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        missing = REQUIRED_OUTPUT_FIELDS - set(out.keys())
        self.assertEqual(
            missing,
            set(),
            f"Ruleset output missing required fields: {sorted(missing)}",
        )

    def test_forbidden_output_fields_absent(self) -> None:
        """api_token is the load-bearing one: it's credential-shaped and
        must never appear in the ruleset output. FDE-owned fields must
        also be absent — the flow owns them, not the ruleset."""
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        leaked = FORBIDDEN_OUTPUT_FIELDS.intersection(set(out.keys()))
        self.assertEqual(
            leaked,
            set(),
            f"Ruleset output contains forbidden fields: {sorted(leaked)}",
        )

    def test_api_token_string_never_in_output(self) -> None:
        """Belt-and-braces — even if a misconfigured rule somehow mapped
        api_token under a non-obvious name, the serialised output must
        not contain the credential string."""
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        as_json = frappe.as_json(out)
        # Marker from the fixture payload.
        self.assertNotIn("redact-never-store", as_json)

    def test_stock_handle_derivation_wms(self) -> None:
        """stockHandle=1 → is_wms_location=1 (the ruleset transform)."""
        row = dict(self.fixture["data"][0])  # has stockHandle=1
        out = self.executor.pull(row)
        self.assertEqual(out["is_wms_location"], 1)

    def test_stock_handle_derivation_non_wms(self) -> None:
        """stockHandle=0 → is_wms_location=0."""
        row = dict(self.fixture["data"][1])  # has stockHandle=0
        out = self.executor.pull(row)
        self.assertEqual(out["is_wms_location"], 0)

    def test_stock_handle_derivation_string_truthy(self) -> None:
        """EE could send stockHandle as a string '1'. The
        conditional_constant transform's `when` covers that case."""
        row = dict(self.fixture["data"][0])
        row["stockHandle"] = "1"
        out = self.executor.pull(row)
        self.assertEqual(out["is_wms_location"], 1)

    def test_stock_handle_derivation_missing(self) -> None:
        """No stockHandle in payload → derivation defaults to 0."""
        row = dict(self.fixture["data"][0])
        del row["stockHandle"]
        out = self.executor.pull(row)
        self.assertEqual(out["is_wms_location"], 0)

    def test_nested_billing_address_paths(self) -> None:
        """The 'address type' key has a literal space — the path
        engine must traverse it. Regression test for the path-validator
        fix that allowed space-bearing keys."""
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        self.assertEqual(out["billing_street"], "Banglore bangalore")
        self.assertEqual(out["billing_state"], "Karnataka")
        self.assertEqual(out["billing_zipcode"], "560102")
        self.assertEqual(out["billing_country"], "India")

    def test_nested_pickup_address_paths(self) -> None:
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        self.assertEqual(out["pickup_street"], "Banglore bangalore")
        self.assertEqual(out["pickup_state"], "Karnataka")
        self.assertEqual(out["pickup_zipcode"], "560102")
        self.assertEqual(out["pickup_country"], "India")

    def test_company_id_coerced_to_string(self) -> None:
        """EE sends company_id as an int (171721). The Data field on
        EasyEcom Location wants a string — int_to_str transform."""
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        self.assertEqual(out["ee_company_id"], "171721")
        self.assertIsInstance(out["ee_company_id"], str)

    def test_zip_coerced_to_string_for_pincode(self) -> None:
        """payload.zip → pincode (Data); coerced to str."""
        row = self.fixture["data"][0]
        out = self.executor.pull(row)
        self.assertEqual(out["pincode"], "560102")
        self.assertIsInstance(out["pincode"], str)

    def test_location_key_and_name_required(self) -> None:
        """Both are required=1 in the ruleset; missing location_key must
        raise on pull."""
        from ecommerce_super.easyecom.exceptions import (
            FieldMappingMissingRequiredError,
        )

        row = {"location_name": "no key"}  # missing location_key
        with self.assertRaises(FieldMappingMissingRequiredError):
            self.executor.pull(row)
