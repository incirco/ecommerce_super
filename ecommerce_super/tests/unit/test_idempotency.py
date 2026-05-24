"""Unit tests for §6.1 per-operation idempotency-key builders.

The contract: same logical operation → same key (idempotent);
any input change → different key (so genuine updates fire).
"""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.utils import idempotency


class TestPerOperationKeyBuilders(unittest.TestCase):
    """§6.1 table — formulae verified."""

    def test_item_push_key_reproducible(self) -> None:
        a = idempotency.item_push_key(
            company="ACME",
            item_code="SKU-001",
            ee_location_key="LOC-1",
            change_hash="abc123",
        )
        b = idempotency.item_push_key(
            company="ACME",
            item_code="SKU-001",
            ee_location_key="LOC-1",
            change_hash="abc123",
        )
        self.assertEqual(a, b)
        # Hex SHA-256 = 64 chars
        self.assertEqual(len(a), 64)

    def test_item_push_key_differs_on_any_field(self) -> None:
        base = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash="h",
        )
        # Each field changing produces a distinct key.
        self.assertNotEqual(
            base,
            idempotency.item_push_key(
                company="B",
                item_code="X",
                ee_location_key="L",
                change_hash="h",
            ),
        )
        self.assertNotEqual(
            base,
            idempotency.item_push_key(
                company="A",
                item_code="Y",
                ee_location_key="L",
                change_hash="h",
            ),
        )
        self.assertNotEqual(
            base,
            idempotency.item_push_key(
                company="A",
                item_code="X",
                ee_location_key="M",
                change_hash="h",
            ),
        )
        self.assertNotEqual(
            base,
            idempotency.item_push_key(
                company="A",
                item_code="X",
                ee_location_key="L",
                change_hash="i",
            ),
        )

    def test_customer_push_key_distinct_from_item(self) -> None:
        """The op-namespace prefix means item/customer with identical
        remaining args produce different keys."""
        item = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash="h",
        )
        cust = idempotency.customer_push_key(
            company="A",
            customer_name="X",
            ee_location_key="L",
            change_hash="h",
        )
        self.assertNotEqual(item, cust)

    def test_supplier_push_key_reproducible(self) -> None:
        a = idempotency.supplier_push_key(
            company="ACME",
            supplier_name="V-001",
            ee_location_key="LOC-1",
            change_hash="abc",
        )
        b = idempotency.supplier_push_key(
            company="ACME",
            supplier_name="V-001",
            ee_location_key="LOC-1",
            change_hash="abc",
        )
        self.assertEqual(a, b)

    def test_po_push_key_no_change_hash(self) -> None:
        """PO names are immutable — no change_hash in the formula (§6.1)."""
        a = idempotency.po_push_key(
            company="ACME", po_name="PO-001", ee_location_key="L"
        )
        b = idempotency.po_push_key(
            company="ACME", po_name="PO-001", ee_location_key="L"
        )
        self.assertEqual(a, b)
        self.assertNotEqual(
            a,
            idempotency.po_push_key(
                company="ACME", po_name="PO-002", ee_location_key="L"
            ),
        )

    def test_so_push_key_distinct_from_po(self) -> None:
        po = idempotency.po_push_key(company="A", po_name="X", ee_location_key="L")
        so = idempotency.so_push_key(company="A", so_name="X", ee_location_key="L")
        self.assertNotEqual(po, so)

    def test_b2b_invoice_push_key_reproducible(self) -> None:
        a = idempotency.b2b_invoice_push_key(
            company="ACME",
            si_name="SI-001",
            ee_location_key="LOC-1",
        )
        b = idempotency.b2b_invoice_push_key(
            company="ACME",
            si_name="SI-001",
            ee_location_key="LOC-1",
        )
        self.assertEqual(a, b)


class TestChangeHashAffectsItemKey(unittest.TestCase):
    """The §6.1 'skip pushes when nothing has changed' guarantee:
    two pushes of the same item with the same payload produce the same
    key; any payload change produces a different key."""

    def test_same_payload_same_key(self) -> None:
        payload_a = {"name": "X", "rate": 10.0}
        payload_b = {"name": "X", "rate": 10.0}  # equivalent, key-order-invariant
        ch_a = idempotency.change_hash(payload_a)
        ch_b = idempotency.change_hash(payload_b)
        self.assertEqual(ch_a, ch_b)
        key_a = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash=ch_a,
        )
        key_b = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash=ch_b,
        )
        self.assertEqual(key_a, key_b)

    def test_payload_change_changes_key(self) -> None:
        ch_old = idempotency.change_hash({"rate": 10.0})
        ch_new = idempotency.change_hash({"rate": 11.0})
        self.assertNotEqual(ch_old, ch_new)
        key_old = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash=ch_old,
        )
        key_new = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash=ch_new,
        )
        self.assertNotEqual(key_old, key_new)

    def test_key_order_invariant(self) -> None:
        """Same content, different key order → same change_hash."""
        a = idempotency.change_hash({"a": 1, "b": 2})
        b = idempotency.change_hash({"b": 2, "a": 1})
        self.assertEqual(a, b)


class TestInternalJobKey(unittest.TestCase):
    """Internal-bookkeeping job types (not in §6.1) get a namespaced builder."""

    def test_internal_key_reproducible(self) -> None:
        a = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="ACME",
            target_doctype="Sales Order",
            target_name="SO-001",
            payload={"x": 1},
        )
        b = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="ACME",
            target_doctype="Sales Order",
            target_name="SO-001",
            payload={"x": 1},
        )
        self.assertEqual(a, b)

    def test_internal_key_distinct_from_item_namespace(self) -> None:
        """An 'internal' key must not collide with a §6.1 outbound key
        that happens to share trailing parts."""
        internal = idempotency.internal_job_key(
            job_type="Webhook Process",
            company="A",
            target_doctype=None,
            target_name=None,
            payload=None,
        )
        item = idempotency.item_push_key(
            company="A",
            item_code="X",
            ee_location_key="L",
            change_hash="h",
        )
        self.assertNotEqual(internal, item)
