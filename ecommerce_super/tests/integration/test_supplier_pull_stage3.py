"""Stage 3 tests for §8f — EE→EN supplier pull.

ALL MOCKED — no real EE traffic. Fixture data:
  - process/ee_mock_fixtures/getvendors_page1_response.json (20 real
    Harmony records, page 1 of 2)
  - process/ee_mock_fixtures/getvendors_pages_response.json (30
    records across 2 cursor pages — used to drive cursor-walk tests)
  - process/ee_mock_fixtures/getstates_{india,italy,armenia}_response.json
    (the §8f Stage 2 multi-country fixtures)

Covers the §8.3 packet decisions:
  1. Two-identifier split — vendor_c_id (read key) AND vendor_code
     (= vendor_id write key) are BOTH captured on the Map row.
  2. Cursor pagination — nextUrl is followed page-by-page; the
     persisted cursor advances after each page commit.
  3. Map-row-only matching — re-pull is no-op; no natural-key auto-
     match (mirrors §8e customer pull's dirty-data reasoning).
  4. Empty-array address — `address.dispatch: []` (16/30 captured
     vendors) → no Address row inserted, no crash.
  5. Country-aware GST gating:
     - Indian + valid GSTIN → Supplier with gstin set, PAN auto-
       extracted by IC, gst_category derived.
     - Indian + blank GSTIN → gst_category='Unregistered'.
     - Indian + invalid GSTIN → IC throws → FNC with cleanup.
     - Foreign (Italy / Armenia) → gst_category='Overseas' set BEFORE
       IC validate; GSTIN/PAN optional; supplier created cleanly.
  6. Lifecycle pull-side — active:0 → Supplier.disabled=1; active:1
     from previously-Disabled → restored to Mapped.
  7. Sync Record per supplier — direction=Pull, entity_type=Supplier,
     status=Success on Mapped/Disabled, no SR for FNC.
  8. Empty-response validator fix — HTTP 200 with body code 400 +
     "no data found"-class message is now treated as success-empty
     rather than raising EasyEcomValidationError.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    COUNTRIES_GET,
    FOUNDATIONAL_ENDPOINTS,
    STATES_GET,
    VENDORS_GET,
    WHOLESALE_VENDOR_CREATE,
    WHOLESALE_VENDOR_UPDATE,
    is_foundational,
)
from ecommerce_super.easyecom.flows._supplier_sync_records import (
    ENTITY_TYPE_SUPPLIER,
)
from ecommerce_super.easyecom.flows.supplier_pull import (
    SUPPLIER_PULL_RULESET,
    MODE_ONBOARDING,
    process_one_supplier,
    pull_suppliers,
    _flatten_vendor_row,
    _classify_country,
)
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)
from ecommerce_super.tests.factories import make_account


FIXTURE_DIR = os.path.join(
    frappe.get_app_path("ecommerce_super"),
    "..",
    "process",
    "ee_mock_fixtures",
)


# Synthetic GSTINs with valid IC check digits (mirrors §8e Stage 3).
# Real Harmony test vendors have placeholder GSTINs that all fail
# India Compliance — those become FNC. To test the happy path we
# substitute these into a synthetic vendor.
VALID_GSTIN_DELHI = "07ABCDE1234F1Z2"      # state_code 07 = Delhi
VALID_GSTIN_KARNATAKA = "29ABCDE1234F1ZW"  # state_code 29 = Karnataka


def _load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)


def _load_pages_fixture() -> list[dict]:
    return _load_fixture("getvendors_pages_response.json")


def _seed_lookups_cache() -> None:
    """Populate Country/State cache so the flow's country resolution
    works (resolve_country() reads from this cache)."""
    from ecommerce_super.easyecom.flows.customer_lookups import (
        pull_countries_and_states,
    )

    countries = _load_fixture("getcountries_response.json")
    india = _load_fixture("getstates_india_response.json")
    italy = _load_fixture("getstates_italy_response.json")
    armenia = _load_fixture("getstates_armenia_response.json")

    client = MagicMock()

    def _get(endpoint, params=None, **_kw):
        if endpoint == COUNTRIES_GET:
            return countries
        if endpoint == STATES_GET:
            cid = int((params or {}).get("countryId") or 0)
            return {
                1: india,
                114: italy,
                15: armenia,
            }.get(cid, {"code": 200, "states": []})
        raise AssertionError(f"unexpected endpoint: {endpoint!r}")

    client.get.side_effect = _get
    pull_countries_and_states(client=client)


def _wipe_supplier_maps() -> None:
    for n in frappe.db.get_all("EasyEcom Supplier Map", pluck="name"):
        try:
            frappe.delete_doc(
                "EasyEcom Supplier Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _wipe_test_suppliers() -> None:
    """Wipe every Supplier whose name appears in the captured fixture,
    plus their linked Addresses. Safe because the fixture's vendor
    names don't collide with any real prod data."""
    vendor_names = set()
    for page in _load_pages_fixture():
        for v in page.get("data") or []:
            if v.get("vendor_name"):
                vendor_names.add(v["vendor_name"])
    # Also clean synthetic-test vendors used below.
    vendor_names.update(
        {"TEST-PULL-Foreign-Italy", "TEST-PULL-Valid-GSTIN-Delhi"}
    )

    for n in frappe.db.get_all(
        "Supplier",
        filters={"supplier_name": ("in", list(vendor_names))},
        pluck="name",
    ):
        for (addr,) in frappe.db.sql(
            """SELECT DISTINCT parent FROM `tabDynamic Link`
               WHERE parenttype='Address' AND link_doctype='Supplier'
                 AND link_name=%s""",
            (n,),
        ):
            try:
                frappe.delete_doc(
                    "Address", addr, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        try:
            frappe.delete_doc(
                "Supplier", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Drop sync records for any Supplier (since names may have changed).
    frappe.db.sql(
        """DELETE FROM `tabEasyEcom Sync Record`
           WHERE entity_doctype='Supplier'"""
    )
    frappe.db.commit()


def _make_mock_client(pages: list[dict]) -> MagicMock:
    """Build a MagicMock EasyEcomClient.get() that returns the next
    fixture page on each call. The first call returns pages[0]
    regardless of params; subsequent calls return pages[1], etc.

    Mirrors how the real client.get(VENDORS_GET) → response with
    nextUrl → client.get(nextUrl) → next response shape.
    """
    iterator = iter(pages)
    client = MagicMock()

    def _get(endpoint, params=None, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return {"code": 200, "data": []}

    client.get.side_effect = _get
    return client


# ----- The test classes -----


class TestEndpointRegistered(FrappeTestCase):
    """The /wms/V2/getVendors endpoint and the two push companions are
    classified foundational so client-layer sets no company on the
    API Call rows."""

    def test_vendors_get_is_foundational(self) -> None:
        self.assertIn(VENDORS_GET, FOUNDATIONAL_ENDPOINTS)
        self.assertTrue(is_foundational(VENDORS_GET))

    def test_vendors_get_with_cursor_classified_foundational(self) -> None:
        """nextUrl carries a cursor query string. The classifier strips
        before set-membership (same fix as PRODUCT_MASTER_GET cursor)."""
        self.assertTrue(is_foundational(f"{VENDORS_GET}?cursor=abc123"))

    def test_create_vendor_is_foundational(self) -> None:
        self.assertIn(WHOLESALE_VENDOR_CREATE, FOUNDATIONAL_ENDPOINTS)

    def test_update_vendor_is_foundational(self) -> None:
        self.assertIn(WHOLESALE_VENDOR_UPDATE, FOUNDATIONAL_ENDPOINTS)


class TestRulesetRetirement(FrappeTestCase):
    """EasyEcom-Supplier-Sync (bidirectional, 4 fields, conflated ids)
    must be retired (active=0). EasyEcom-Supplier-Pull (Pull, 19
    fields, two-id split) must be active."""

    def test_supplier_sync_is_retired(self) -> None:
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Supplier-Sync", "active"
        )
        self.assertEqual(int(active or 0), 0)

    def test_supplier_pull_is_active(self) -> None:
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Supplier-Pull", "active"
        )
        self.assertEqual(int(active or 0), 1)

    def test_supplier_pull_direction_is_pull(self) -> None:
        d = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Supplier-Pull", "direction"
        )
        self.assertEqual(d, "Pull")

    def test_supplier_pull_has_both_identifier_rules(self) -> None:
        """The ruleset MUST carry both ee_vendor_c_id (read) and
        ee_vendor_id (write) rules — the two-identifier split is the
        defining §8.3 contract."""
        rules = frappe.db.get_all(
            "EasyEcom Field Mapping Rule",
            filters={"parent": "EasyEcom-Supplier-Pull"},
            fields=["erpnext_path", "easyecom_path", "required"],
        )
        paths = {(r.erpnext_path, r.easyecom_path) for r in rules}
        self.assertIn(("ee_vendor_c_id", "vendor_c_id"), paths)
        self.assertIn(("ee_vendor_id", "vendor_code"), paths)
        # ee_vendor_c_id MUST be required; ee_vendor_id may be empty
        # on rows where Create hasn't returned a vendor_id yet (Stage 4
        # writeback).
        required_paths = {
            r.erpnext_path for r in rules if int(r.required or 0) == 1
        }
        self.assertIn("ee_vendor_c_id", required_paths)


