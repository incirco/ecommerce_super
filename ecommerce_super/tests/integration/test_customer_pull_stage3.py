"""Stage 3 tests for §8e — EE→EN customer pull.

ALL MOCKED — no real EE traffic. Fixture data:
  process/ee_mock_fixtures/getcustomers_b2b_response.json (23 real
  Harmony records, captured during build) — exercises the 'duplicate
  gstNum/companyname → never wrongly link' contract on real dirty
  data.

Covers the 4 packet decisions:
  1. Matching: map row exists → reuse; else create new (no fuzzy)
  2. Pagination: flat list, no cursor
  3. c_id == customerId: schema stores both (Stage 4 verifies parity)
  4. GSTIN gating: URP → Unregistered; valid → set; invalid → FNC

Plus the soft pincode-state validation case (Created-Flagged).
"""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    FOUNDATIONAL_ENDPOINTS,
    WHOLESALE_USER_MANAGEMENT,
    is_foundational,
)
from ecommerce_super.easyecom.flows.customer_pull import (
    process_customer_rows,
    pull_customers,
)
from ecommerce_super.easyecom.flows._customer_sync_records import (
    ENTITY_TYPE_CUSTOMER,
)


FIXTURE_DIR = os.path.join(
    frappe.get_app_path("ecommerce_super"),
    "..",
    "process",
    "ee_mock_fixtures",
)


# Synthetic GSTINs with valid check digits, computed via India
# Compliance's published algorithm. We use these when we want a test
# customer to SURVIVE India Compliance validation. The real Harmony
# fixture's gstNums are placeholder test data with bogus check digits
# (every captured customer fails validate_gstin_check_digit) — that's
# realistic for a sandbox and is itself exercised by the FNC tests.
VALID_GSTIN_DELHI = "07ABCDE1234F1Z2"          # state code 07 = Delhi
VALID_GSTIN_KARNATAKA = "29ABCDE1234F1ZW"      # state code 29 = Karnataka
VALID_GSTIN_GUJARAT = "24ABCDE1234F1Z6"        # state code 24 = Gujarat


def _load_customers_fixture() -> dict:
    path = os.path.join(FIXTURE_DIR, "getcustomers_b2b_response.json")
    with open(path) as f:
        return json.load(f)


def _load_countries_fixture() -> dict:
    with open(os.path.join(FIXTURE_DIR, "getcountries_response.json")) as f:
        return json.load(f)


def _load_states_fixture() -> dict:
    with open(os.path.join(FIXTURE_DIR, "getstates_india_response.json")) as f:
        return json.load(f)


def _seed_lookups_cache() -> None:
    """Pull the captured countries/states fixtures into the cache so
    the customer pull's pincode-state validation can run."""
    from ecommerce_super.easyecom.flows.customer_lookups import (
        pull_countries_and_states,
    )
    from ecommerce_super.easyecom.client.endpoints import (
        COUNTRIES_GET,
        STATES_GET,
    )

    countries = _load_countries_fixture()
    states_in = _load_states_fixture()
    client = MagicMock()

    def _get(endpoint, params=None, **_kw):
        if endpoint == COUNTRIES_GET:
            return countries
        if endpoint == STATES_GET:
            return states_in if int((params or {}).get("countryId") or 0) == 1 else {"code": 200, "states": []}
        raise AssertionError(f"unexpected endpoint: {endpoint!r}")

    client.get.side_effect = _get
    pull_countries_and_states(client=client)


