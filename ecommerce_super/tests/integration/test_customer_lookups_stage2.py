"""Stage 2 tests for §8e — country/state discover-and-cache + resolver helpers.

ALL MOCKED — no real EE traffic. The Harmony getCountries/getStates
fixtures were captured live during Stage 2 build (Harmony reads are
sanctioned per the packet); tests run against the JSON files in
process/ee_mock_fixtures/ so they're hermetic.

Coverage mirrors §8a/§8b discover tests + adds the resolver-specific
cases the packet calls out:
  - Karnataka pincode 560035 → ok
  - Arunachal Pradesh + 560035 → mismatch (the dirty-data case)
  - Daman & Diu dupe (id 35 vs 3848) → resolver picks the larger id
  - Unresolvable name → None (no fuzzy fallback)
"""

from __future__ import annotations

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
    is_foundational,
)
from ecommerce_super.easyecom.customer.state_resolver import (
    resolve_country,
    resolve_state,
    validate_pincode_state,
)
from ecommerce_super.easyecom.flows.customer_lookups import (
    pull_countries_and_states,
)


FIXTURE_DIR = os.path.join(
    frappe.get_app_path("ecommerce_super"),
    "..",  # apps/ecommerce_super/ecommerce_super -> apps/ecommerce_super
    "process",
    "ee_mock_fixtures",
)


def _load_fixture(name: str) -> dict:
    """Load one of the captured Harmony response fixtures."""
    path = os.path.join(FIXTURE_DIR, name)
    with open(path) as f:
        return json.load(f)


def _build_mock_client(*, countries: dict, states_by_country: dict) -> MagicMock:
    """Build a mock EasyEcomClient whose .get() returns the fixture
    response for the matching endpoint. states_by_country is keyed by
    int countryId so the test controls which states each country
    returns (we use 1=India everywhere)."""
    client = MagicMock()

    def _get(endpoint, params=None, **_kw):
        if endpoint == COUNTRIES_GET:
            return countries
        if endpoint == STATES_GET:
            country_id = int((params or {}).get("countryId") or 0)
            return states_by_country.get(country_id, {"code": 200, "states": []})
        raise AssertionError(f"unexpected endpoint in mock: {endpoint!r}")

    client.get.side_effect = _get
    return client


def _wipe_lookups_cache() -> None:
    """Remove any rows the discover would touch — keeps test isolation
    even though FrappeTestCase rolls back per test. The pull's
    db.commit() inside Phase 1 makes the country rows survive rollback
    if we don't wipe explicitly."""
    frappe.db.delete("EasyEcom State")
    frappe.db.delete("EasyEcom Country")
    frappe.db.commit()


class TestFoundationalEndpointRegistration(FrappeTestCase):
    """The new endpoints must be classified foundational so the client
    layer leaves company blank and the API Call rows tag is_foundational=1."""

    def test_getCountries_is_foundational(self) -> None:
        self.assertIn(COUNTRIES_GET, FOUNDATIONAL_ENDPOINTS)
        self.assertTrue(is_foundational(COUNTRIES_GET))

    def test_getStates_is_foundational(self) -> None:
        self.assertIn(STATES_GET, FOUNDATIONAL_ENDPOINTS)
        self.assertTrue(is_foundational(STATES_GET))

    def test_getStates_with_query_string_classified_foundational(self) -> None:
        """The live caller appends ?countryId=N. is_foundational must
        strip the query before set-membership (mirrors the cursor-strip
        fix on PRODUCT_MASTER_GET)."""
        self.assertTrue(is_foundational(f"{STATES_GET}?countryId=1"))