class TestFlattenVendorRow(FrappeTestCase):
    """The address-envelope flattener handles the three observed
    shapes: nested object, empty array, missing entirely."""

    def test_nested_billing_dispatch_objects_flatten(self) -> None:
        row = {
            "vendor_c_id": 1,
            "address": {
                "billing": {
                    "address": "B1", "city": "Bcity", "zip": "B123",
                    "state_name": "Karnataka", "country": "India",
                },
                "dispatch": {
                    "address": "D1", "city": "Dcity", "zip": "D456",
                    "state_name": "Maharashtra", "country": "India",
                },
            },
        }
        flat = _flatten_vendor_row(row)
        self.assertEqual(flat["billing_address_line"], "B1")
        self.assertEqual(flat["billing_state_name"], "Karnataka")
        self.assertEqual(flat["dispatch_country"], "India")
        self.assertEqual(flat["dispatch_zip"], "D456")

    def test_empty_array_dispatch_yields_absent_dispatch_keys(self) -> None:
        """16 of 30 captured vendors have `dispatch: []`. Engine sees
        NO dispatch_* keys (Permissive policy skips silently)."""
        row = {
            "vendor_c_id": 1,
            "address": {
                "billing": {"address": "B1", "city": "Bcity"},
                "dispatch": [],
            },
        }
        flat = _flatten_vendor_row(row)
        self.assertIn("billing_address_line", flat)
        self.assertNotIn("dispatch_address_line", flat)
        self.assertNotIn("dispatch_city", flat)

    def test_both_empty_arrays_yields_no_address_keys(self) -> None:
        """vendor_c_id 126279 (MSTEST_123) has both empty."""
        row = {
            "vendor_c_id": 1,
            "address": {"billing": [], "dispatch": []},
        }
        flat = _flatten_vendor_row(row)
        self.assertNotIn("billing_address_line", flat)
        self.assertNotIn("dispatch_address_line", flat)

    def test_missing_address_field(self) -> None:
        flat = _flatten_vendor_row({"vendor_c_id": 1})
        self.assertEqual(flat["vendor_c_id"], 1)
        self.assertNotIn("billing_address_line", flat)

    def test_address_is_junk_yields_flat_minus_addresses(self) -> None:
        flat = _flatten_vendor_row({"vendor_c_id": 1, "address": "junk"})
        self.assertNotIn("billing_city", flat)