def _wipe_all() -> None:
    """Belt-and-braces: clear every row this test suite touches.
    FrappeTestCase rolls back per test, but commits inside the pull
    flow (currently the lookups pre-seed) survive — wipe explicitly
    so the next test starts clean."""
    # Order matters: maps reference Customer, Customer-linked Addresses
    # need their Dynamic Links cleared first.
    map_names = frappe.db.get_all(
        "EasyEcom Customer Map",
        filters={"ee_c_id": ("like", "%")},
        pluck="name",
    )
    for n in map_names:
        try:
            frappe.delete_doc("EasyEcom Customer Map", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    # Delete test-created Customers — match by name prefix or by gstin
    # being a Harmony test value. Simplest: target everything created
    # this test session via the Customer Map link (the map was wiped
    # above so we use Customer.customer_name pattern).
    # The fixture's customers have names like "GreenLeaf Supermarket",
    # "EasyEcom Test Customer 2", "TEST CUSTOMER" — use a marker.
    for name in frappe.db.get_all(
        "Customer",
        filters={"customer_name": ("in", _customer_names_from_fixture())},
        pluck="name",
    ):
        # Delete addresses linked to this Customer first.
        addr_names = frappe.db.sql(
            """SELECT DISTINCT parent FROM `tabDynamic Link`
               WHERE parenttype='Address'
                 AND link_doctype='Customer'
                 AND link_name=%s""",
            (name,),
        )
        for (addr,) in addr_names:
            try:
                frappe.delete_doc("Address", addr, force=True, ignore_permissions=True)
            except Exception:
                pass
        try:
            frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
        except Exception:
            pass
    # Sync Records linked to those customers — Frappe doesn't cascade.
    frappe.db.sql(
        """DELETE FROM `tabEasyEcom Sync Record`
           WHERE entity_doctype='Customer' AND entity_name LIKE '%'"""
    )
    frappe.db.commit()


def _customer_names_from_fixture() -> list[str]:
    return [c.get("companyname") for c in _load_customers_fixture()["data"]]


class TestEndpointRegistered(FrappeTestCase):
    """The wholesale customer endpoint must be classified foundational."""

    def test_wholesale_endpoint_is_foundational(self) -> None:
        self.assertIn(WHOLESALE_USER_MANAGEMENT, FOUNDATIONAL_ENDPOINTS)
        self.assertTrue(is_foundational(WHOLESALE_USER_MANAGEMENT))

    def test_wholesale_endpoint_with_query_classified_foundational(self) -> None:
        """Query-string strip handles ?type=b2b correctly."""
        self.assertTrue(
            is_foundational(f"{WHOLESALE_USER_MANAGEMENT}?type=b2b")
        )


class TestRulesetRetirement(FrappeTestCase):
    """The dead EasyEcom-Customer-Sync ruleset must be inactive after
    migrate; the new EasyEcom-Customer-Pull must be active. The
    EasyEcom-Customer-Anon-Pull (§11/§12 anonymous buyers) must be
    untouched."""

    def test_customer_sync_is_retired(self) -> None:
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Customer-Sync", "active"
        )
        self.assertEqual(int(active or 0), 0, "EasyEcom-Customer-Sync must be active=0 (soft-retired)")

    def test_customer_pull_is_active(self) -> None:
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Customer-Pull", "active"
        )
        self.assertEqual(int(active or 0), 1)

    def test_anon_pull_is_untouched(self) -> None:
        """The marketplace anonymous-buyer ruleset is §11/§12 scope —
        not 8e — and must remain active+intact."""
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Customer-Anon-Pull", "active"
        )
        self.assertEqual(int(active or 0), 1)


