"""Integration tests for the §8a location discovery TRIGGER SURFACE:

  - the @frappe.whitelist() wrapper `discover_locations` (Account form button)
  - the scheduler entry `scheduled_discover_locations`
  - the Notification Log fan-out to EasyEcom FDE users on new-location runs

The underlying upsert is exercised by test_location_discovery.py — these
tests focus on the surface that triggers it.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows import location_discovery
from ecommerce_super.easyecom.flows.location_discovery import (
    _notify_if_new_locations,
    _split_new_vs_updated,
    _users_with_role,
    discover_locations,
    scheduled_discover_locations,
    upsert_locations_from_payload,
)


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


def _wipe_notifications(subject_substr: str) -> None:
    for n in frappe.db.get_all(
        "Notification Log",
        filters={"subject": ("like", f"%{subject_substr}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "Notification Log", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _make_fde_user(email: str) -> str:
    if frappe.db.exists("User", email):
        frappe.delete_doc("User", email, force=True, ignore_permissions=True)
    user = frappe.new_doc("User")
    user.update(
        {
            "email": email,
            "first_name": "Disc",
            "last_name": "Tester",
            "send_welcome_email": 0,
            "enabled": 1,
        }
    )
    user.insert(ignore_permissions=True)
    user.append("roles", {"role": "EasyEcom FDE"})
    user.save(ignore_permissions=True)
    frappe.db.commit()
    return email


class TestSchedulerWiring(FrappeTestCase):
    """The hooks.py scheduler_events entry resolves to a callable."""

    def test_scheduler_target_is_callable(self) -> None:
        self.assertTrue(callable(scheduled_discover_locations))

    def test_scheduler_target_swallows_exceptions(self) -> None:
        """A failure inside pull_locations must not raise out of the
        scheduler hook — that would crash the scheduler tick."""
        # Monkey-patch pull_locations to raise; the scheduler wrapper
        # must catch it.
        orig = location_discovery.pull_locations

        def _boom(**kwargs):
            raise RuntimeError("simulated EE outage")

        location_discovery.pull_locations = _boom
        try:
            # Must NOT raise.
            scheduled_discover_locations()
        finally:
            location_discovery.pull_locations = orig


class TestWhitelistWrapper(FrappeTestCase):
    """discover_locations is the FDE-facing wrapper."""

    PREFIX = "trig-wl-"

    def setUp(self) -> None:
        self._original_user = frappe.session.user
        _wipe_locations(self.PREFIX)

    def tearDown(self) -> None:
        frappe.set_user(self._original_user)
        _wipe_locations(self.PREFIX)
        for email in ("nofde-discover@test.local",):
            if frappe.db.exists("User", email):
                frappe.delete_doc("User", email, force=True, ignore_permissions=True)

    def test_operator_role_rejected(self) -> None:
        """EasyEcom Operator is read-only — the discovery button must
        refuse to fire for them."""
        email = "nofde-discover@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        user = frappe.new_doc("User")
        user.update(
            {
                "email": email,
                "first_name": "NoFDE",
                "send_welcome_email": 0,
                "enabled": 1,
            }
        )
        user.insert(ignore_permissions=True)
        user.append("roles", {"role": "EasyEcom Operator"})
        user.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.set_user(email)
        with self.assertRaises(frappe.PermissionError):
            discover_locations()

    def test_summary_shape_on_success(self) -> None:
        """The wrapper returns a dict the JS handler can render. We
        invoke upsert_locations_from_payload directly first so the
        wrapper's internal call to pull_locations is the layer that
        decides 'new vs updated' — bypassing the HTTP layer entirely by
        seeding rows via the same upsert code path that pull_locations
        ultimately invokes."""
        # Pre-seed two rows (one fresh, one we'll then mark as 'mapped'
        # so the next discovery treats it as Updated rather than New).
        rows = [
            {
                "location_key": f"{self.PREFIX}fresh",
                "location_name": "fresh",
                "company_id": 1,
                "stockHandle": 1,
            },
            {
                "location_key": f"{self.PREFIX}existing",
                "location_name": "existing",
                "company_id": 2,
                "stockHandle": 0,
            },
        ]
        upsert_locations_from_payload(rows)
        frappe.db.commit()

        # Mock pull_locations so the wrapper doesn't hit the HTTP layer.
        orig = location_discovery.pull_locations

        def _stub(**kwargs):
            return upsert_locations_from_payload(rows)

        location_discovery.pull_locations = _stub
        try:
            # Mark 'existing' as mapped so it counts as updated, not new.
            frappe.db.set_value(
                "EasyEcom Location",
                f"ECS-LOC-{self.PREFIX}existing",
                {"frappe_company": "_Test Company", "workflow_state": "Mapped but not Live"},
            )
            frappe.db.commit()
            result = discover_locations()
        finally:
            location_discovery.pull_locations = orig

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["new_locations"], [f"ECS-LOC-{self.PREFIX}fresh"])

    def test_wrapper_catches_exceptions(self) -> None:
        """A pull failure returns {ok: False, message: ...} — never
        raises through the whitelist boundary."""
        orig = location_discovery.pull_locations

        def _boom(**kwargs):
            raise RuntimeError("ee unreachable")

        location_discovery.pull_locations = _boom
        try:
            result = discover_locations()
        finally:
            location_discovery.pull_locations = orig
        self.assertFalse(result["ok"])
        self.assertIn("ee unreachable", result["message"])


class TestSplitNewVsUpdated(FrappeTestCase):
    """The heuristic that splits succeeded docnames into new (To Map +
    no Company) vs updated (everything else)."""

    PREFIX = "trig-split-"

    def setUp(self) -> None:
        _wipe_locations(self.PREFIX)

    def tearDown(self) -> None:
        _wipe_locations(self.PREFIX)

    def test_to_map_no_company_counts_as_new(self) -> None:
        rows = [
            {
                "location_key": f"{self.PREFIX}fresh",
                "location_name": "fresh",
                "stockHandle": 1,
            }
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        new, updated = _split_new_vs_updated(outcome.succeeded)
        self.assertEqual(new, [f"ECS-LOC-{self.PREFIX}fresh"])
        self.assertEqual(updated, [])

    def test_mapped_row_counts_as_updated(self) -> None:
        rows = [
            {
                "location_key": f"{self.PREFIX}live",
                "location_name": "live",
                "stockHandle": 1,
            }
        ]
        upsert_locations_from_payload(rows)
        frappe.db.commit()
        # Move it out of To Map to look like a previously-mapped row.
        frappe.db.set_value(
            "EasyEcom Location",
            f"ECS-LOC-{self.PREFIX}live",
            {"frappe_company": "_Test Company", "workflow_state": "Mapped but not Live"},
        )
        frappe.db.commit()

        # Second pull-then-split: now treated as Updated.
        outcome2 = upsert_locations_from_payload(rows)
        frappe.db.commit()
        new, updated = _split_new_vs_updated(outcome2.succeeded)
        self.assertEqual(new, [])
        self.assertEqual(updated, [f"ECS-LOC-{self.PREFIX}live"])

    def test_empty_input_returns_empty_partition(self) -> None:
        new, updated = _split_new_vs_updated([])
        self.assertEqual(new, [])
        self.assertEqual(updated, [])


class TestNotificationLog(FrappeTestCase):
    """One Notification Log row per EasyEcom FDE user when new locations
    appear. Quiet on empty input."""

    PREFIX = "trig-notif-"
    SUBJECT_NEEDLE = "EasyEcom:"
    FDE_EMAIL_1 = "fde1-discover@test.local"
    FDE_EMAIL_2 = "fde2-discover@test.local"

    def setUp(self) -> None:
        _wipe_locations(self.PREFIX)
        _wipe_notifications(self.SUBJECT_NEEDLE)
        _make_fde_user(self.FDE_EMAIL_1)
        _make_fde_user(self.FDE_EMAIL_2)

    def tearDown(self) -> None:
        _wipe_locations(self.PREFIX)
        _wipe_notifications(self.SUBJECT_NEEDLE)
        for email in (self.FDE_EMAIL_1, self.FDE_EMAIL_2):
            if frappe.db.exists("User", email):
                frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        frappe.db.commit()

    def test_users_with_role_finds_fdes(self) -> None:
        users = _users_with_role("EasyEcom FDE")
        self.assertIn(self.FDE_EMAIL_1, users)
        self.assertIn(self.FDE_EMAIL_2, users)

    def test_empty_input_writes_no_notifications(self) -> None:
        _notify_if_new_locations([])
        frappe.db.commit()
        rows = frappe.db.get_all(
            "Notification Log",
            filters={"subject": ("like", f"%{self.SUBJECT_NEEDLE}%")},
        )
        self.assertEqual(len(rows), 0)

    def test_new_locations_fan_out_one_per_fde(self) -> None:
        # Discover one new location.
        rows = [
            {
                "location_key": f"{self.PREFIX}new1",
                "location_name": "n1",
                "stockHandle": 1,
            }
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        new, _updated = _split_new_vs_updated(outcome.succeeded)
        _notify_if_new_locations(new)
        frappe.db.commit()

        notifs = frappe.db.get_all(
            "Notification Log",
            filters={"subject": ("like", f"%{self.SUBJECT_NEEDLE}%")},
            fields=["name", "for_user", "subject", "document_type", "document_name"],
        )
        # One per FDE.
        users_notified = {n.for_user for n in notifs}
        self.assertIn(self.FDE_EMAIL_1, users_notified)
        self.assertIn(self.FDE_EMAIL_2, users_notified)
        # Subject says "1 new location(s)".
        sample = notifs[0]
        self.assertIn("1 new location", sample.subject)
        # document_type links to EasyEcom Location for the bell pivot.
        self.assertEqual(sample.document_type, "EasyEcom Location")
        self.assertEqual(sample.document_name, f"ECS-LOC-{self.PREFIX}new1")

    def test_summary_includes_count_and_sample(self) -> None:
        """Multiple new locations → one summary per FDE with a count
        and the first-five sample."""
        rows = [
            {
                "location_key": f"{self.PREFIX}batch-{i}",
                "location_name": f"b{i}",
                "stockHandle": 1,
            }
            for i in range(7)
        ]
        outcome = upsert_locations_from_payload(rows)
        frappe.db.commit()
        new, _updated = _split_new_vs_updated(outcome.succeeded)
        _notify_if_new_locations(new)
        frappe.db.commit()

        notifs = frappe.db.get_all(
            "Notification Log",
            filters={"subject": ("like", f"%{self.SUBJECT_NEEDLE}%")},
            fields=["name", "for_user", "subject", "email_content"],
        )
        # Both of OUR test FDEs got a notification. (Other suite FDE
        # users may exist from leaky tests; we don't pin the total count.)
        users_notified = {n.for_user for n in notifs}
        self.assertIn(self.FDE_EMAIL_1, users_notified)
        self.assertIn(self.FDE_EMAIL_2, users_notified)
        # Each notification's body has the 7-count and 5-sample suffix.
        for_email_1 = [n for n in notifs if n.for_user == self.FDE_EMAIL_1][0]
        self.assertIn("7 new", for_email_1.email_content)
        self.assertIn("(2 more)", for_email_1.email_content)