class TestCountryClassification(FrappeTestCase):
    """resolve_country + Indian-name aliasing → 3-way bucket. Drives
    the GST gating downstream."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()

    def test_india_canonical(self) -> None:
        self.assertEqual(_classify_country("India"), "india")

    def test_india_aliases(self) -> None:
        for variant in ("india", "INDIA", "  India  ", "in", "IND"):
            self.assertEqual(_classify_country(variant), "india")

    def test_italy_is_foreign(self) -> None:
        self.assertEqual(_classify_country("Italy"), "foreign")

    def test_armenia_is_foreign(self) -> None:
        self.assertEqual(_classify_country("Armenia"), "foreign")

    def test_blank_is_unknown(self) -> None:
        self.assertEqual(_classify_country(""), "unknown")
        self.assertEqual(_classify_country(None), "unknown")

    def test_garbage_country_is_unknown(self) -> None:
        """Country names not in the Stage 2 cache return 'unknown'."""
        self.assertEqual(_classify_country("Wakanda"), "unknown")


class TestPullHappyPath(FrappeTestCase):
    """Drive process_one_supplier with synthetic rows covering the
    happy-path branches: Indian valid, foreign Overseas, lifecycle
    disabled. The captured fixture's Indian vendors all carry dirty
    GSTINs (FNC); the happy path needs a synthetic vendor."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()

    def setUp(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def _executor(self) -> FieldMappingExecutor:
        return FieldMappingExecutor(SUPPLIER_PULL_RULESET)

    def test_indian_valid_gstin_creates_supplier_mapped(self) -> None:
        row = {
            "vendor_c_id": 999001,
            "vendor_code": "VN-T-001",
            "vendor_name": "TEST-PULL-Valid-GSTIN-Delhi",
            "tax_identification_number": VALID_GSTIN_DELHI,
            "pan": "ABCDE1234F",
            "email": "valid@test.local",
            "contact_number": "9999999001",
            "active": 1,
            "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "1 Test Marg", "city": "New Delhi",
                    "state_name": "Delhi", "zip": "110001",
                    "country": "India",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertEqual(outcome.status, "Mapped")
        self.assertEqual(outcome.operation, "created")
        self.assertEqual(outcome.country_kind, "india")
        self.assertEqual(outcome.ee_vendor_c_id, "999001")
        self.assertEqual(outcome.ee_vendor_id, "VN-T-001")
        self.assertIsNotNone(outcome.supplier_docname)

        # Map row stores BOTH identifiers.
        row_db = frappe.db.get_value(
            "EasyEcom Supplier Map",
            "ECS-SUPP-999001",
            ["ee_vendor_c_id", "ee_vendor_id", "status", "erpnext_name"],
            as_dict=True,
        )
        self.assertEqual(row_db.ee_vendor_c_id, "999001")
        self.assertEqual(row_db.ee_vendor_id, "VN-T-001")
        self.assertEqual(row_db.status, "Mapped")

        # Supplier exists with GSTIN persisted.
        supp = frappe.db.get_value(
            "Supplier", row_db.erpnext_name,
            ["gstin", "gst_category", "country", "disabled"],
            as_dict=True,
        )
        self.assertEqual(supp.gstin, VALID_GSTIN_DELHI)
        self.assertEqual(supp.country, "India")
        self.assertEqual(int(supp.disabled), 0)

    def test_foreign_italy_creates_overseas_supplier(self) -> None:
        """Italian supplier → gst_category=Overseas (set before IC
        validate); GSTIN/PAN optional; IC accepts."""
        row = {
            "vendor_c_id": 999002,
            "vendor_code": "VN-T-002",
            "vendor_name": "TEST-PULL-Foreign-Italy",
            "tax_identification_number": "",
            "pan": "",
            "email": "italy@test.local",
            "contact_number": "+39123456789",
            "active": 1,
            "currency_code": "EUR",
            "address": {
                "billing": {
                    "address": "Via Roma 1", "city": "Roma",
                    "state_name": "Abruzzo", "zip": "00100",
                    "country": "Italy",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertEqual(outcome.status, "Mapped", outcome.flag_reasons)
        self.assertEqual(outcome.country_kind, "foreign")

        supp = frappe.db.get_value(
            "Supplier", outcome.supplier_docname,
            ["gst_category", "country", "gstin"],
            as_dict=True,
        )
        self.assertEqual(supp.gst_category, "Overseas")
        self.assertEqual(supp.country, "Italy")
        self.assertFalse(supp.gstin)

    def test_active_zero_lands_as_disabled(self) -> None:
        """Lifecycle pull-side — active:0 → Supplier.disabled=1 +
        Map.status=Disabled."""
        row = {
            "vendor_c_id": 999003,
            "vendor_code": "VN-T-003",
            "vendor_name": "TEST-PULL-Inactive",
            "tax_identification_number": VALID_GSTIN_KARNATAKA,
            "pan": "",
            "email": "inactive@test.local",
            "contact_number": "9999999003",
            "active": 0,
            "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "X", "city": "Bengaluru",
                    "state_name": "Karnataka", "zip": "560035",
                    "country": "India",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertEqual(outcome.status, "Disabled")
        self.assertEqual(outcome.operation, "created")
        supp_disabled = frappe.db.get_value(
            "Supplier", outcome.supplier_docname, "disabled"
        )
        self.assertEqual(int(supp_disabled), 1)


class TestMapRowReuse(FrappeTestCase):
    """Second pull of the same vendor → no new Supplier created, no
    new Map row. Lifecycle refresh runs (active flip flips disabled)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()

    def setUp(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def _executor(self) -> FieldMappingExecutor:
        return FieldMappingExecutor(SUPPLIER_PULL_RULESET)

    def _vendor(self, active: int = 1) -> dict:
        return {
            "vendor_c_id": 998001,
            "vendor_code": "VN-Reuse-001",
            "vendor_name": "TEST-PULL-Valid-GSTIN-Delhi",
            "tax_identification_number": VALID_GSTIN_DELHI,
            "pan": "",
            "email": "reuse@test.local",
            "contact_number": "9999998001",
            "active": active,
            "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "1 Test", "city": "Delhi",
                    "state_name": "Delhi", "zip": "110001",
                    "country": "India",
                },
                "dispatch": [],
            },
        }

    def test_second_pull_is_skipped(self) -> None:
        first = process_one_supplier(
            self._vendor(), executor=self._executor(),
            account_mode=MODE_ONBOARDING,
        )
        before_count = frappe.db.count("EasyEcom Supplier Map")
        second = process_one_supplier(
            self._vendor(), executor=self._executor(),
            account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(second.operation, "skipped")
        self.assertEqual(second.supplier_docname, first.supplier_docname)
        self.assertEqual(frappe.db.count("EasyEcom Supplier Map"), before_count)

    def test_active_to_zero_flips_existing_to_disabled(self) -> None:
        """Mapped + active=1 → next pull with active=0 → status flips
        to Disabled and Supplier.disabled=1."""
        first = process_one_supplier(
            self._vendor(active=1), executor=self._executor(),
            account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(first.status, "Mapped")

        # Same vendor, now active=0.
        second = process_one_supplier(
            self._vendor(active=0), executor=self._executor(),
            account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(second.status, "Disabled")
        self.assertEqual(second.operation, "disabled")
        supp_disabled = frappe.db.get_value(
            "Supplier", first.supplier_docname, "disabled"
        )
        self.assertEqual(int(supp_disabled), 1)

    def test_disabled_to_active_restores_mapped(self) -> None:
        """Symmetric: Disabled + next pull active=1 → restored to
        Mapped + Supplier.disabled=0."""
        first = process_one_supplier(
            self._vendor(active=0), executor=self._executor(),
            account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(first.status, "Disabled")

        third = process_one_supplier(
            self._vendor(active=1), executor=self._executor(),
            account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(third.status, "Mapped")
        supp_disabled = frappe.db.get_value(
            "Supplier", first.supplier_docname, "disabled"
        )
        self.assertEqual(int(supp_disabled), 0)


class TestGstinGating(FrappeTestCase):
    """Indian + blank GSTIN → Unregistered; invalid GSTIN → IC throws
    → FNC with cleanup; valid → Mapped."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()

    def setUp(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def _executor(self) -> FieldMappingExecutor:
        return FieldMappingExecutor(SUPPLIER_PULL_RULESET)

    def test_indian_blank_gstin_is_unregistered(self) -> None:
        row = {
            "vendor_c_id": 997001, "vendor_code": "VN-T-UNREG",
            "vendor_name": "TEST-PULL-Indian-Unregistered",
            "tax_identification_number": "", "pan": "",
            "email": "unreg@test.local", "contact_number": "9999997001",
            "active": 1, "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "X", "city": "X", "state_name": "Delhi",
                    "zip": "110001", "country": "India",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertEqual(outcome.status, "Mapped", outcome.flag_reasons)
        supp = frappe.db.get_value(
            "Supplier", outcome.supplier_docname,
            ["gstin", "gst_category"], as_dict=True,
        )
        self.assertFalse(supp.gstin)
        self.assertEqual(supp.gst_category, "Unregistered")

    def test_indian_invalid_gstin_lands_as_fnc(self) -> None:
        """The captured Harmony vendors all have dirty placeholder
        GSTINs (e.g. '29LBAFB4243P4X1' — pos 14 is X, not Z). India
        Compliance throws → flow rolls back partial inserts → FNC
        Map row inserted; no Supplier exists."""
        row = {
            "vendor_c_id": 997002, "vendor_code": "VN-T-DIRTY",
            "vendor_name": "TEST-PULL-Dirty-GSTIN",
            "tax_identification_number": "29LBAFB4243P4X1",  # bad pos 14
            "pan": "LBAFB4243P", "email": "dirty@test.local",
            "contact_number": "9999997002", "active": 1, "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "X", "city": "X", "state_name": "Karnataka",
                    "zip": "560035", "country": "India",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertEqual(outcome.status, "Flagged-Not-Created")
        self.assertEqual(outcome.operation, "flagged")
        self.assertIsNone(outcome.supplier_docname)
        self.assertTrue(outcome.flag_reasons)
        # FNC Map row exists and carries the validator tag.
        fnc = frappe.db.get_value(
            "EasyEcom Supplier Map", "ECS-SUPP-997002",
            ["status", "flag_reason", "ee_vendor_id"], as_dict=True,
        )
        self.assertEqual(fnc.status, "Flagged-Not-Created")
        self.assertTrue(fnc.flag_reason.startswith("ic_"))
        # vendor_id (write key) IS captured even on FNC — so a later
        # retry / fix-and-push can use it.
        self.assertEqual(fnc.ee_vendor_id, "VN-T-DIRTY")

    def test_fnc_rolls_back_partial_supplier_insert(self) -> None:
        """No Supplier should exist after an FNC outcome — the cleanup
        path runs before the Map row is written."""
        row = {
            "vendor_c_id": 997003, "vendor_code": "VN-T-DIRTY-2",
            "vendor_name": "TEST-PULL-RollBack",
            "tax_identification_number": "29INVALID12345X",
            "pan": "", "email": "rb@test.local",
            "contact_number": "", "active": 1, "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "X", "city": "X", "state_name": "Karnataka",
                    "zip": "560035", "country": "India",
                },
                "dispatch": [],
            },
        }
        process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertFalse(
            frappe.db.exists(
                "Supplier", {"supplier_name": "TEST-PULL-RollBack"}
            )
        )


class TestSyncRecordWrites(FrappeTestCase):
    """One Sync Record (direction=Pull, entity_type=Supplier) per
    Mapped/Disabled outcome; none for FNC (Dynamic Link can't resolve)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()

    def setUp(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def _executor(self) -> FieldMappingExecutor:
        return FieldMappingExecutor(SUPPLIER_PULL_RULESET)

    def test_sync_record_written_for_mapped_supplier(self) -> None:
        row = {
            "vendor_c_id": 996001, "vendor_code": "VN-SR-001",
            "vendor_name": "TEST-PULL-Valid-GSTIN-Delhi",
            "tax_identification_number": VALID_GSTIN_DELHI, "pan": "",
            "email": "sr@test.local", "contact_number": "9999996001",
            "active": 1, "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "X", "city": "Delhi", "state_name": "Delhi",
                    "zip": "110001", "country": "India",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        sr = frappe.db.get_value(
            "EasyEcom Sync Record",
            {
                "entity_doctype": "Supplier",
                "entity_name": outcome.supplier_docname,
                "direction": "Pull",
            },
            ["status", "entity_type"],
            as_dict=True,
        )
        self.assertIsNotNone(sr)
        self.assertEqual(sr.status, "Success")
        self.assertEqual(sr.entity_type, ENTITY_TYPE_SUPPLIER)

    def test_no_sync_record_for_fnc(self) -> None:
        row = {
            "vendor_c_id": 996002, "vendor_code": "VN-SR-FNC",
            "vendor_name": "TEST-PULL-FNC",
            "tax_identification_number": "29LBAFB4243P4X1",  # IC will throw
            "pan": "", "email": "fnc-sr@test.local",
            "contact_number": "9999996002", "active": 1, "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "X", "city": "X", "state_name": "Karnataka",
                    "zip": "560035", "country": "India",
                },
                "dispatch": [],
            },
        }
        outcome = process_one_supplier(
            row, executor=self._executor(), account_mode=MODE_ONBOARDING
        )
        self.assertEqual(outcome.status, "Flagged-Not-Created")
        # No SR row anywhere targeting an FNC vendor.
        sr_count = frappe.db.count(
            "EasyEcom Sync Record",
            {"entity_doctype": "Supplier", "direction": "Pull"},
        )
        # If there were any SR rows from prior tests they were wiped
        # by setUp; this FNC pull must add zero.
        self.assertEqual(sr_count, 0)


class TestCursorWalk(FrappeTestCase):
    """pull_suppliers walks ALL pages via nextUrl. The cursor advances
    page-by-page on the EasyEcom Account; a clean walk stamps the
    completion timestamp + total."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()
        cls.pages = _load_pages_fixture()

    def setUp(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()
        # Ensure a Harmony-like enabled account exists.
        if not frappe.db.exists("EasyEcom Account", "Harmony"):
            make_account(name="Harmony", enabled=False)
        # The "single enabled" invariant: Harmony is enabled on the
        # live site already (live smoke). For test isolation we use
        # the account directly via the `account=` param.

    def tearDown(self) -> None:
        _wipe_supplier_maps()
        _wipe_test_suppliers()

    def test_walk_uses_all_pages_and_persists_cursor(self) -> None:
        client = _make_mock_client(copy.deepcopy(self.pages))
        outcome = pull_suppliers(
            client=client, account="Harmony", start_fresh=True
        )
        self.assertEqual(outcome.pages_walked, 2)
        # Total = sum of all rows across pages
        expected_total = sum(len(p.get("data") or []) for p in self.pages)
        self.assertEqual(outcome.total, expected_total)
        # Final cursor is None (clean walk).
        self.assertIsNone(outcome.final_cursor)
        # Account state — clean walk bumps completion + total.
        acct = frappe.db.get_value(
            "EasyEcom Account", "Harmony",
            [
                "supplier_pull_cursor",
                "supplier_pull_last_updated_at",
                "supplier_pull_total_seen",
            ],
            as_dict=True,
        )
        self.assertFalse((acct.supplier_pull_cursor or "").strip())
        self.assertIsNotNone(acct.supplier_pull_last_updated_at)
        self.assertEqual(int(acct.supplier_pull_total_seen), expected_total)

    def test_real_harmony_payload_produces_mostly_fnc_due_to_dirty_gstin(self) -> None:
        """Sanity: the captured 30 vendors have placeholder GSTINs that
        all fail India Compliance. The flow should produce a high
        FNC count + low Mapped count + low failure count (FNC is a
        successful decision, not a failure)."""
        client = _make_mock_client(copy.deepcopy(self.pages))
        outcome = pull_suppliers(
            client=client, account="Harmony", start_fresh=True
        )
        self.assertEqual(outcome.failed, 0)
        # Real captured data: most rows go FNC, a few may go Mapped
        # (the ones with blank GSTIN go through as Unregistered).
        self.assertGreater(outcome.flagged_not_created, 0)


class TestEmptyResponseValidator(FrappeTestCase):
    """The §8f Stage 3 validator-fix: HTTP 200 + body code 400 +
    no-data message is success-empty, not EasyEcomValidationError."""

    def test_states_no_data_message_is_success_empty(self) -> None:
        """Hit the demote path via a direct probe.
        EE returns `{"code": 400, "message": "Unable to find states ..."}`
        for empty-state territories. The client's no-data detector
        should keep classification as 'success' — caller gets the body
        back, downstream `(resp or {}).get("states") or []` yields []."""
        from ecommerce_super.easyecom.client.client import (
            _is_no_data_envelope,
        )
        # Real-shape body from the live probe.
        body = {
            "code": 400,
            "message": "Unable to find states for given country Id",
        }
        self.assertTrue(_is_no_data_envelope(body))

    def test_vendors_no_data_message_is_success_empty(self) -> None:
        from ecommerce_super.easyecom.client.client import (
            _is_no_data_envelope,
        )
        body = {"code": 400, "message": "Unable to find vendors"}
        self.assertTrue(_is_no_data_envelope(body))

    def test_no_data_found_legacy_phrasing_is_success_empty(self) -> None:
        from ecommerce_super.easyecom.client.client import (
            _is_no_data_envelope,
        )
        body = {"code": 400, "message": "No Data Found"}
        self.assertTrue(_is_no_data_envelope(body))

    def test_real_validation_error_still_raises(self) -> None:
        """A real validation error like "Invalid input" must NOT be
        demoted-to-success. Critical safety check."""
        from ecommerce_super.easyecom.client.client import (
            _is_no_data_envelope,
        )
        for real_error_msg in (
            "Invalid input",
            "Missing required field 'vendor_code'",
            "Authentication failed",
            "Unauthorized",
        ):
            self.assertFalse(
                _is_no_data_envelope(
                    {"code": 400, "message": real_error_msg}
                ),
                f"real error '{real_error_msg}' must NOT be treated as no-data",
            )

    def test_no_data_envelope_robust_to_missing_message(self) -> None:
        from ecommerce_super.easyecom.client.client import (
            _is_no_data_envelope,
        )
        self.assertFalse(_is_no_data_envelope({"code": 400}))
        self.assertFalse(_is_no_data_envelope({}))
        self.assertFalse(_is_no_data_envelope(None))
        self.assertFalse(_is_no_data_envelope([]))


class TestStaleSupplierSyncDoesNotRun(FrappeTestCase):
    """active=0 on the retired Supplier-Sync ruleset means the engine
    won't compile or invoke it. Sanity check that compiling the
    PULL ruleset works AND that the retired one is excluded from
    Pull-direction lookups."""

    def test_pull_ruleset_compiles(self) -> None:
        FieldMappingExecutor(SUPPLIER_PULL_RULESET)  # should not raise

    def test_active_supplier_pull_rulesets_excludes_retired_sync(self) -> None:
        """Engine selects by active=1; the retired Sync ruleset must
        not appear in any direction-Pull lookup for Supplier entity."""
        active_pulls = frappe.db.get_all(
            "EasyEcom Field Mapping",
            filters={
                "entity_type": "Supplier",
                "direction": "Pull",
                "active": 1,
            },
            pluck="name",
        )
        self.assertIn("EasyEcom-Supplier-Pull", active_pulls)
        self.assertNotIn("EasyEcom-Supplier-Sync", active_pulls)


_DUP_PREFIX = "TEST-8F-DUP-"


class TestSupplierDupNameResilience(FrappeTestCase):
    """Regression for the blank-site smoke finding 2026-05-27:
    Harmony's sandbox has multiple vendors sharing the same
    `vendor_name` (e.g. 'MSTEST_123', 'Akanksha', 'library'). On a
    fresh ERPNext install (FrappeCloud staging, blank smoke site),
    Buying Settings.supp_master_name defaults to "Supplier Name" —
    meaning Supplier.name = supplier_name. So the second insert of a
    same-named vendor collides on Supplier.name and raises
    DuplicateEntryError. The fix in supplier_pull._create_supplier
    retries once with a vendor_c_id suffix on the supplier_name;
    both rows end up distinct, the Supplier Map carries the real
    vendor_c_id as the join key, and the FDE sees both in the list.

    This test temporarily forces supp_master_name='Supplier Name'
    (the FrappeCloud / fresh-install default) so the dup behaviour
    is deterministic regardless of the dev site's customised naming
    config."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_lookups_cache()

    def setUp(self) -> None:
        # Force Supplier autoname=field:supplier_name to match the
        # FrappeCloud / fresh-ERPNext default — that's where the dup-
        # name bug actually fires (this dev site uses 'Naming Series',
        # so without the override the test would pass vacuously).
        self._orig_supp_master_name = frappe.db.get_single_value(
            "Buying Settings", "supp_master_name"
        )
        frappe.db.set_single_value(
            "Buying Settings", "supp_master_name", "Supplier Name"
        )
        # ERPNext's Supplier.autoname_from() reads the meta's autoname
        # field via Buying Settings — we must force the doctype's
        # autoname property too, since the Supplier controller reads
        # it on insert.
        self._orig_autoname = frappe.get_meta("Supplier").autoname
        frappe.db.set_value(
            "DocType", "Supplier", "autoname", "field:supplier_name",
            update_modified=False,
        )
        frappe.db.commit()
        frappe.clear_cache(doctype="Supplier")

        _wipe_supplier_maps()
        for n in frappe.db.get_all(
            "Supplier",
            filters={"supplier_name": ("like", f"{_DUP_PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc("Supplier", n, force=True, ignore_permissions=True)
            except Exception:
                pass
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe_supplier_maps()
        for n in frappe.db.get_all(
            "Supplier",
            filters={"supplier_name": ("like", f"{_DUP_PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc("Supplier", n, force=True, ignore_permissions=True)
            except Exception:
                pass
        # Restore the dev site's original naming preference.
        if self._orig_supp_master_name is not None:
            frappe.db.set_single_value(
                "Buying Settings",
                "supp_master_name",
                self._orig_supp_master_name,
            )
        frappe.db.set_value(
            "DocType", "Supplier", "autoname",
            self._orig_autoname or "",
            update_modified=False,
        )
        frappe.clear_cache(doctype="Supplier")
        frappe.db.commit()

    def _row(self, *, c_id: str, name: str, vendor_code: str) -> dict:
        return {
            "vendor_c_id": c_id,
            "vendor_code": vendor_code,
            "vendor_name": name,
            "tax_identification_number": VALID_GSTIN_DELHI,
            "pan": "ABCDE1234F",
            "email": f"{vendor_code.lower()}@test.local",
            "contact_number": "9999900001",
            "active": 1,
            "currency_code": "INR",
            "address": {
                "billing": {
                    "address": "1 Test",
                    "city": "Delhi",
                    "state_name": "Delhi",
                    "zip": "110001",
                    "country": "India",
                },
                "dispatch": [],
            },
        }

    def test_dup_name_second_supplier_disambiguates_via_vendor_c_id(self) -> None:
        executor = FieldMappingExecutor(SUPPLIER_PULL_RULESET)
        dup_name = f"{_DUP_PREFIX}DupName"
        # vendor_c_id is Int in EE — the ruleset's int_to_str
        # transform enforces that. Use big-enough numbers that won't
        # collide with prior smoke artifacts.
        c_id_a, c_id_b = 950001, 950002

        # First row — happy path.
        out1 = process_one_supplier(
            self._row(c_id=c_id_a, name=dup_name, vendor_code="VC-A"),
            executor=executor, account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(out1.status, "Mapped")
        first_docname = out1.supplier_docname
        self.assertEqual(
            frappe.db.get_value("Supplier", first_docname, "supplier_name"),
            dup_name,
            "first Supplier keeps the EE vendor_name verbatim",
        )

        # Second row — DIFFERENT vendor_c_id, SAME vendor_name. Without
        # the fix this raised DuplicateEntryError; with the fix it
        # disambiguates.
        out2 = process_one_supplier(
            self._row(c_id=c_id_b, name=dup_name, vendor_code="VC-B"),
            executor=executor, account_mode=MODE_ONBOARDING,
        )
        self.assertEqual(out2.status, "Mapped")
        self.assertEqual(out2.operation, "created")
        self.assertNotEqual(out2.supplier_docname, first_docname)
        self.assertEqual(
            frappe.db.get_value("Supplier", out2.supplier_docname, "supplier_name"),
            f"{dup_name} ({c_id_b})",
            "second Supplier's supplier_name carries the vendor_c_id "
            "suffix so the FDE can distinguish both rows",
        )

        # Map row's join key is still the read-side vendor_c_id —
        # downstream PO/GRN lookups via the Map are unaffected.
        m = frappe.db.get_value(
            "EasyEcom Supplier Map",
            {
                "erpnext_doctype": "Supplier",
                "erpnext_name": out2.supplier_docname,
            },
            ["ee_vendor_c_id", "ee_vendor_id"],
            as_dict=True,
        )
        self.assertEqual(m.ee_vendor_c_id, str(c_id_b))
        self.assertEqual(m.ee_vendor_id, "VC-B")
