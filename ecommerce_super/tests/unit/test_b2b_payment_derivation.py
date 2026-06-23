"""§11 Phase 1 — Payment derivation pure function tests.

Three commercial scenarios per the §11 packet §11.3:
  I.   Full prepaid       — PE total >= SO.grand_total → Prepaid(5)
  II.  Partial prepaid    — 0 < PE total < SO.grand_total → COD(2) + remainder
  III. Pure credit terms  — PE total == 0 → COD(2) + full grand_total

Plus an edge case: multiple PEs are summed (the resolver must
aggregate, not pick the latest).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.payment import (
    derive_payment_fields,
)


def _make_so(grand_total: float, name: str = "SAL-ORD-TEST-001") -> MagicMock:
    so = MagicMock()
    so.name = name
    so.grand_total = grand_total
    return so


def _make_pe(reference_no: str | None, name: str = "PE-001") -> MagicMock:
    pe = MagicMock()
    pe.reference_no = reference_no
    pe.name = name
    return pe


class TestDerivePaymentFields(unittest.TestCase):
    def test_scenario_iii_pure_credit_terms_zero_advance(self) -> None:
        """No Payment Entries → COD(2) with empty txn # and full
        grand_total as collectable."""
        so = _make_so(grand_total=10000.0)
        with patch.object(frappe, "get_all", return_value=[]):
            result = derive_payment_fields(so)
        self.assertEqual(result["paymentMode"], 2)
        self.assertEqual(result["paymentTransactionNumber"], "")
        self.assertEqual(result["collectableAmount"], 10000.0)
        self.assertEqual(result["shippingMethod"], 1)

    def test_scenario_i_full_prepaid(self) -> None:
        """Single PE >= grand_total → Prepaid(5) with txn # and 0
        collectable."""
        so = _make_so(grand_total=10000.0)
        pe = _make_pe(reference_no="UTR-ICICI-AB1234")
        with (
            patch.object(
                frappe,
                "get_all",
                return_value=[
                    {"parent": "PE-001", "allocated_amount": 10000.0}
                ],
            ),
            patch.object(frappe, "get_doc", return_value=pe),
        ):
            result = derive_payment_fields(so)
        self.assertEqual(result["paymentMode"], 5)
        self.assertEqual(result["paymentTransactionNumber"], "UTR-ICICI-AB1234")
        self.assertEqual(result["collectableAmount"], 0.0)
        self.assertEqual(result["shippingMethod"], 3)

    def test_scenario_i_overpaid_treated_as_full_prepaid(self) -> None:
        """PE total > grand_total (rare; refund pending) → Prepaid(5)
        with 0 collectable. The integration doesn't try to compute
        refunds in the payload."""
        so = _make_so(grand_total=10000.0)
        pe = _make_pe(reference_no="UTR-OVERPAY")
        with (
            patch.object(
                frappe,
                "get_all",
                return_value=[
                    {"parent": "PE-001", "allocated_amount": 12000.0}
                ],
            ),
            patch.object(frappe, "get_doc", return_value=pe),
        ):
            result = derive_payment_fields(so)
        self.assertEqual(result["paymentMode"], 5)
        self.assertEqual(result["collectableAmount"], 0.0)
        self.assertEqual(result["shippingMethod"], 3)

    def test_scenario_ii_partial_prepaid_bucketed_as_cod(self) -> None:
        """PE total < grand_total → COD(2) with remainder as
        collectable + shipping=Standard COD(1)."""
        so = _make_so(grand_total=10000.0)
        pe = _make_pe(reference_no="UTR-PARTIAL")
        with (
            patch.object(
                frappe,
                "get_all",
                return_value=[
                    {"parent": "PE-001", "allocated_amount": 3000.0}
                ],
            ),
            patch.object(frappe, "get_doc", return_value=pe),
        ):
            result = derive_payment_fields(so)
        self.assertEqual(result["paymentMode"], 2)
        self.assertEqual(result["paymentTransactionNumber"], "UTR-PARTIAL")
        self.assertEqual(result["collectableAmount"], 7000.0)
        self.assertEqual(result["shippingMethod"], 1)

    def test_multiple_payment_entries_summed(self) -> None:
        """Multiple submitted PEs against the same SO → advance is
        SUM of all allocated_amounts. Most recent PE's reference_no
        used for the transaction number."""
        so = _make_so(grand_total=10000.0)
        pe_last = _make_pe(reference_no="UTR-FINAL", name="PE-003")
        with (
            patch.object(
                frappe,
                "get_all",
                return_value=[
                    {"parent": "PE-001", "allocated_amount": 4000.0},
                    {"parent": "PE-002", "allocated_amount": 3000.0},
                    {"parent": "PE-003", "allocated_amount": 3000.0},
                ],
            ),
            patch.object(frappe, "get_doc", return_value=pe_last),
        ):
            result = derive_payment_fields(so)
        # 4000 + 3000 + 3000 = 10000 → full prepaid.
        self.assertEqual(result["paymentMode"], 5)
        self.assertEqual(result["paymentTransactionNumber"], "UTR-FINAL")
        self.assertEqual(result["collectableAmount"], 0.0)

    def test_pe_with_no_reference_no_falls_back_to_pe_name(self) -> None:
        """When PE.reference_no is empty/None, use PE.name as the
        transaction number — at least the payload carries something
        traceable back to ERPNext."""
        so = _make_so(grand_total=5000.0)
        pe = _make_pe(reference_no=None, name="PE-NO-REF-001")
        with (
            patch.object(
                frappe,
                "get_all",
                return_value=[
                    {"parent": "PE-NO-REF-001", "allocated_amount": 5000.0}
                ],
            ),
            patch.object(frappe, "get_doc", return_value=pe),
        ):
            result = derive_payment_fields(so)
        self.assertEqual(result["paymentTransactionNumber"], "PE-NO-REF-001")


if __name__ == "__main__":
    unittest.main()