class TestPullCountriesAndStates(FrappeTestCase):
    """Drive the discover-and-cache flow with the captured fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.states_fixture = _load_fixture("getstates_india_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_first_run_populates_countries_and_states(self) -> None:
        client = _build_mock_client(
            countries=self.countries_fixture,
            states_by_country={1: self.states_fixture},
        )
        outcome = pull_countries_and_states(client=client)

        self.assertGreater(outcome.countries_total, 0)
        self.assertEqual(outcome.countries_new, outcome.countries_total)
        self.assertEqual(outcome.countries_updated, 0)
        self.assertEqual(outcome.countries_skipped, 0)

        # India should be cached as id 1.
        india = frappe.db.get_value(
            "EasyEcom Country",
            {"country_id": 1},
            ["country_name", "code_2", "code_3", "default_currency_code"],
            as_dict=True,
        )
        self.assertIsNotNone(india)
        self.assertEqual(india.country_name, "India")
        self.assertEqual(india.code_2, "IN")
        self.assertEqual(india.code_3, "IND")
        self.assertEqual(india.default_currency_code, "INR")

        # All 39 Indian states from the live fixture.
        self.assertEqual(outcome.states_total, len(self.states_fixture["states"]))
        self.assertEqual(outcome.states_new, outcome.states_total)

    def test_re_run_is_idempotent(self) -> None:
        """Two consecutive runs: first inserts, second updates in-place.
        Counts shift from new=total to updated=total. No row is deleted."""
        client = _build_mock_client(
            countries=self.countries_fixture,
            states_by_country={1: self.states_fixture},
        )
        first = pull_countries_and_states(client=client)
        before_count = frappe.db.count("EasyEcom State")

        # Re-run.
        client2 = _build_mock_client(
            countries=self.countries_fixture,
            states_by_country={1: self.states_fixture},
        )
        second = pull_countries_and_states(client=client2)

        self.assertEqual(second.countries_new, 0)
        self.assertEqual(second.countries_updated, first.countries_total)
        self.assertEqual(second.states_new, 0)
        self.assertEqual(second.states_updated, first.states_total)
        self.assertEqual(frappe.db.count("EasyEcom State"), before_count)

    def test_skipped_row_when_id_missing(self) -> None:
        """A row missing `id` is skipped (not failed) — Stage 1 substrate
        scenario where EE's shape might add an empty placeholder."""
        countries_with_junk = {
            "code": 200,
            "countries": [
                {"id": 1, "country": "India", "code_2": "IN", "code_3": "IND",
                 "default_currency_code": "INR"},
                {"country": "Missing ID"},  # no id → skipped
            ],
        }
        client = _build_mock_client(
            countries=countries_with_junk,
            states_by_country={1: {"code": 200, "states": []}},
        )
        outcome = pull_countries_and_states(client=client)
        self.assertEqual(outcome.countries_total, 2)
        self.assertEqual(outcome.countries_new, 1)
        self.assertEqual(outcome.countries_skipped, 1)


class TestCountryResolver(FrappeTestCase):
    """Country name → resolution. Case-insensitive, strip, no fuzzy."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.states_fixture = _load_fixture("getstates_india_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()
        client = _build_mock_client(
            countries=self.countries_fixture,
            states_by_country={1: self.states_fixture},
        )
        pull_countries_and_states(client=client)

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_resolve_india_exact(self) -> None:
        r = resolve_country("India")
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "India")
        self.assertEqual(r.country_id, 1)

    def test_resolve_india_case_insensitive(self) -> None:
        for variant in ("india", "INDIA", "  India  ", "iNdIa"):
            r = resolve_country(variant)
            self.assertIsNotNone(r, f"failed for variant {variant!r}")
            self.assertEqual(r.country_id, 1)

    def test_resolve_unknown_returns_none(self) -> None:
        """No fuzzy matching — 'Indi' (typo) returns None, caller flags."""
        self.assertIsNone(resolve_country("Indi"))
        self.assertIsNone(resolve_country("Wakanda"))

    def test_resolve_empty_or_none_returns_none(self) -> None:
        self.assertIsNone(resolve_country(None))
        self.assertIsNone(resolve_country(""))
        self.assertIsNone(resolve_country("   "))


class TestStateResolver(FrappeTestCase):
    """State name → state_id. The Daman & Diu dupe + the no-fuzzy contract."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.states_fixture = _load_fixture("getstates_india_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()
        client = _build_mock_client(
            countries=self.countries_fixture,
            states_by_country={1: self.states_fixture},
        )
        pull_countries_and_states(client=client)

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_resolve_karnataka(self) -> None:
        self.assertEqual(resolve_state("Karnataka", country_id=1), 12)

    def test_resolve_case_insensitive_and_stripped(self) -> None:
        self.assertEqual(resolve_state("  karnataka  ", country_id=1), 12)
        self.assertEqual(resolve_state("KARNATAKA", country_id=1), 12)

    def test_unresolvable_returns_none(self) -> None:
        """'Karnatka' (typo) is NOT resolved to Karnataka — no fuzzy."""
        self.assertIsNone(resolve_state("Karnatka", country_id=1))
        self.assertIsNone(resolve_state("Atlantis", country_id=1))

    def test_wrong_country_returns_none(self) -> None:
        """A real Indian state shouldn't resolve under a different country id."""
        self.assertIsNone(resolve_state("Karnataka", country_id=999))

    def test_daman_and_diu_dupe_picks_larger_id(self) -> None:
        """The legacy 'Daman & Diu' (id=35) and the merged 'Dadra & Nagar
        Haveli and Daman & Diu' (id=3848) have DIFFERENT names but both
        contain 'Daman & Diu'. Exact-name match — 'Daman & Diu' resolves
        to 35 (the LARGEST id matching THAT name); the merged unit
        resolves to 3848 under its full name. Both are deterministic
        (no name collision means no ambiguity)."""
        # The dupe-resolver rule applies when EE genuinely returns the
        # SAME name twice. In this fixture each row has a distinct name,
        # so both id 35 and 3848 are independently resolvable.
        self.assertEqual(resolve_state("Daman & Diu", country_id=1), 35)
        self.assertEqual(
            resolve_state(
                "Dadra & Nagar Haveli and Daman & Diu", country_id=1
            ),
            3848,
        )

    def test_dupe_rule_picks_largest_when_same_name(self) -> None:
        """Synthetic test of the LARGEST-id rule: inject a duplicate name
        with a smaller id and verify the resolver returns the larger."""
        # Insert a synthetic legacy 'Karnataka' at id 999999 (larger than
        # the real id=12) — the resolver should return 999999.
        synthetic = frappe.new_doc("EasyEcom State")
        synthetic.update(
            {
                "state_id": 999999,
                "state_name": "Karnataka",
                "country": frappe.db.get_value(
                    "EasyEcom Country", {"country_id": 1}, "name"
                ),
                "country_id": 1,
                "is_union_territory": 0,
                "zip_start_range": 56,
                "zip_end_range": 59,
                "postal_code": "KA",
                "zone": "South",
            }
        )
        synthetic.insert(ignore_permissions=True)
        self.assertEqual(resolve_state("Karnataka", country_id=1), 999999)


