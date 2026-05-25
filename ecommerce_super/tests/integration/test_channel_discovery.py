"""Integration tests for §8.6.3 Channel discovery sweep.

Drives sweep_all_locations against a mocked client whose response is
a per-location dict (different locations may return different channel
lists). Validates the per-location sweep, savepoint isolation, dedupe
by marketplace_id, and is_active = active-on-any-location.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows import channel_discovery
from ecommerce_super.easyecom.flows.channel_discovery import (
    discover_channels,
    sweep_all_locations,
)
from ecommerce_super.tests.factories import make_location


def _wipe_marketplaces(prefix: str | None = None) -> None:
    filters = {"marketplace_id": ("like", f"{prefix}%")} if prefix else {}
    for n in frappe.db.get_all("Marketplace", filters=filters, pluck="name"):
        try:
            frappe.delete_doc("Marketplace", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()


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


class _StubClient:
    """Per-location stub for EasyEcomClient. Yields a response keyed
    on the location_key passed at construction."""

    PER_LOCATION_RESPONSES: dict = {}
    RAISE_FOR_LOCATIONS: set = set()

    def __init__(self, location_key: str | None = None, company: str | None = None) -> None:
        self.location_key = location_key

    def get(self, endpoint: str):
        if self.location_key in self.RAISE_FOR_LOCATIONS:
            raise RuntimeError(f"simulated JWT failure for {self.location_key}")
        return self.PER_LOCATION_RESPONSES.get(self.location_key, {"data": []})


class TestPerLocationSweep(FrappeTestCase):
    """The sweep MUST iterate every discovered location regardless of
    workflow_state, and MUST dedupe by marketplace_id."""

    PREFIX_LOC = "chan-sweep-loc-"
    PREFIX_MID = "chan-sweep-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Three locations in different workflow states — the sweep covers
        # all four (To Map, Mapped but not Live, Live, Skipped) per the
        # §8.6.3 contract.
        cls.loc_to_map = make_location(
            location_key=f"{cls.PREFIX_LOC}to-map", workflow_state="To Map"
        )
        cls.loc_skipped = make_location(
            location_key=f"{cls.PREFIX_LOC}skipped", workflow_state="Skipped"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_locations(cls.PREFIX_LOC)
        super().tearDownClass()

    def setUp(self) -> None:
        _wipe_marketplaces("chan-sweep-")
        # Also clean any real-id channels the test creates.
        for mid in (2, 60, 122):
            _wipe_marketplaces(str(mid))
        self._original_client = channel_discovery.EasyEcomClient
        channel_discovery.EasyEcomClient = _StubClient
        _StubClient.PER_LOCATION_RESPONSES = {}
        _StubClient.RAISE_FOR_LOCATIONS = set()

    def tearDown(self) -> None:
        channel_discovery.EasyEcomClient = self._original_client
        _wipe_marketplaces("chan-sweep-")
        for mid in (2, 60, 122):
            _wipe_marketplaces(str(mid))

    def test_sweep_polls_every_location_regardless_of_workflow_state(self) -> None:
        """To Map AND Skipped locations both polled — channel catalogue
        must be complete (§8.6.3). Asserts on the OUR test locations
        getting polled, not on total count (other tests in the suite
        may have leaked Location rows)."""
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX_LOC}to-map": {
                "data": [{"marketplace_id": 2, "marketplace_name": "Flipkart", "status": "Active"}]
            },
            f"{self.PREFIX_LOC}skipped": {
                "data": [{"marketplace_id": 122, "marketplace_name": "meesho", "status": "Active"}]
            },
        }
        result = sweep_all_locations()
        # Both of OUR test locations are in succeeded_location_keys.
        succeeded = set(result["succeeded_location_keys"])
        self.assertIn(f"{self.PREFIX_LOC}to-map", succeeded)
        self.assertIn(f"{self.PREFIX_LOC}skipped", succeeded)
        # Both channels landed.
        self.assertTrue(frappe.db.exists("Marketplace", {"marketplace_id": "2"}))
        self.assertTrue(frappe.db.exists("Marketplace", {"marketplace_id": "122"}))

    def test_dedupe_by_marketplace_id(self) -> None:
        """Same channel on two locations → ONE Marketplace row."""
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX_LOC}to-map": {
                "data": [{"marketplace_id": 2, "marketplace_name": "Flipkart", "status": "Active"}]
            },
            f"{self.PREFIX_LOC}skipped": {
                "data": [{"marketplace_id": 2, "marketplace_name": "Flipkart", "status": "Active"}]
            },
        }
        sweep_all_locations()
        # Exactly one Marketplace row exists for id 2 (deduped across the
        # two-location response).
        rows = frappe.db.get_all("Marketplace", filters={"marketplace_id": "2"})
        self.assertEqual(len(rows), 1)

    def test_is_active_promoted_when_active_on_any_location(self) -> None:
        """Channel Inactive on loc A, Active on loc B → is_active=1 in
        the catalogue (active-on-any-location)."""
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX_LOC}to-map": {
                "data": [{"marketplace_id": 60, "marketplace_name": "TaTa Cliq", "status": "Inactive"}]
            },
            f"{self.PREFIX_LOC}skipped": {
                "data": [{"marketplace_id": 60, "marketplace_name": "TaTa Cliq", "status": "Active"}]
            },
        }
        sweep_all_locations()
        is_active = frappe.db.get_value(
            "Marketplace", {"marketplace_id": "60"}, "is_active"
        )
        self.assertEqual(int(is_active), 1)

    def test_per_location_savepoint_isolation(self) -> None:
        """One location's failure (e.g. JWT problem) records that
        location Failed and the sweep continues — §7.1 + 8a savepoint
        helper."""
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX_LOC}to-map": {
                "data": [{"marketplace_id": 2, "marketplace_name": "Flipkart", "status": "Active"}]
            },
            # Skipped location will RAISE.
        }
        _StubClient.RAISE_FOR_LOCATIONS = {f"{self.PREFIX_LOC}skipped"}
        result = sweep_all_locations()
        # The to-map location succeeded; the skipped location failed.
        # Assert on our specific locations rather than total count
        # (other suite tests may have leaked Locations).
        succeeded = set(result["succeeded_location_keys"])
        self.assertIn(f"{self.PREFIX_LOC}to-map", succeeded)
        self.assertNotIn(f"{self.PREFIX_LOC}skipped", succeeded)
        failed_keys = [loc["location_key"] for loc, _exc in result["failed_locations"]]
        self.assertIn(f"{self.PREFIX_LOC}skipped", failed_keys)
        for loc, exc in result["failed_locations"]:
            if loc["location_key"] == f"{self.PREFIX_LOC}skipped":
                self.assertIsInstance(exc, RuntimeError)
        # The successful location's channel still landed.
        self.assertTrue(frappe.db.exists("Marketplace", {"marketplace_id": "2"}))

    def test_new_channels_land_in_unclassified(self) -> None:
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX_LOC}to-map": {
                "data": [{"marketplace_id": 122, "marketplace_name": "meesho", "status": "Active"}]
            },
        }
        sweep_all_locations()
        ws = frappe.db.get_value("Marketplace", {"marketplace_id": "122"}, "workflow_state")
        self.assertEqual(ws, "Unclassified")

    def test_repull_skips_existing_does_not_reclassify(self) -> None:
        """Re-pull on an existing channel that the FDE has classified
        must NOT reset workflow_state or channel_type."""
        # First sweep: discover the channel.
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX_LOC}to-map": {
                "data": [{"marketplace_id": 122, "marketplace_name": "meesho", "status": "Active"}]
            },
        }
        sweep_all_locations()
        # FDE classifies it (via db.set_value to bypass workflow gate
        # in the test — production goes through Actions → Classify).
        frappe.db.set_value(
            "Marketplace",
            {"marketplace_id": "122"},
            {"channel_type": "B2C Marketplace", "workflow_state": "Classified"},
        )
        frappe.db.commit()
        # Second sweep — same channel.
        result2 = sweep_all_locations()
        # Counted as existing, not new.
        self.assertEqual(len(result2["new_channels"]), 0)
        # FDE-set fields untouched.
        ws = frappe.db.get_value("Marketplace", {"marketplace_id": "122"}, "workflow_state")
        ct = frappe.db.get_value("Marketplace", {"marketplace_id": "122"}, "channel_type")
        self.assertEqual(ws, "Classified")
        self.assertEqual(ct, "B2C Marketplace")


class TestWhitelistWrapper(FrappeTestCase):
    """discover_channels is the FDE-facing wrapper."""

    PREFIX = "chan-wl-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.loc = make_location(
            location_key=f"{cls.PREFIX}loc", workflow_state="To Map"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_locations(cls.PREFIX)
        super().tearDownClass()

    def setUp(self) -> None:
        self._original_user = frappe.session.user
        self._original_client = channel_discovery.EasyEcomClient
        channel_discovery.EasyEcomClient = _StubClient
        _StubClient.PER_LOCATION_RESPONSES = {}
        _StubClient.RAISE_FOR_LOCATIONS = set()
        _wipe_marketplaces("chan-wl-")
        for mid in (2,):
            _wipe_marketplaces(str(mid))

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        channel_discovery.EasyEcomClient = self._original_client
        _wipe_marketplaces("chan-wl-")
        for mid in (2,):
            _wipe_marketplaces(str(mid))
        for email in ("nofde-channels@test.local",):
            if frappe.db.exists("User", email):
                frappe.delete_doc("User", email, force=True, ignore_permissions=True)

    def test_operator_role_rejected(self) -> None:
        email = "nofde-channels@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        user = frappe.new_doc("User")
        user.update({"email": email, "first_name": "NoFDE", "send_welcome_email": 0, "enabled": 1})
        user.insert(ignore_permissions=True)
        user.append("roles", {"role": "EasyEcom Operator"})
        user.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.set_user(email)
        with self.assertRaises(frappe.PermissionError):
            discover_channels()

    def test_summary_shape_on_success(self) -> None:
        _StubClient.PER_LOCATION_RESPONSES = {
            f"{self.PREFIX}loc": {
                "data": [{"marketplace_id": 2, "marketplace_name": "Flipkart", "status": "Active"}]
            },
        }
        result = discover_channels()
        self.assertTrue(result["ok"])
        # The summary dict has the right SHAPE; counts may be > our
        # specific test's contribution because other tests may have
        # left Location rows in the DB.
        for key in (
            "locations_polled",
            "locations_failed",
            "channels_total",
            "channels_new",
            "channels_existing",
            "new_channels",
            "failed_locations",
        ):
            self.assertIn(key, result)
        # Our specific channel is in new_channels (or already existed).
        self.assertTrue(
            frappe.db.exists("Marketplace", {"marketplace_id": "2"}),
            "Discover Channels did not create the Marketplace row our stub returned",
        )

    def test_wrapper_catches_exceptions(self) -> None:
        orig = channel_discovery.sweep_all_locations

        def _boom():
            raise RuntimeError("simulated sweep failure")

        channel_discovery.sweep_all_locations = _boom
        try:
            result = discover_channels()
        finally:
            channel_discovery.sweep_all_locations = orig
        self.assertFalse(result["ok"])
        self.assertIn("simulated sweep failure", result["message"])