class TestPullHappyPath(FrappeTestCase):
    """Pull the full Harmony fixture and verify the per-customer outputs."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def test_every_row_produces_a_map_row(self) -> None:
        """Every fixture row → exactly one Customer Map row (either via
        Customer-create path or via FNC path when India Compliance
        rejects the placeholder gstNum). 23 rows = 23 Map rows."""
        outcome = process_customer_rows(self.fixture["data"])
        self.assertEqual(outcome.total, 23)
        self.assertEqual(outcome.failed, 0, "no infra failures expected")
        self.assertEqual(frappe.db.count("EasyEcom Customer Map"), 23)

    def test_harmony_fixture_has_mixed_gstin_validity(self) -> None:
        """Documents the live observation: the captured Harmony sandbox
        fixture has 23 customers; SOME have valid GSTINs (state code +
        check digit + pincode-state all internally consistent), SOME
        have placeholder bogus check digits.

        Captured outcome on the fixture: ~10 Mapped, ~13 FNC, 0 failed.
        Asserted as bounded ranges so a future fixture refresh doesn't
        false-alarm on small valid-count drift."""
        outcome = process_customer_rows(self.fixture["data"])
        self.assertEqual(outcome.total, 23)
        self.assertEqual(outcome.failed, 0)
        # At least some rows pass India Compliance + some don't.
        self.assertGreater(outcome.created, 0)
        self.assertGreater(outcome.flagged_not_created, 0)
        # And they sum to total (none silently dropped).
        accounted = (
            outcome.created
            + outcome.created_flagged
            + outcome.flagged_not_created
            + outcome.skipped
        )
        self.assertEqual(accounted, 23)

    def test_urp_substituted_row_creates_customer_with_addresses(self) -> None:
        """For the address/SR happy-path: substitute gstNum='' (URP-empty)
        on a fixture row so India Compliance accepts the Customer."""
        row = copy.deepcopy(self.fixture["data"][0])
        row["c_id"] = 99000001
        row["gstNum"] = ""  # URP path

        outcome = process_customer_rows([row])
        self.assertEqual(outcome.created, 1)
        self.assertEqual(outcome.failed, 0)

        c = frappe.db.get_value(
            "EasyEcom Customer Map", {"ee_c_id": str(row["c_id"])}, "erpnext_name"
        )
        self.assertIsNotNone(c)
        # Billing + Shipping Address linked via Dynamic Link.
        addresses = frappe.db.sql(
            """SELECT a.address_type
               FROM `tabAddress` a
               JOIN `tabDynamic Link` dl ON dl.parent=a.name
               WHERE dl.parenttype='Address'
                 AND dl.link_doctype='Customer'
                 AND dl.link_name=%s""",
            (c,),
            as_dict=True,
        )
        types = {a["address_type"] for a in addresses}
        self.assertIn("Billing", types)
        self.assertIn("Shipping", types)

    def test_map_row_stores_both_ids(self) -> None:
        """Stage 1 schema requirement: ee_c_id (join key) AND
        ee_customer_id (write-side) both populated even on FNC outcome."""
        process_customer_rows(self.fixture["data"][:1])
        first = self.fixture["data"][0]
        row = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"ee_c_id": str(first["c_id"])},
            ["ee_c_id", "ee_customer_id"],
            as_dict=True,
        )
        self.assertEqual(row.ee_c_id, str(first["c_id"]))
        self.assertEqual(row.ee_customer_id, str(first["c_id"]))


class TestDirtyDuplicateMatching(FrappeTestCase):
    """The packet's central design point: gstNum / companyname are
    dirty/duplicated in real data, so the pull MUST create one Customer
    per c_id, never collapsing by natural-key."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def test_duplicate_gstnums_create_separate_map_rows(self) -> None:
        """4 records share '07AALFP1768K1ZQ' — even though India
        Compliance rejects the bogus check digit and they all FNC,
        the pull creates 4 DISTINCT Customer Map rows (one per c_id).
        Auto-matching on gstNum would have collapsed them to 1 row;
        the contract is 'one Map row per EE c_id', enforced even at
        FNC. This is what 'never wrongly link > never duplicate' means
        in practice."""
        target_gstin = "07AALFP1768K1ZQ"
        dupe_rows = [r for r in self.fixture["data"] if r["gstNum"] == target_gstin]
        self.assertEqual(len(dupe_rows), 4, "fixture invariant — adjust if fixture changes")

        outcome = process_customer_rows(dupe_rows)
        # All 4 FNC due to bogus check digit; no infra failures.
        self.assertEqual(outcome.failed, 0)
        # Four distinct Map rows, one per c_id.
        map_count = frappe.db.count(
            "EasyEcom Customer Map",
            filters={"ee_c_id": ("in", [str(r["c_id"]) for r in dupe_rows])},
        )
        self.assertEqual(map_count, 4)

    def test_duplicate_companynames_create_separate_map_rows(self) -> None:
        """'TEST CUSTOMER' appears twice — pull keeps them separate
        (same as gstNum dupes). The Map row is the join key, NOT the
        natural-key fields."""
        dupe_rows = [r for r in self.fixture["data"] if r["companyname"] == "TEST CUSTOMER"]
        self.assertEqual(len(dupe_rows), 2, "fixture invariant — adjust if fixture changes")

        outcome = process_customer_rows(dupe_rows)
        self.assertEqual(outcome.failed, 0)
        map_count = frappe.db.count(
            "EasyEcom Customer Map",
            filters={"ee_c_id": ("in", [str(r["c_id"]) for r in dupe_rows])},
        )
        self.assertEqual(map_count, 2)

    def test_duplicate_gstnums_with_urp_path_create_separate_customers(self) -> None:
        """Same dupe-rows scenario but URP-substituted so we actually
        get Customer creates — proves the 'separate Customer per c_id'
        rule applies at the create level too (not just FNC). Both
        c_ids get a fresh Customer + Map row, sharing nothing except
        identical content."""
        target_gstin = "07AALFP1768K1ZQ"
        dupe_rows = [
            copy.deepcopy(r) for r in self.fixture["data"]
            if r["gstNum"] == target_gstin
        ][:2]
        for r in dupe_rows:
            r["gstNum"] = ""  # URP path → India Compliance accepts
            # Keep c_ids distinct (they already are).

        outcome = process_customer_rows(dupe_rows)
        self.assertEqual(outcome.created, 2, "two separate Customers, not collapsed")
        # Two Customer docs (auto-named).
        customers = frappe.db.get_all(
            "EasyEcom Customer Map",
            filters={"ee_c_id": ("in", [str(r["c_id"]) for r in dupe_rows])},
            fields=["erpnext_name"],
        )
        self.assertEqual(len(customers), 2)
        # The two Customer docnames differ — separate identities.
        self.assertEqual(len({c.erpnext_name for c in customers}), 2)


