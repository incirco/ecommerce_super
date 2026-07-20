"""gh#227 — multi-invoice per SO regression lock.

Historical bug: a B2B Sales Order that produced multiple EE invoices
(partial fulfilment, split shipments) only mirrored the first one.
`gsp_handler.find_or_create_si_for_gsp` had a 2-step lookup that
returned the Map.sales_invoice link once set, so the second invoice
was silently collapsed into the first SI. `polling._apply_decision`
had the same bug in a different shape — gated on Map.sales_invoice
being empty, and only picked the max()-latest invoice_row per poll.

The fix (see PR closing gh#227):
  1. `invoice_mirror.mirror_si_from_ee_response` — added a legacy-
     adoption branch: if Map.sales_invoice points at a pre-fix SI
     with no invoice_id stamped, adopt it (stamp + return) instead of
     duplicating.
  2. `gsp_handler.find_or_create_si_for_gsp` — deleted step 2
     (Map.sales_invoice → return). Delegates to the mirror; second
     invoice_id for the same SO falls through to create a new SI.
  3. `polling._apply_decision` — removed the `not
     map_doc.get("sales_invoice")` guard; iterates ALL invoice_rows
     in the poll response (sorted by last_update_date), letting the
     mirror's own invoice_id idempotency dedup.

These tests lock the caller-level contract. Mirror-level scenarios
are locked in test_mirror_native_make_sales_invoice.py's
TestLegacyAdoptionShim + TestMultiInvoicePerSO.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows.b2b_sales import (
    gsp_handler as handler_mod,
    polling as polling_mod,
)
from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
    InvoiceMirrorError,
    InvoiceMirrorVariance,
)


def _ee_row(**overrides):
    base = {
        "invoice_id": "EE-INV-A",
        "reference_code": "SO-MULTI-001",
        "total_amount": 1000.0,
    }
    base.update(overrides)
    return base


# ============================================================
# Handler-level gh#227 lock
# ============================================================


class TestHandlerNoLongerCollapsesSecondInvoice(unittest.TestCase):
    """gh#227: the handler's deleted step 2 must NOT come back. When
    a second invoice_id arrives for the same SO, the handler must
    fall through to `mirror_si_from_ee_response`, which will create
    a new SI (or return one if idempotent). It must NOT short-circuit
    on Map.sales_invoice."""

    def test_second_invoice_id_reaches_the_mirror(self):
        """First invoice mirrored SI-A + set Map.sales_invoice=SI-A.
        Second invoice arrives with a DIFFERENT invoice_id. Handler
        must NOT return SI-A; must delegate to mirror instead."""
        mirror_stub = MagicMock(return_value={
            "sales_invoice": "SI-B-NEW",
            "operation": "created",
            "variance_pct": 0.0,
            "ee_total": 1000.0,
            "si_total": 1000.0,
        })

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                # Second invoice_id — step 1 idempotency misses.
                # (The deleted step 2 would have returned SI-A from
                # Map.sales_invoice — we prove that path no longer runs.)
                return None
            if doctype == "EasyEcom B2B Order Map":
                # step 3 loads the Map — return a stub name
                return "MAP-MULTI-001"
            return None

        map_doc_stub = MagicMock()
        map_doc_stub.name = "MAP-MULTI-001"
        map_doc_stub.sales_order = "SO-MULTI-001"
        # Map.sales_invoice is set to SI-A (the FIRST invoice). The bug
        # was that step 2 returned this. We prove it no longer does.
        map_doc_stub.sales_invoice = "SI-A-FIRST"

        with (
            patch.object(handler_mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(handler_mod.frappe, "get_doc", return_value=map_doc_stub),
            patch.object(handler_mod.frappe.db, "set_value"),
            patch.object(handler_mod.frappe.db, "commit"),
            patch.object(handler_mod, "now_datetime", return_value="2026-07-19 12:00:00"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                mirror_stub,
            ),
            patch.object(handler_mod, "_post_variance_comment_on_si"),
        ):
            result = handler_mod.find_or_create_si_for_gsp(
                ee_row=_ee_row(invoice_id="EE-INV-B"),
                ee_account="Test Account",
            )

        # Handler MUST return the NEW SI created by the mirror, NOT
        # the old SI-A that was on Map.sales_invoice.
        self.assertEqual(result, "SI-B-NEW")
        self.assertNotEqual(
            result, "SI-A-FIRST",
            "gh#227 regression: handler collapsed second invoice into first SI",
        )
        mirror_stub.assert_called_once()

    def test_same_invoice_id_returns_via_step_1_idempotency(self):
        """Step 1 (invoice_id lookup) still handles the true
        idempotent case — same invoice_id twice returns same SI."""
        mirror_stub = MagicMock()

        def _get_value(doctype, filters=None, field=None, **_):
            if doctype == "Sales Invoice":
                if isinstance(filters, dict) and \
                        filters.get("ecs_easyecom_invoice_id") == "EE-INV-SAME":
                    return "SI-CACHED"
            return None

        with (
            patch.object(handler_mod.frappe.db, "get_value", side_effect=_get_value),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                mirror_stub,
            ),
        ):
            result = handler_mod.find_or_create_si_for_gsp(
                ee_row=_ee_row(invoice_id="EE-INV-SAME"),
                ee_account="Test Account",
            )

        self.assertEqual(result, "SI-CACHED")
        # Step 1 short-circuited — mirror not invoked
        mirror_stub.assert_not_called()


# ============================================================
# Polling-level gh#227 lock
# ============================================================


class TestPollingIteratesAllInvoiceRows(unittest.TestCase):
    """gh#227: polling used to (a) pick only max() invoice_row, and
    (b) gate on Map.sales_invoice being empty. Both collapsed
    multi-invoice orders. Now iterates ALL invoice rows and lets the
    mirror handle idempotency."""

    def _run_polling(self, *, rows, mirror_side_effect, map_doc=None):
        """Invoke polling._apply_decision for the 'Invoice Pending'
        transition path. Captures set_value writes + returns
        (updates, mirror_call_count)."""
        map_doc = map_doc or self._map_doc_with_no_prior_si()
        captured_updates = {}
        mirror_calls = []

        def _fake_mirror(*, map_doc, ee_row):
            mirror_calls.append(ee_row)
            outcome = mirror_side_effect(ee_row)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def _fake_set_value(doctype, name, updates_or_field, value=None, **_):
            if isinstance(updates_or_field, dict):
                captured_updates.update(updates_or_field)

        def _fake_get_value(doctype, filters=None, field=None, **_):
            # Company lookup at the top of _apply_decision
            if doctype == "Sales Order" and field == "company":
                return "Test Co"
            # Variance-recovery SI lookup by invoice_id
            if doctype == "Sales Invoice":
                inv_id = (filters or {}).get("ecs_easyecom_invoice_id")
                if inv_id:
                    return f"SI-FOR-{inv_id}"
            return None

        with (
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                side_effect=_fake_mirror,
            ),
            patch.object(polling_mod.frappe.db, "set_value", side_effect=_fake_set_value),
            patch.object(polling_mod.frappe.db, "get_value", side_effect=_fake_get_value),
            patch.object(polling_mod.frappe.db, "commit"),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
            ),
            patch.object(polling_mod, "_stamp_dispatch_status_on_si", return_value=None),
        ):
            polling_mod._apply_decision(
                decision="transition_to",
                payload="Invoice Pending",
                map_doc=map_doc,
                rows=rows,
                ee_account_name="Test",
                correlation_id="corr-xyz",
            )
        return captured_updates, mirror_calls

    def _map_doc_with_no_prior_si(self, prior_si=None):
        m = MagicMock()
        m.name = "MAP-MULTI-POLL-001"
        m.sales_order = "SO-MULTI-001"
        m.company = "Test Co"
        m.get = lambda k, default=None: {
            "sales_invoice": prior_si,
            "ee_invoice_number": None,
        }.get(k, default)
        return m

    def test_poll_with_two_invoice_rows_mirrors_both(self):
        """EE returns 2 invoice_rows in one poll (invoice_ids A + B).
        Mirror must be called TWICE — once per invoice_id. Previously
        only the max()-latest row was mirrored, and only if Map had no
        SI yet."""
        rows = [
            {
                "order_type_key": "businessorder",
                "invoice_id": "EE-INV-A",
                "invoice_number": "INV-A-001",
                "last_update_date": "2026-07-19 10:00:00",
                "total_amount": 1000.0,
            },
            {
                "order_type_key": "businessorder",
                "invoice_id": "EE-INV-B",
                "invoice_number": "INV-B-002",
                "last_update_date": "2026-07-19 12:00:00",
                "total_amount": 500.0,
            },
        ]

        outcomes = {
            "EE-INV-A": {
                "sales_invoice": "SI-A", "operation": "created",
                "variance_pct": 0.0, "ee_total": 1000.0, "si_total": 1000.0,
            },
            "EE-INV-B": {
                "sales_invoice": "SI-B", "operation": "created",
                "variance_pct": 0.0, "ee_total": 500.0, "si_total": 500.0,
            },
        }
        updates, mirror_calls = self._run_polling(
            rows=rows,
            mirror_side_effect=lambda ee_row: outcomes[ee_row["invoice_id"]],
        )

        self.assertEqual(len(mirror_calls), 2, "Both invoices should mirror")
        # Order-preserving iteration (sorted by last_update_date)
        self.assertEqual(mirror_calls[0]["invoice_id"], "EE-INV-A")
        self.assertEqual(mirror_calls[1]["invoice_id"], "EE-INV-B")
        # Map.sales_invoice tracks LATEST — SI-B
        self.assertEqual(updates.get("sales_invoice"), "SI-B")
        self.assertEqual(updates.get("status"), "Invoice Generated")

    def test_prior_si_on_map_does_NOT_block_new_invoice_mirroring(self):
        """Map already has sales_invoice=SI-A (from prior poll). A new
        invoice_id shows up. Previously blocked by the guard; now
        allowed through. Mirror handles idempotency."""
        rows = [
            {
                "order_type_key": "businessorder",
                "invoice_id": "EE-INV-B",
                "invoice_number": "INV-B-002",
                "last_update_date": "2026-07-19 12:00:00",
                "total_amount": 500.0,
            },
        ]
        map_doc = self._map_doc_with_no_prior_si(prior_si="SI-A-EXISTING")

        _, mirror_calls = self._run_polling(
            rows=rows,
            mirror_side_effect=lambda ee_row: {
                "sales_invoice": "SI-B", "operation": "created",
                "variance_pct": 0.0, "ee_total": 500.0, "si_total": 500.0,
            },
            map_doc=map_doc,
        )
        self.assertEqual(len(mirror_calls), 1,
                         "gh#227 regression: prior Map.sales_invoice blocked new mirror")

    def test_rows_without_invoice_id_are_skipped(self):
        """Rows with invoice_number but no invoice_id (or vice versa)
        are skipped — invoice_id is the required anchor."""
        rows = [
            {"order_type_key": "businessorder",
             "invoice_number": "INV-X", "invoice_id": None,
             "last_update_date": "2026-07-19 10:00:00"},
            {"order_type_key": "businessorder",
             "invoice_number": "INV-Y", "invoice_id": "EE-INV-Y",
             "last_update_date": "2026-07-19 11:00:00", "total_amount": 100.0},
        ]
        _, mirror_calls = self._run_polling(
            rows=rows,
            mirror_side_effect=lambda ee_row: {
                "sales_invoice": "SI-Y", "operation": "created",
                "variance_pct": 0.0, "ee_total": 100.0, "si_total": 100.0,
            },
        )
        self.assertEqual(len(mirror_calls), 1)
        self.assertEqual(mirror_calls[0]["invoice_id"], "EE-INV-Y")

    def test_prerequisite_error_on_first_invoice_breaks_loop(self):
        """If invoice A fails with InvoiceMirrorError (missing prereq),
        halt the loop — subsequent invoices likely fail the same way
        (same Customer Map / Item Map). Next polling tick retries all."""
        rows = [
            {"order_type_key": "businessorder",
             "invoice_id": "EE-INV-A", "invoice_number": "INV-A",
             "last_update_date": "2026-07-19 10:00:00", "total_amount": 100.0},
            {"order_type_key": "businessorder",
             "invoice_id": "EE-INV-B", "invoice_number": "INV-B",
             "last_update_date": "2026-07-19 11:00:00", "total_amount": 200.0},
        ]

        def _mirror(ee_row):
            if ee_row["invoice_id"] == "EE-INV-A":
                return InvoiceMirrorError("missing Customer Map")
            return {
                "sales_invoice": "SI-B", "operation": "created",
                "variance_pct": 0.0, "ee_total": 200.0, "si_total": 200.0,
            }

        _, mirror_calls = self._run_polling(rows=rows, mirror_side_effect=_mirror)
        # Loop broke — B was never attempted
        self.assertEqual(len(mirror_calls), 1)
        self.assertEqual(mirror_calls[0]["invoice_id"], "EE-INV-A")

    def test_variance_on_one_invoice_does_not_block_siblings(self):
        """Variance is a SOFT signal (SI was still created). Continue
        iterating remaining invoices; alert on the last-seen variance."""
        rows = [
            {"order_type_key": "businessorder",
             "invoice_id": "EE-INV-A", "invoice_number": "INV-A",
             "last_update_date": "2026-07-19 10:00:00", "total_amount": 100.0},
            {"order_type_key": "businessorder",
             "invoice_id": "EE-INV-B", "invoice_number": "INV-B",
             "last_update_date": "2026-07-19 11:00:00", "total_amount": 200.0},
        ]

        def _mirror(ee_row):
            if ee_row["invoice_id"] == "EE-INV-A":
                return InvoiceMirrorVariance("variance on A")
            return {
                "sales_invoice": "SI-B", "operation": "created",
                "variance_pct": 0.0, "ee_total": 200.0, "si_total": 200.0,
            }

        _, mirror_calls = self._run_polling(rows=rows, mirror_side_effect=_mirror)
        # Both were attempted — variance on A didn't halt B
        self.assertEqual(len(mirror_calls), 2)


if __name__ == "__main__":
    unittest.main()
