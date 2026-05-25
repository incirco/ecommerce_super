"""Integration tests for §8.4.1 Location discovery pull.

Drives upsert_locations_from_payload (which is what the production pull
calls after fetching the live response) against the real /getAllLocation
payload captured in tests/sample_payloads/getAllLocation.json.

Covers:
  - New locations land in workflow_state="To Map"
  - is_wms_location is derived from stockHandle (1→1, 0→0)
  - All EE-supplied fields map (including nested billing/pickup addresses)
  - api_token is never persisted onto the Location row
  - Re-pull updates EE-supplied fields in place but leaves workflow_state
    AND FDE-set fields (frappe_company, is_primary, gstin) untouched
  - Endpoint constant is /getAllLocation (the §8a correction)
"""

from __future__ import annotations

import json
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import LOCATIONS_GET
from ecommerce_super.easyecom.flows.location_discovery import (
    upsert_locations_from_payload,
)


def _load_fixture() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "sample_payloads", "getAllLocation.json")
    with open(path) as f:
        return json.load(f)


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


class TestEndpointConstantIsCorrect(FrappeTestCase):
    """§8a item 1: the foundation's /Wms/Inventory/getLocations was wrong.
    The live endpoint is /getAllLocation."""

    def test_locations_get_is_the_correct_endpoint(self) -> None:
        self.assertEqual(LOCATIONS_GET, "/getAllLocation")