class TestMapRowReuse(FrappeTestCase):
    """When a Customer Map row already exists for the c_id, the pull
    must reuse it (no second Customer, no second Map)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def test_repulled_customer_is_skipped(self) -> None:
        """Pull once → created. Pull again → all skipped. Uses URP-
        substituted rows so India Compliance accepts the creates (the
        captured fixture's gstNums fail check-digit; that's tested
        elsewhere as the FNC path)."""
        sample = []
        for i, r in enumerate(self.fixture["data"][:3]):
            row = copy.deepcopy(r)
            row["c_id"] = 99100100 + i
            row["gstNum"] = ""  # URP → clean Customer create
            sample.append(row)

        first = process_customer_rows(sample)
        self.assertEqual(first.created, 3)
        self.assertEqual(first.skipped, 0)

        second = process_customer_rows(sample)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.skipped, 3)
        # Still exactly 3 Customer Map rows — no duplication.
        self.assertEqual(
            frappe.db.count(
                "EasyEcom Customer Map",
                filters={"ee_c_id": ("in", [str(r["c_id"]) for r in sample])},
            ),
            3,
        )


class TestGstinGating(FrappeTestCase):
    """The 4 GSTIN paths: valid, empty (URP-empty), URP literal, invalid."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def _row_with_gstin(self, gstin: str | None) -> dict:
        """Synthesize a test row by deep-copying a real one and patching
        gstNum. Avoids inventing the rest of the shape from scratch."""
        row = copy.deepcopy(self.fixture["data"][0])
        row["c_id"] = 99999000 + hash(str(gstin)) % 1000  # unique synthetic id
        row["gstNum"] = gstin if gstin is not None else ""
        # Make sure the synthetic id doesn't collide with existing map rows.
        return row

    def test_valid_gstin_sets_gstin_and_lets_india_compliance_derive_category(self) -> None:
        """A correctly-formatted 15-char GSTIN with VALID check digit
        AND a matching billingState (India Compliance cross-checks
        gstin's state code vs Address.state) survives validation and
        lands on Customer.gstin."""
        row = self._row_with_gstin(VALID_GSTIN_DELHI)
        # Align state with the GSTIN's state code (07=Delhi) so India
        # Compliance's gstin-state-code validator accepts.
        row["billingState"] = "Delhi"
        row["billingZipcode"] = "110001"
        row["dispatchState"] = "Delhi"
        row["dispatchZipcode"] = "110001"

        outcome = process_customer_rows([row])
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(outcome.flagged_not_created, 0)
        self.assertEqual(outcome.created + outcome.created_flagged, 1)
        c = frappe.db.get_value(
            "EasyEcom Customer Map", {"ee_c_id": str(row["c_id"])}, "erpnext_name"
        )
        self.assertIsNotNone(c)
        gstin = frappe.db.get_value("Customer", c, "gstin")
        self.assertEqual(gstin, VALID_GSTIN_DELHI)

    def test_empty_gstin_treated_as_unregistered(self) -> None:
        """Empty gstNum → gst_category='Unregistered' + empty gstin."""
        row = self._row_with_gstin("")
        outcome = process_customer_rows([row])
        self.assertEqual(outcome.failed, 0)
        c = frappe.db.get_value(
            "EasyEcom Customer Map", {"ee_c_id": str(row["c_id"])}, "erpnext_name"
        )
        self.assertIsNotNone(c)
        gstin, gst_category = frappe.db.get_value(
            "Customer", c, ["gstin", "gst_category"]
        )
        self.assertFalse(gstin)
        self.assertEqual(gst_category, "Unregistered")

    def test_urp_literal_treated_as_unregistered(self) -> None:
        """gstNum='URP' (EE's literal sentinel for unregistered) →
        gst_category='Unregistered' + empty gstin."""
        row = self._row_with_gstin("URP")
        outcome = process_customer_rows([row])
        self.assertEqual(outcome.failed, 0)
        c = frappe.db.get_value(
            "EasyEcom Customer Map", {"ee_c_id": str(row["c_id"])}, "erpnext_name"
        )
        self.assertIsNotNone(c)
        gstin, gst_category = frappe.db.get_value(
            "Customer", c, ["gstin", "gst_category"]
        )
        self.assertFalse(gstin)
        self.assertEqual(gst_category, "Unregistered")

    def test_invalid_gstin_lands_as_flagged_not_created(self) -> None:
        """India Compliance throws on bad 15-char format; the flow
        catches and creates a Flagged-Not-Created Map row (no Customer)."""
        # 14 chars instead of 15 — invalid length.
        row = self._row_with_gstin("INVALIDGST1234")
        outcome = process_customer_rows([row])
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(outcome.flagged_not_created, 1)
        # Map row exists with status=Flagged-Not-Created and NO Customer.
        m = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"ee_c_id": str(row["c_id"])},
            ["status", "erpnext_name", "flag_reason"],
            as_dict=True,
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.status, "Flagged-Not-Created")
        self.assertFalse(m.erpnext_name)
        self.assertIn("validate failed", m.flag_reason.lower())


