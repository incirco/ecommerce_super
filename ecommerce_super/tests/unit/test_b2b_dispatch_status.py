"""§11.6 — Dispatch status stamping tests.

When EE polling reports order_status_id 5/6/7 on a Map with a linked
Sales Invoice, _stamp_dispatch_status_on_si writes:
  - ecs_easyecom_dispatch_status (Pending/Shipped/Delivered/Returned/Cancelled)
  - ecs_easyecom_dispatched_at (first time we see 5 or 6)
  - ecs_easyecom_delivered_at (first time we see 6)
  - ecs_easyecom_tracking_url (whenever EE provides one)

Defensive design — must not raise when:
  - Map has no linked SI
  - SI doesn't have the Custom Fields yet (pre-patch installs)
  - Status_id is outside the known enum
  - DB write fails

Mocks frappe.get_doc / frappe.db.get_value / frappe.db.set_value so
the tests run without a bench.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2b_sales.polling import (
    DISPATCH_STATUS_BY_ID,
    TRACKING_URL_CANDIDATE_KEYS,
    _stamp_dispatch_status_on_si,
)


def _fake_map(*, name="ECS-B2B-FAKE", sales_invoice=None):
    m = MagicMock()
    m.name = name
    m.get.side_effect = lambda k, d=None: {
        "sales_invoice": sales_invoice,
    }.get(k, d)
    return m


def _b2b_row(
    *,
    status_id=None,
    last_update="2026-06-29 10:00:00",
    tracking_url=None,
    tracking_key="tracking_link",
    invoice_id=None,
):
    row = {
        "order_type_key": "businessorder",
        "last_update_date": last_update,
    }
    if status_id is not None:
        row["order_status_id"] = status_id
    if tracking_url is not None:
        row[tracking_key] = tracking_url
    if invoice_id is not None:
        row["invoice_id"] = invoice_id
    return row


# ============================================================
# Status enum / mapping
# ============================================================


class TestDispatchStatusByIdEnum(unittest.TestCase):

    def test_pending_statuses_collapse(self):
        for sid in (1, 2, 3, 4, 30):
            self.assertEqual(DISPATCH_STATUS_BY_ID[sid], "Pending")

    def test_shipment_milestones(self):
        self.assertEqual(DISPATCH_STATUS_BY_ID[5], "Shipped")
        self.assertEqual(DISPATCH_STATUS_BY_ID[6], "Delivered")
        self.assertEqual(DISPATCH_STATUS_BY_ID[7], "Returned")

    def test_cancelled(self):
        self.assertEqual(DISPATCH_STATUS_BY_ID[9], "Cancelled")

    def test_does_not_cover_8(self):
        # status_id=8 is not in EE's documented enum — should not be
        # silently absorbed
        self.assertNotIn(8, DISPATCH_STATUS_BY_ID)


# ============================================================
# Stamping behaviour
# ============================================================


class TestStampDispatchStatus(unittest.TestCase):

    def test_returns_none_when_no_linked_si(self):
        map_doc = _fake_map(sales_invoice=None)
        with patch("frappe.get_doc", return_value=map_doc):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=5)],
            )
        self.assertIsNone(result)

    def test_returns_none_when_no_b2b_rows(self):
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        with patch("frappe.get_doc", return_value=map_doc):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[{"order_type_key": "salesorder"}],  # not businessorder
            )
        self.assertIsNone(result)

    def test_returns_none_when_status_id_outside_enum(self):
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        with patch("frappe.get_doc", return_value=map_doc):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=99)],  # not in enum
            )
        self.assertIsNone(result)

    def test_stamps_shipped_and_dispatched_at_on_first_observation(self):
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        # SI has no dispatch fields populated yet
        si_current = {
            "ecs_easyecom_dispatch_status": None,
            "ecs_easyecom_dispatched_at": None,
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=5)],
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["new_status"], "Shipped")
        self.assertIn("ecs_easyecom_dispatch_status", result["fields_written"])
        self.assertIn("ecs_easyecom_dispatched_at", result["fields_written"])
        # delivered_at NOT written (still in Shipped)
        self.assertNotIn("ecs_easyecom_delivered_at", result["fields_written"])
        # set_value was called with the SI name + updates dict
        args, _ = mock_set.call_args
        self.assertEqual(args[0], "Sales Invoice")
        self.assertEqual(args[1], "ACC-SINV-001")
        updates = args[2]
        self.assertEqual(updates["ecs_easyecom_dispatch_status"], "Shipped")
        self.assertIn("ecs_easyecom_dispatched_at", updates)

    def test_delivered_backfills_dispatched_at_when_missed(self):
        """If polling never observed Shipped (e.g. fast-shipping →
        delivered between two ticks), the Delivered observation must
        backfill dispatched_at — never leave it empty for a delivered
        order."""
        map_doc = _fake_map(sales_invoice="ACC-SINV-002")
        si_current = {
            "ecs_easyecom_dispatch_status": "Pending",
            "ecs_easyecom_dispatched_at": None,
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=6)],  # Delivered
            )
        self.assertEqual(result["new_status"], "Delivered")
        updates = mock_set.call_args[0][2]
        self.assertEqual(updates["ecs_easyecom_dispatch_status"], "Delivered")
        self.assertIn("ecs_easyecom_dispatched_at", updates)
        self.assertIn("ecs_easyecom_delivered_at", updates)

    def test_idempotent_when_already_stamped(self):
        """Re-running the same payload with the same status writes nothing."""
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        si_current = {
            "ecs_easyecom_dispatch_status": "Shipped",
            "ecs_easyecom_dispatched_at": "2026-06-29 09:00:00",
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": "https://courier.example/abc",
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(
                    status_id=5,
                    tracking_url="https://courier.example/abc",
                )],
            )
        self.assertIsNone(result)
        mock_set.assert_not_called()

    def test_dispatched_at_not_overwritten_on_subsequent_shipped(self):
        """dispatched_at is set once on first Shipped observation —
        subsequent Shipped observations (idempotent polls) must NOT
        overwrite the earlier timestamp."""
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        si_current = {
            "ecs_easyecom_dispatch_status": "Shipped",
            "ecs_easyecom_dispatched_at": "2026-06-29 09:00:00",
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=5)],
            )
        self.assertIsNone(result)
        mock_set.assert_not_called()

    def test_tracking_url_picked_up_from_alternate_key(self):
        """EE payloads vary on the field name for the tracking URL —
        the scan must try several keys."""
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        si_current = {
            "ecs_easyecom_dispatch_status": "Shipped",
            "ecs_easyecom_dispatched_at": "2026-06-29 09:00:00",
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(
                    status_id=5,
                    tracking_url="https://courier.example/xyz",
                    tracking_key="courier_tracking_url",  # alt key
                )],
            )
        self.assertIsNotNone(result)
        updates = mock_set.call_args[0][2]
        self.assertEqual(
            updates["ecs_easyecom_tracking_url"],
            "https://courier.example/xyz",
        )

    def test_cancelled_status_stamped(self):
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        si_current = {
            "ecs_easyecom_dispatch_status": "Pending",
            "ecs_easyecom_dispatched_at": None,
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=9)],
            )
        self.assertEqual(result["new_status"], "Cancelled")
        updates = mock_set.call_args[0][2]
        self.assertEqual(updates["ecs_easyecom_dispatch_status"], "Cancelled")
        # No dispatched_at/delivered_at on cancel-from-pending
        self.assertNotIn("ecs_easyecom_dispatched_at", updates)

    def test_silent_when_si_lacks_custom_fields(self):
        """Pre-§11.6 installs: SI doesn't have the dispatch fields yet
        so get_value raises. Must return None without propagating."""
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", side_effect=RuntimeError("col not found")),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=5)],
            )
        self.assertIsNone(result)

    def test_silent_when_set_value_raises(self):
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        si_current = {
            "ecs_easyecom_dispatch_status": None,
            "ecs_easyecom_dispatched_at": None,
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value", side_effect=RuntimeError("db down")),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE",
                rows=[_b2b_row(status_id=5)],
            )
        self.assertIsNone(result)

    def test_silent_when_map_lookup_raises(self):
        with patch("frappe.get_doc", side_effect=RuntimeError("not found")):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-MISSING",
                rows=[_b2b_row(status_id=5)],
            )
        self.assertIsNone(result)

    def test_picks_latest_row_for_multi_shipment(self):
        """Shipment-split orders return multiple businessorder rows.
        We stamp using the latest by last_update_date."""
        map_doc = _fake_map(sales_invoice="ACC-SINV-001")
        si_current = {
            "ecs_easyecom_dispatch_status": "Shipped",
            "ecs_easyecom_dispatched_at": "2026-06-29 09:00:00",
            "ecs_easyecom_delivered_at": None,
            "ecs_easyecom_tracking_url": None,
        }
        rows = [
            _b2b_row(status_id=5, last_update="2026-06-29 09:00:00"),
            _b2b_row(status_id=6, last_update="2026-06-29 14:00:00"),  # latest
        ]
        with (
            patch("frappe.get_doc", return_value=map_doc),
            patch("frappe.db.get_value", return_value=si_current),
            patch("frappe.db.set_value") as mock_set,
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-29 10:30:00",
            ),
        ):
            result = _stamp_dispatch_status_on_si(
                map_name="ECS-B2B-FAKE", rows=rows,
            )
        self.assertEqual(result["status_id"], 6)
        self.assertEqual(result["new_status"], "Delivered")


# ============================================================
# Tracking key precedence
# ============================================================


class TestTrackingUrlKeys(unittest.TestCase):

    def test_tracking_link_is_first_candidate(self):
        # If the spec says "tracking_link" is the canonical key, it
        # must be probed first so EE payloads using both don't
        # silently land on an aliased key.
        self.assertEqual(TRACKING_URL_CANDIDATE_KEYS[0], "tracking_link")

    def test_multiple_candidates_exist(self):
        # At least 3 alternates — keeps the scan defensive against
        # payload-key drift.
        self.assertGreaterEqual(len(TRACKING_URL_CANDIDATE_KEYS), 3)


if __name__ == "__main__":
    unittest.main()
