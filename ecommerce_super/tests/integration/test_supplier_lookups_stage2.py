"""Stage 2 tests for §8f — eager multi-country state/country caching.

The §8e Stage 2 substrate (EasyEcom Country / EasyEcom State + the
discover-and-cache flow + the resolver helpers) was already
implemented as a single-country-iteration loop — Phase 2 walks every
country cached by Phase 1, so the code is already eager-all-countries.
What §8e Stage 2 *verified* was India only (a single fixture).

This module is the §8f extension: drive the flow with a captured
3-country fixture set (India + Italy + Armenia) and prove the
eager-all-countries behaviour and the foreign-state resolver work
end-to-end on real EE shapes.

ALL MOCKED — no real EE traffic. The Italy + Armenia fixtures were
captured live from Harmony per the packet's "Harmony OK to capture
fixtures" carve-out (foundational reads only). The India fixture is
reused from the §8e captures.

§8f-specific coverage (the things §8e didn't exercise):
  - Eager sweep: 247 countries cached + states pulled for India +
    Italy + Armenia in one pull, with all three countries' states in
    the cache afterward.
  - Per-country failure isolation: when getStates raises on ONE
    country (e.g. Italy), the sweep records the failure in
    `outcome.states_failed` and CONTINUES with the remaining
    countries (Armenia still gets cached).
  - Foreign-state resolution: resolve_state("Abruzzo", country_id=114)
    → 1556; resolve_state for the two Armenian states (384/386 from
    the real vendor sample) round-trips.
  - Idempotent across multiple countries: re-run upserts everything
    in-place; no duplication, no orphan, no row deletion.

Resolver behaviour for foreign states with null pincode ranges is
covered too — Italy / Armenia rows arrive with zip_start_range=null
which `_upsert_state` stores as 0; `validate_pincode_state` returns
`unknown_state` for those (can't validate), which is the correct
soft-flag for foreign suppliers.
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
    STATES_GET,
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
    "..",
    "process",
    "ee_mock_fixtures",
)


def _load_fixture(name: str) -> dict:
    path = os.path.join(FIXTURE_DIR, name)
    with open(path) as f:
        return json.load(f)


def _build_multi_country_mock(
    *,
    countries: dict,
    states_by_country: dict[int, dict],
    raise_on_country_id: int | None = None,
) -> MagicMock:
    """Build a mock EE client that returns the captured fixture for
    each country requested. `raise_on_country_id` lets a test inject a
    deliberate failure on one country to exercise the
    fail-and-continue path."""
    client = MagicMock()

    def _get(endpoint, params=None, **_kw):
        if endpoint == COUNTRIES_GET:
            return countries
        if endpoint == STATES_GET:
            country_id = int((params or {}).get("countryId") or 0)
            if (
                raise_on_country_id is not None
                and country_id == raise_on_country_id
            ):
                raise RuntimeError(
                    f"simulated upstream failure for country_id={country_id}"
                )
            return states_by_country.get(
                country_id, {"code": 200, "states": []}
            )
        raise AssertionError(f"unexpected endpoint in mock: {endpoint!r}")

    client.get.side_effect = _get
    return client


def _wipe_lookups_cache() -> None:
    """Wipe both tables — Phase 1's intermediate commit makes country
    rows survive FrappeTestCase rollback otherwise. Mirrors the §8e
    Stage 2 test pattern."""
    frappe.db.delete("EasyEcom State")
    frappe.db.delete("EasyEcom Country")
    frappe.db.commit()


class TestEagerMultiCountrySweep(FrappeTestCase):
    """End-to-end: pull countries + walk-every-country getStates and
    verify all three target countries' states land in the cache."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.india_fixture = _load_fixture("getstates_india_response.json")
        cls.italy_fixture = _load_fixture("getstates_italy_response.json")
        cls.armenia_fixture = _load_fixture("getstates_armenia_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_eager_sweep_caches_all_three_countries(self) -> None:
        client = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        outcome = pull_countries_and_states(client=client)

        # All 247 countries from the captured fixture are cached.
        self.assertEqual(outcome.countries_total, 247)
        self.assertEqual(outcome.countries_new, 247)
        self.assertEqual(outcome.countries_failed, [])

        # States for the three populated countries land in the cache.
        india_count = frappe.db.count(
            "EasyEcom State", {"country_id": 1}
        )
        italy_count = frappe.db.count(
            "EasyEcom State", {"country_id": 114}
        )
        armenia_count = frappe.db.count(
            "EasyEcom State", {"country_id": 15}
        )
        self.assertEqual(
            india_count, len(self.india_fixture["states"])
        )
        self.assertEqual(
            italy_count, len(self.italy_fixture["states"])
        )
        self.assertEqual(
            armenia_count, len(self.armenia_fixture["states"])
        )

        # Outcome totals consolidate.
        expected_total = (
            len(self.india_fixture["states"])
            + len(self.italy_fixture["states"])
            + len(self.armenia_fixture["states"])
        )
        self.assertEqual(outcome.states_total, expected_total)
        self.assertEqual(outcome.states_new, expected_total)
        self.assertEqual(outcome.states_failed, [])

    def test_eager_sweep_visits_every_country_endpoint(self) -> None:
        """The packet's "~247 calls" — verify Phase 2 hits getStates
        once per country, exactly. Confirms the loop walks ALL cached
        countries, not just a subset."""
        client = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        pull_countries_and_states(client=client)

        # The mock's get() is called once for /getCountries + once per
        # country for /getStates. So total = 1 + 247 = 248.
        getstates_calls = [
            c
            for c in client.get.call_args_list
            if c.args and c.args[0] == STATES_GET
        ]
        self.assertEqual(len(getstates_calls), 247)

        called_country_ids = sorted(
            int(c.kwargs.get("params", {}).get("countryId"))
            for c in getstates_calls
        )
        self.assertEqual(called_country_ids[0], 1)  # India is id 1
        self.assertEqual(len(set(called_country_ids)), 247)


class TestPerCountryFailureIsolation(FrappeTestCase):
    """When ONE country's getStates raises, the sweep records the
    failure and CONTINUES — every other country's states are still
    pulled. Packet directive: "if one country's getStates fails, log
    + continue, don't abort the whole sweep. Report which failed."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.india_fixture = _load_fixture("getstates_india_response.json")
        cls.italy_fixture = _load_fixture("getstates_italy_response.json")
        cls.armenia_fixture = _load_fixture("getstates_armenia_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_one_country_failure_does_not_abort_sweep(self) -> None:
        """Italy raises; India + Armenia must still be cached."""
        client = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
            raise_on_country_id=114,  # Italy fails
        )
        outcome = pull_countries_and_states(client=client)

        # Countries phase succeeded entirely.
        self.assertEqual(outcome.countries_total, 247)
        self.assertEqual(outcome.countries_new, 247)

        # India + Armenia states landed.
        self.assertEqual(
            frappe.db.count("EasyEcom State", {"country_id": 1}),
            len(self.india_fixture["states"]),
        )
        self.assertEqual(
            frappe.db.count("EasyEcom State", {"country_id": 15}),
            len(self.armenia_fixture["states"]),
        )
        # Italy did NOT (the call raised before any state was upserted).
        self.assertEqual(
            frappe.db.count("EasyEcom State", {"country_id": 114}), 0
        )

        # The failure is reported in outcome.states_failed — with
        # enough context for the FDE to know WHICH country broke and
        # WHY. Packet directive: "Report which failed."
        italy_failures = [
            f
            for f in outcome.states_failed
            if f.get("country_id") == 114
        ]
        self.assertEqual(len(italy_failures), 1)
        self.assertIn("Italy", italy_failures[0]["country"])
        self.assertIn("RuntimeError", italy_failures[0]["error"])
        self.assertIn("simulated upstream", italy_failures[0]["error"])


class TestForeignStateResolution(FrappeTestCase):
    """resolve_state across foreign countries — same (name, country_id)
    contract as India, no fuzzy, case-insensitive. The packet's
    vendor sample referenced Italy 1556/Abruzzo and Armenia 384/386,
    so these are the canonical regression cases."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.india_fixture = _load_fixture("getstates_india_response.json")
        cls.italy_fixture = _load_fixture("getstates_italy_response.json")
        cls.armenia_fixture = _load_fixture("getstates_armenia_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()
        client = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        pull_countries_and_states(client=client)

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_country_resolver_returns_italy(self) -> None:
        r = resolve_country("Italy")
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "Italy")
        self.assertEqual(r.country_id, 114)

    def test_country_resolver_returns_armenia(self) -> None:
        r = resolve_country("Armenia")
        self.assertIsNotNone(r)
        self.assertEqual(r.country_id, 15)

    def test_italian_abruzzo_resolves_to_1556(self) -> None:
        """Packet's vendor sample state. Italy=114, Abruzzo=1556 — the
        canonical Italian-region row (not one of the many
        '<city> - Abruzzo' fine-grained rows EE also returns)."""
        self.assertEqual(resolve_state("Abruzzo", country_id=114), 1556)

    def test_italian_abruzzo_case_insensitive(self) -> None:
        for variant in ("abruzzo", "ABRUZZO", "  Abruzzo  "):
            self.assertEqual(
                resolve_state(variant, country_id=114),
                1556,
                f"failed for variant {variant!r}",
            )

    def test_armenia_aragatsotni_marz_resolves_to_384(self) -> None:
        self.assertEqual(
            resolve_state("Aragatsotni Marz", country_id=15), 384
        )

    def test_armenia_armaviri_marz_resolves_to_386(self) -> None:
        self.assertEqual(
            resolve_state("Armaviri Marz", country_id=15), 386
        )

    def test_foreign_state_wrong_country_id_returns_none(self) -> None:
        """Abruzzo is Italian — must NOT resolve under India's
        country_id. The (name, country_id) scoping protects against
        a Stage 4 push handing a foreign state name to the wrong
        country."""
        self.assertIsNone(resolve_state("Abruzzo", country_id=1))
        self.assertIsNone(resolve_state("Aragatsotni Marz", country_id=1))
        self.assertIsNone(resolve_state("Karnataka", country_id=114))

    def test_foreign_state_unknown_name_returns_none(self) -> None:
        """No fuzzy — 'Abruzo' (typo) returns None, caller flags."""
        self.assertIsNone(resolve_state("Abruzo", country_id=114))
        self.assertIsNone(resolve_state("Atlantis", country_id=114))

    def test_foreign_state_pincode_validation_is_unknown(self) -> None:
        """Italy/Armenia state rows have zip_start_range=null in the
        captured fixture — `_upsert_state` stores 0; the resolver's
        validate_pincode_state returns `unknown_state` for that
        (can't validate without a range). This is the correct
        soft-flag behaviour for foreign suppliers: pull doesn't
        block, FDE reviews."""
        # Abruzzo
        r = validate_pincode_state("65100", state_id=1556)
        self.assertEqual(r.status, "unknown_state")
        # Aragatsotni Marz
        r2 = validate_pincode_state("0040", state_id=384)
        self.assertEqual(r2.status, "unknown_state")


class TestIdempotentEagerReRun(FrappeTestCase):
    """Re-run the eager sweep across multiple countries — every row is
    upserted in-place; counts shift from new→updated; no row
    deletion. Mirrors the §8e idempotency test, scaled to 3 countries."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.countries_fixture = _load_fixture("getcountries_response.json")
        cls.india_fixture = _load_fixture("getstates_india_response.json")
        cls.italy_fixture = _load_fixture("getstates_italy_response.json")
        cls.armenia_fixture = _load_fixture("getstates_armenia_response.json")

    def setUp(self) -> None:
        _wipe_lookups_cache()

    def tearDown(self) -> None:
        _wipe_lookups_cache()

    def test_multi_country_re_run_is_idempotent(self) -> None:
        client = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        first = pull_countries_and_states(client=client)
        country_rows_after_first = frappe.db.count("EasyEcom Country")
        state_rows_after_first = frappe.db.count("EasyEcom State")

        # Re-run with a fresh mock instance (calls are tracked
        # per-instance; the side_effect is the same fixtures).
        client2 = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        second = pull_countries_and_states(client=client2)

        # Counts shift from new→updated; nothing inserted, nothing deleted.
        self.assertEqual(second.countries_new, 0)
        self.assertEqual(second.countries_updated, first.countries_total)
        self.assertEqual(second.states_new, 0)
        self.assertEqual(second.states_updated, first.states_total)
        self.assertEqual(
            frappe.db.count("EasyEcom Country"), country_rows_after_first
        )
        self.assertEqual(
            frappe.db.count("EasyEcom State"), state_rows_after_first
        )

        # Foreign-state resolution still works after the re-run.
        self.assertEqual(resolve_state("Abruzzo", country_id=114), 1556)
        self.assertEqual(
            resolve_state("Aragatsotni Marz", country_id=15), 384
        )

    def test_failure_recovery_on_second_run(self) -> None:
        """First run: Italy fails. Second run (Italy healthy): Italy
        states arrive; no duplication of India/Armenia rows. This is
        the FDE-restart scenario — flaky upstream, hit Refresh again."""
        client_with_fail = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
            raise_on_country_id=114,
        )
        first = pull_countries_and_states(client=client_with_fail)
        self.assertTrue(
            any(
                f.get("country_id") == 114
                for f in first.states_failed
            )
        )
        self.assertEqual(
            frappe.db.count("EasyEcom State", {"country_id": 114}), 0
        )
        india_count_before = frappe.db.count(
            "EasyEcom State", {"country_id": 1}
        )

        # Restart with healthy Italy.
        client_healthy = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        second = pull_countries_and_states(client=client_healthy)

        # Italy is now populated.
        self.assertEqual(
            frappe.db.count("EasyEcom State", {"country_id": 114}),
            len(self.italy_fixture["states"]),
        )
        # India unchanged (same count, all updated rather than inserted).
        self.assertEqual(
            frappe.db.count("EasyEcom State", {"country_id": 1}),
            india_count_before,
        )
        # Italy rows are counted as `new` on the second run (didn't
        # exist before); India + Armenia rows are `updated`.
        self.assertEqual(
            second.states_new, len(self.italy_fixture["states"])
        )
        self.assertEqual(second.states_failed, [])

    def test_failure_recovery_resolver_works_after_recovery(self) -> None:
        """Belt-and-braces: after the recovery run, Abruzzo resolves."""
        client_fail = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
            raise_on_country_id=114,
        )
        pull_countries_and_states(client=client_fail)
        # Pre-recovery: resolver can't find Abruzzo.
        self.assertIsNone(resolve_state("Abruzzo", country_id=114))

        client_ok = _build_multi_country_mock(
            countries=self.countries_fixture,
            states_by_country={
                1: self.india_fixture,
                114: self.italy_fixture,
                15: self.armenia_fixture,
            },
        )
        pull_countries_and_states(client=client_ok)
        # Post-recovery: Abruzzo is back.
        self.assertEqual(resolve_state("Abruzzo", country_id=114), 1556)
