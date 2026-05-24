"""Integration tests for the §6.3.5 enqueue facade strictness.

The completion packet changed the facade to REQUIRE idempotency_key;
silent generic-formula substitution was removed. Callers must pass a
key built via easyecom.utils.idempotency.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.queue import enqueue_easyecom_job
from ecommerce_super.easyecom.utils import idempotency


def _cleanup_qjs(prefix_marker: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Queue Job",
        filters={"idempotency_key": ("like", f"%{prefix_marker}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Queue Job", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestFacadeRequiresIdempotencyKey(FrappeTestCase):
    """§2.7 no silent divergence — facade must not invent a key."""

    def test_missing_key_raises(self) -> None:
        with self.assertRaises(ValueError) as cm:
            enqueue_easyecom_job(
                job_type="Item Push",
                company="_Test Company",
                payload={"x": 1},
                # idempotency_key=None intentionally
            )
        self.assertIn("idempotency_key", str(cm.exception))
        self.assertIn("utils.idempotency", str(cm.exception))

    def test_empty_string_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            enqueue_easyecom_job(
                job_type="Item Push",
                company="_Test Company",
                payload={"x": 1},
                idempotency_key="",
            )


class TestFacadeAcceptsBuilderKey(FrappeTestCase):
    """When given a per-op key from the new module, the facade works."""

    MARKER = "test-facade-accepts"

    def setUp(self) -> None:
        _cleanup_qjs(self.MARKER)

    def tearDown(self) -> None:
        _cleanup_qjs(self.MARKER)

    def test_with_item_push_key(self) -> None:
        # Mix the marker into the change_hash so cleanup can find it.
        ch = idempotency.change_hash({"_marker": self.MARKER})
        key = idempotency.item_push_key(
            company="_Test Company",
            item_code="X",
            ee_location_key="LOC-1",
            change_hash=ch,
        )
        # No target_doctype/target_name — Dynamic Link validation needs a
        # real linked record and this test is about facade behaviour, not
        # link validation.
        name = enqueue_easyecom_job(
            job_type="Item Push",
            company="_Test Company",
            payload={"x": 1},
            idempotency_key=key,
        )
        qj = frappe.get_doc("EasyEcom Queue Job", name)
        self.assertEqual(qj.idempotency_key, key)
        self.assertEqual(qj.state, "Queued")
        self.assertEqual(qj.queue_tier, "default")
        # correlation_id auto-minted when not provided
        self.assertTrue(qj.correlation_id)


class TestParentCorrelationIdPropagation(FrappeTestCase):
    """§6.2 replay-induced jobs: new correlation_id with parent pointing
    at the original."""

    MARKER = "test-parent-corr"

    def setUp(self) -> None:
        _cleanup_qjs(self.MARKER)

    def tearDown(self) -> None:
        _cleanup_qjs(self.MARKER)

    def test_parent_correlation_id_stored(self) -> None:
        # Original
        orig_corr = "11111111-2222-7333-8444-555555555555"
        orig_key = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="_Test Company",
            payload={"_marker": self.MARKER, "original": True},
        )
        enqueue_easyecom_job(
            job_type="Webhook Process",
            company="_Test Company",
            idempotency_key=orig_key,
            correlation_id=orig_corr,
        )

        # Replay-induced — fresh corr, parent points at original.
        replay_key = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="_Test Company",
            payload={"_marker": self.MARKER, "replay": True},
        )
        replay_name = enqueue_easyecom_job(
            job_type="Webhook Process",
            company="_Test Company",
            idempotency_key=replay_key,
            parent_correlation_id=orig_corr,
        )
        replay_qj = frappe.get_doc("EasyEcom Queue Job", replay_name)
        self.assertEqual(replay_qj.parent_correlation_id, orig_corr)
        self.assertNotEqual(replay_qj.correlation_id, orig_corr)
        # Fresh corr is a UUIDv7 (version nibble at position 14)
        self.assertEqual(replay_qj.correlation_id[14], "7")