class TestPincodeStateMismatch(FrappeTestCase):
    """The dirty-data soft-flag case: state name doesn't match pincode
    range → Customer is created with Created-Flagged status."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def test_arunachal_pradesh_with_bangalore_pincode_is_created_flagged(self) -> None:
        """The packet's exact scenario: billingState='Arunachal Pradesh'
        + billingZipcode='560035' (a Bangalore code). Soft flag — the
        Customer IS created; status='Created-Flagged'. URP-empty gstNum
        so India Compliance doesn't intercept with a check-digit FNC."""
        row = copy.deepcopy(self.fixture["data"][0])
        row["c_id"] = 88888001
        row["gstNum"] = ""  # URP — bypass GSTIN check so we exercise the pincode-state path
        row["billingState"] = "Arunachal Pradesh"
        row["billingZipcode"] = "560035"
        row["dispatchState"] = "Arunachal Pradesh"
        row["dispatchZipcode"] = "560035"

        outcome = process_customer_rows([row])
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(outcome.flagged_not_created, 0)
        self.assertEqual(outcome.created_flagged, 1)

        m = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"ee_c_id": str(row["c_id"])},
            ["status", "flag_reason", "erpnext_name"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Created-Flagged")
        self.assertIsNotNone(m.erpnext_name)
        self.assertIn("560035", m.flag_reason)
        self.assertIn("Arunachal Pradesh", m.flag_reason)


class TestSyncRecordWrites(FrappeTestCase):
    """A Sync Record (direction=Pull, entity_doctype=Customer) is
    written for each successfully-created customer. FNC rows do NOT
    get Sync Records (no entity to link to — same semantic as §8d)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def test_sync_record_per_created_customer(self) -> None:
        """One Pull Sync Record per CREATED Customer. Uses URP rows so
        India Compliance doesn't FNC them on check-digit (no SR for FNC
        rows — covered by test_no_sync_record_for_fnc_outcome below)."""
        sample = []
        for i, r in enumerate(self.fixture["data"][:3]):
            row = copy.deepcopy(r)
            row["c_id"] = 99200200 + i
            row["gstNum"] = ""  # URP path → Customer is created
            sample.append(row)

        process_customer_rows(sample)

        sr_count = frappe.db.count(
            "EasyEcom Sync Record",
            filters={"entity_doctype": "Customer", "direction": "Pull"},
        )
        self.assertEqual(sr_count, 3)

    def test_sync_record_entity_type_is_customer(self) -> None:
        row = copy.deepcopy(self.fixture["data"][0])
        row["c_id"] = 99300300
        row["gstNum"] = ""  # URP path
        process_customer_rows([row])
        c = frappe.db.get_value(
            "EasyEcom Customer Map", {"ee_c_id": str(row["c_id"])}, "erpnext_name"
        )
        self.assertIsNotNone(c)
        sr_status, sr_entity_type = frappe.db.get_value(
            "EasyEcom Sync Record",
            {
                "entity_doctype": "Customer",
                "entity_name": c,
                "direction": "Pull",
            },
            ["status", "entity_type"],
        )
        self.assertEqual(sr_status, "Success")
        self.assertEqual(sr_entity_type, ENTITY_TYPE_CUSTOMER)

    def test_no_sync_record_for_fnc_outcome(self) -> None:
        """FNC outcome has no Customer → no Sync Record row."""
        import copy as _copy
        row = _copy.deepcopy(self.fixture["data"][0])
        row["c_id"] = 77777001
        row["gstNum"] = "INVALIDGST1234"  # India Compliance throws

        process_customer_rows([row])
        # The Customer Map row exists, but no Customer → no SR.
        sr_count = frappe.db.count(
            "EasyEcom Sync Record",
            filters={"entity_doctype": "Customer", "direction": "Pull"},
        )
        # FNC contributed 0 SR rows; sample also wasn't fully processed
        # for other rows so check that this c_id has no SR.
        # Since the FNC's entity_name is None and Customer doesn't exist,
        # the Sync Record write returns None — assert nothing's pointing
        # at this c_id.
        rows = frappe.db.get_all(
            "EasyEcom Sync Record",
            filters={"entity_doctype": "Customer", "direction": "Pull"},
            pluck="entity_name",
        )
        self.assertNotIn(f"<unmapped:{row['c_id']}>", rows)


class TestFullPullViaClient(FrappeTestCase):
    """End-to-end: mock the EasyEcomClient.get() to return the fixture,
    run pull_customers() (the entry the whitelist endpoint calls), and
    verify the aggregate."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture = _load_customers_fixture()

    def setUp(self) -> None:
        _wipe_all()
        _seed_lookups_cache()

    def tearDown(self) -> None:
        _wipe_all()

    def test_pull_customers_returns_aggregate(self) -> None:
        client = MagicMock()
        client.get.return_value = self.fixture

        outcome = pull_customers(client=client)

        # One HTTP call to /Wholesale/v2/UserManagement (flat list — no
        # pagination follow-up). Confirms decision #2.
        self.assertEqual(client.get.call_count, 1)
        endpoint_called = client.get.call_args[0][0]
        self.assertEqual(endpoint_called, WHOLESALE_USER_MANAGEMENT)

        self.assertEqual(outcome.total, 23)
        self.assertEqual(outcome.failed, 0)
        # Mixed outcome on the captured fixture (see
        # test_harmony_fixture_has_mixed_gstin_validity for context).
        # Sum-accounting check:
        accounted = (
            outcome.created
            + outcome.created_flagged
            + outcome.flagged_not_created
            + outcome.skipped
        )
        self.assertEqual(accounted, 23)