class TestPincodeStateValidation(FrappeTestCase):
    """The 4 PincodeMatch outcomes + the packet's Arunachal Pradesh +
    Bangalore-pincode dirty-data case."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.states_fixture = _load_fixture("getstates_india_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()
        client = _build_mock_client(
            countries=self.countries_fixture,
            states_by_country={1: self.states_fixture},
        )
        pull_countries_and_states(client=client)

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_bangalore_pincode_matches_karnataka(self) -> None:
        """560035 starts with '56' which is in Karnataka's 56-59 range."""
        r = validate_pincode_state("560035", state_id=12)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.state_name, "Karnataka")
        self.assertEqual(r.expected_prefix_range, (56, 59))
        self.assertEqual(r.pincode_prefix, 56)

    def test_bangalore_pincode_claimed_as_arunachal_pradesh_is_mismatch(self) -> None:
        """The packet's dirty-data case: state='Arunachal Pradesh' but
        pincode=560035. Arunachal's range is 790-792; 560035 starts with
        '560' which is NOT in that range — soft mismatch (Stage 3
        Created-Flagged)."""
        r = validate_pincode_state("560035", state_id=2)
        self.assertEqual(r.status, "mismatch")
        self.assertEqual(r.state_name, "Arunachal Pradesh")
        self.assertEqual(r.expected_prefix_range, (790, 792))
        self.assertEqual(r.pincode_prefix, 560)

    def test_unknown_state_id(self) -> None:
        r = validate_pincode_state("560035", state_id=99999)
        self.assertEqual(r.status, "unknown_state")

    def test_no_pincode(self) -> None:
        for empty in (None, "", "   ", "abc"):
            r = validate_pincode_state(empty, state_id=12)
            self.assertEqual(r.status, "no_pincode", f"failed for {empty!r}")

    def test_int_pincode_accepted(self) -> None:
        """Some callers pass int(560035) rather than str. Both should work."""
        r = validate_pincode_state(560035, state_id=12)
        self.assertEqual(r.status, "ok")

    def test_single_prefix_state_with_null_end_range(self) -> None:
        """Andaman & Nicobar has zip_start_range=744 with zip_end_range=null.
        744101 should be 'ok'; 745000 should be 'mismatch'."""
        andaman_id = frappe.db.get_value(
            "EasyEcom State",
            {"state_name": "Andaman & Nicobar Islands"},
            "state_id",
        )
        self.assertIsNotNone(andaman_id)
        ok = validate_pincode_state("744101", state_id=andaman_id)
        self.assertEqual(ok.status, "ok")
        miss = validate_pincode_state("745000", state_id=andaman_id)
        self.assertEqual(miss.status, "mismatch")

    def test_pincode_short_treated_as_no_pincode(self) -> None:
        """A pincode shorter than the state's prefix-digit-count can't
        be validated — return no_pincode rather than fabricate a match."""
        r = validate_pincode_state("56", state_id=2)  # Arunachal needs 3 digits
        self.assertEqual(r.status, "no_pincode")
