"""Payment derivation for the §11 createOrder payload.

EE's data model carries only two payment modes — Prepaid (5) and
COD (2). The client's operational reality has three commercial
scenarios; we bucket the partial-prepaid and pure-credit-terms
cases as COD with `collectableAmount` carrying the deferred amount.
EE never executes a physical cash-on-delivery action; the field is
operationally an "amount-pending" carrier.

Scenarios (§11 packet §11.3 Payment Block):
  I.   Full prepaid       — PE total >= SO.grand_total
  II.  Partial prepaid    — 0 < PE total < SO.grand_total
  III. Pure credit terms  — PE total == 0

Source: linked Payment Entry References on the Sales Order, submitted
(docstatus=1) only. Multiple PEs are summed for the advance amount.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt


def derive_payment_fields(sales_order: Any) -> dict:
    """Derive paymentMode + paymentTransactionNumber + collectableAmount
    + shippingMethod from a Sales Order's linked Payment Entries.

    Args:
        sales_order: SO docname or SO document. If a docname, fetched.

    Returns:
        {
            "paymentMode": int,                  # 2=COD, 5=Prepaid (EE enum)
            "paymentTransactionNumber": str,     # "" if no PE
            "collectableAmount": float,
            "shippingMethod": int,               # 1=Standard COD, 3=Standard Prepaid
        }
    """
    so = (
        sales_order
        if hasattr(sales_order, "grand_total")
        else frappe.get_doc("Sales Order", sales_order)
    )

    pe_refs = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Order",
            "reference_name": so.name,
            "docstatus": 1,
        },
        fields=["parent", "allocated_amount"],
    )

    advance_amount = flt(sum(p["allocated_amount"] for p in pe_refs))
    pe_names = [p["parent"] for p in pe_refs]

    if advance_amount <= 0:
        # Scenario III — pure credit terms. No PE at all.
        return {
            "paymentMode": 2,
            "paymentTransactionNumber": "",
            "collectableAmount": flt(so.grand_total),
            "shippingMethod": 1,
        }

    # Pick the last-submitted PE as the source for the transaction
    # number. Phase 1 doesn't try to enumerate multi-PE transaction
    # references; the most recent one is good enough for EE's audit.
    pe = frappe.get_doc("Payment Entry", pe_names[-1])
    transaction_number = pe.reference_no or pe.name

    if advance_amount >= flt(so.grand_total):
        # Scenario I — full prepaid.
        return {
            "paymentMode": 5,
            "paymentTransactionNumber": transaction_number,
            "collectableAmount": 0.0,
            "shippingMethod": 3,
        }

    # Scenario II — partial prepaid → bucketed as COD with the
    # remainder in collectableAmount.
    return {
        "paymentMode": 2,
        "paymentTransactionNumber": transaction_number,
        "collectableAmount": flt(so.grand_total) - advance_amount,
        "shippingMethod": 1,
    }