class TestDiscoveryUpsertNewRows(FrappeTestCase):
    """New rows must land in To Map with is_wms_location from stockHandle."""

    PREFIX = "ne2948810"  # matches the real-payload location_keys

    def setUp(self) -> None:
        _wipe_locations(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_locations(self.PREFIX)

    def test_real_payload_creates_two_locations_in_to_map(self) -> None:
        fixture = _load_fixture()
        outcome = upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        self.assertEqual(outcome.succeeded_count, 2)
        self.assertEqual(outcome.failed_count, 0)
        # Both should be in To Map.
        for loc_key in ("ne29488101841", "ne29488101842"):
            docname = f"ECS-LOC-{loc_key}"
            self.assertTrue(frappe.db.exists("EasyEcom Location", docname))
            doc = frappe.get_doc("EasyEcom Location", docname)
            self.assertEqual(doc.workflow_state, "To Map")
            self.assertEqual(doc.is_operational, 0)
            # FDE-set fields stay blank.
            self.assertFalse(doc.frappe_company)
            self.assertFalse(doc.is_primary)
            self.assertFalse(doc.gstin)

    def test_is_wms_location_derived_from_stockHandle(self) -> None:
        """stockHandle=1 → is_wms_location=1; stockHandle=0 → 0."""
        fixture = _load_fixture()
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        # First location has stockHandle=1 → WMS.
        wms = frappe.get_doc("EasyEcom Location", "ECS-LOC-ne29488101841")
        self.assertEqual(wms.is_wms_location, 1)
        # Second has stockHandle=0 → not WMS.
        nonwms = frappe.get_doc("EasyEcom Location", "ECS-LOC-ne29488101842")
        self.assertEqual(nonwms.is_wms_location, 0)

    def test_address_fields_captured(self) -> None:
        """Flat city/state/country/zip plus nested billing/pickup addresses
        all land on the doc per the §8.4.1 mapping table."""
        fixture = _load_fixture()
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        doc = frappe.get_doc("EasyEcom Location", "ECS-LOC-ne29488101841")
        self.assertEqual(doc.city, "Bangalore")
        self.assertEqual(doc.state, "Karnataka")
        self.assertEqual(doc.country, "India")
        self.assertEqual(doc.pincode, "560102")  # zip → pincode
        self.assertEqual(doc.address_line, "Banglore bangalore")  # address → address_line
        # Billing nested.
        self.assertEqual(doc.billing_street, "Banglore bangalore")
        self.assertEqual(doc.billing_state, "Karnataka")
        self.assertEqual(doc.billing_zipcode, "560102")
        self.assertEqual(doc.billing_country, "India")
        # Pickup nested.
        self.assertEqual(doc.pickup_street, "Banglore bangalore")
        self.assertEqual(doc.pickup_zipcode, "560102")

    def test_ee_company_id_and_flags_captured(self) -> None:
        fixture = _load_fixture()
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        doc = frappe.get_doc("EasyEcom Location", "ECS-LOC-ne29488101841")
        self.assertEqual(doc.ee_company_id, "171721")
        self.assertEqual(doc.is_store, 0)
        self.assertEqual(doc.copy_master_from_primary, 1)

        primary_master = frappe.get_doc("EasyEcom Location", "ECS-LOC-ne29488101842")
        self.assertEqual(primary_master.is_store, 1)
        self.assertEqual(primary_master.copy_master_from_primary, 0)

    def test_api_token_never_persisted(self) -> None:
        """§8a §7.7 contract: api_token is credential-shaped and must
        never land on the Location row. The redaction layer scrubs the
        API Call log; this flow's mapper must scrub the doc."""
        fixture = _load_fixture()
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        # Frappe Document doesn't carry fields that aren't in the DocType,
        # but a defensive caller might have added one. Verify the doc
        # representation literally does not contain the credential string.
        doc = frappe.get_doc("EasyEcom Location", "ECS-LOC-ne29488101841")
        as_json = frappe.as_json(doc.as_dict())
        # The fixture's api_token string is a marker; ensure it's gone.
        self.assertNotIn("redact-never-store", as_json)
        self.assertNotIn("api_token", as_json)


class TestRePullSemantics(FrappeTestCase):
    """Re-pull must REFRESH EE-supplied fields and LEAVE workflow_state
    plus FDE-set fields (frappe_company, is_primary, gstin) untouched."""

    LOC_KEY = "ne29488101841"
    DOCNAME = "ECS-LOC-ne29488101841"
    OTHER_KEY = "ne29488101842"
    OTHER_DOCNAME = "ECS-LOC-ne29488101842"

    def setUp(self) -> None:
        _wipe_locations("ne2948810")

    def tearDown(self) -> None:
        _wipe_locations("ne2948810")

    def test_repull_leaves_fde_set_fields_untouched(self) -> None:
        # First pull → To Map.
        fixture = _load_fixture()
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        # FDE maps + goes live (simulated via direct field writes here).
        # In production this happens via the Workflow.
        frappe.db.set_value(
            "EasyEcom Location",
            self.DOCNAME,
            {
                "frappe_company": "_Test Company",
                "is_primary": 1,
                "gstin": "29AABCT1234A1Z5",
                "workflow_state": "Live",
            },
        )
        frappe.db.commit()
        # The other (non-primary) location stays To Map without FDE-set fields.

        # Mutate the fixture's EE-supplied side to simulate EE changing
        # the name and address.
        fixture["data"][0]["location_name"] = "prod_wh_2_RENAMED"
        fixture["data"][0]["city"] = "Mysore"

        # Second pull.
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        doc = frappe.get_doc("EasyEcom Location", self.DOCNAME)
        # EE-supplied fields REFRESHED.
        self.assertEqual(doc.location_name, "prod_wh_2_RENAMED")
        self.assertEqual(doc.city, "Mysore")
        # FDE-set fields UNTOUCHED.
        self.assertEqual(doc.frappe_company, "_Test Company")
        self.assertEqual(doc.is_primary, 1)
        self.assertEqual(doc.gstin, "29AABCT1234A1Z5")
        # Workflow state UNTOUCHED.
        self.assertEqual(doc.workflow_state, "Live")

    def test_repull_does_not_reset_is_wms_location_override(self) -> None:
        """is_wms_location is derived ONLY on first create — re-pull never
        overrides an FDE override. (The packet's wording: 'FDE may
        override on the form'.)"""
        fixture = _load_fixture()
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        # FDE overrides: turn WMS off on the row that was discovered as WMS=1.
        frappe.db.set_value("EasyEcom Location", self.DOCNAME, "is_wms_location", 0)
        frappe.db.commit()

        # Re-pull — fixture still has stockHandle=1.
        upsert_locations_from_payload(fixture["data"])
        frappe.db.commit()

        # The FDE's override survives.
        doc = frappe.get_doc("EasyEcom Location", self.DOCNAME)
        self.assertEqual(doc.is_wms_location, 0)


class TestRobustness(FrappeTestCase):
    """Real-world payloads have edge cases (numeric pincodes, missing
    sub-objects, space-bearing keys). The mapper must not choke."""

    def setUp(self) -> None:
        _wipe_locations("rob-")

    def tearDown(self) -> None:
        _wipe_locations("rob-")

    def test_missing_address_type_is_handled(self) -> None:
        """Some locations have no 'address type' key. Don't crash."""
        rows = [
            {
                "location_key": "rob-no-addr",
                "location_name": "No Address Type",
                "company_id": 1,
                "stockHandle": 0,
            }
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        self.assertEqual(outcome.succeeded_count, 1)
        self.assertEqual(outcome.failed_count, 0)

    def test_numeric_zip_is_coerced_to_string(self) -> None:
        rows = [
            {
                "location_key": "rob-int-zip",
                "location_name": "Numeric Zip",
                "company_id": 999,
                "zip": 560042,  # numeric, not string
                "stockHandle": 1,
            }
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        self.assertEqual(outcome.succeeded_count, 1)
        doc = frappe.get_doc("EasyEcom Location", "ECS-LOC-rob-int-zip")
        self.assertEqual(doc.pincode, "560042")
        self.assertEqual(doc.ee_company_id, "999")

    def test_missing_location_key_lands_in_failed(self) -> None:
        """A row without location_key can't be upserted. The savepoint
        helper records it as failed; siblings survive."""
        rows = [
            {"location_key": "rob-ok", "location_name": "OK", "stockHandle": 1},
            {"location_name": "Bad: no key"},  # will raise
            {"location_key": "rob-ok-2", "location_name": "OK 2", "stockHandle": 0},
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        self.assertEqual(outcome.succeeded_count, 2)
        self.assertEqual(outcome.failed_count, 1)
        # The two OK ones committed.
        self.assertTrue(frappe.db.exists("EasyEcom Location", "ECS-LOC-rob-ok"))
        self.assertTrue(frappe.db.exists("EasyEcom Location", "ECS-LOC-rob-ok-2"))
