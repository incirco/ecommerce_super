"""gh#267 — Corrupt Submitted Sales Invoices report.

Finds Sales Invoices with `docstatus=1` and `update_stock=1` whose
Stock Ledger Entry count is less than the invoice's stock-item line
count, OR whose GL Entry count is zero.

This is the fingerprint of the gh#267 half-submitted state:
  gsp_handler.mint_irn_for_si caught a failed si.submit() and re-raised
  without frappe.db.rollback(); Frappe committed the partial state at
  request teardown → SI stuck as docstatus=1 with partial stock and/or
  missing GL entries.

Read-only. Same query works on any ERPNext site.

Since PR #290 landed, new occurrences should stop. This report is for:
  - baseline scan (before deploy): freeze the pre-existing count
  - delta scan (after deploy): confirm no NEW rows appear
  - historical audit: identify all such SIs across the site's history
"""
from __future__ import annotations

import frappe
from frappe import _


BUCKET_ZERO_STOCK = "ZERO_STOCK"
BUCKET_PARTIAL = "PARTIAL"
BUCKET_ZERO_GL = "ZERO_GL"


def execute(filters: dict | None = None):
    filters = filters or {}
    columns = _get_columns()
    data = _get_data(filters)
    summary = _get_summary(data)
    return columns, data, None, None, summary


def _get_columns() -> list[dict]:
    return [
        {"label": _("Sales Invoice"), "fieldname": "name",
         "fieldtype": "Link", "options": "Sales Invoice", "width": 150},
        {"label": _("Posting Date"), "fieldname": "posting_date",
         "fieldtype": "Date", "width": 100},
        {"label": _("Bucket"), "fieldname": "bucket",
         "fieldtype": "Data", "width": 110},
        {"label": _("Grand Total"), "fieldname": "grand_total",
         "fieldtype": "Currency", "width": 130},
        {"label": _("Customer"), "fieldname": "customer",
         "fieldtype": "Link", "options": "Customer", "width": 200},
        {"label": _("Warehouse"), "fieldname": "set_warehouse",
         "fieldtype": "Link", "options": "Warehouse", "width": 180},
        {"label": _("Expected SLE"), "fieldname": "expected_sle",
         "fieldtype": "Int", "width": 100},
        {"label": _("Actual SLE"), "fieldname": "actual_sle",
         "fieldtype": "Int", "width": 100},
        {"label": _("SLE Gap"), "fieldname": "sle_gap",
         "fieldtype": "Int", "width": 90},
        {"label": _("GL Entries"), "fieldname": "gl_count",
         "fieldtype": "Int", "width": 100},
        {"label": _("Suggested Action"), "fieldname": "suggested_action",
         "fieldtype": "Small Text", "width": 400},
    ]


def _get_data(filters: dict) -> list[dict]:
    where = ["si.docstatus = 1", "si.update_stock = 1"]
    args: dict = {}

    if filters.get("company"):
        where.append("si.company = %(company)s")
        args["company"] = filters["company"]
    if filters.get("from_date"):
        where.append("si.posting_date >= %(from_date)s")
        args["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        where.append("si.posting_date <= %(to_date)s")
        args["to_date"] = filters["to_date"]

    rows = frappe.db.sql(
        f"""
        SELECT
            si.name,
            si.posting_date,
            si.company,
            si.customer,
            si.grand_total,
            si.currency,
            si.set_warehouse,
            (
                SELECT COUNT(*) FROM `tabSales Invoice Item` sii
                JOIN `tabItem` it ON it.name = sii.item_code
                WHERE sii.parent = si.name AND it.is_stock_item = 1
            ) AS expected_sle,
            (
                SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
                WHERE sle.voucher_type = 'Sales Invoice'
                  AND sle.voucher_no = si.name
                  AND sle.is_cancelled = 0
            ) AS actual_sle,
            (
                SELECT COUNT(*) FROM `tabGL Entry` gle
                WHERE gle.voucher_type = 'Sales Invoice'
                  AND gle.voucher_no = si.name
                  AND gle.is_cancelled = 0
            ) AS gl_count
        FROM `tabSales Invoice` si
        WHERE {' AND '.join(where)}
        ORDER BY si.posting_date DESC, si.name DESC
        """,
        args, as_dict=True,
    )

    bucket_filter = filters.get("bucket")
    out = []
    for r in rows:
        expected = int(r["expected_sle"] or 0)
        actual = int(r["actual_sle"] or 0)
        gl = int(r["gl_count"] or 0)
        # Service-only SIs (no stock items) — skip unless GL is also zero
        if expected == 0 and gl > 0:
            continue

        if gl == 0 and actual == 0 and expected > 0:
            r["bucket"] = BUCKET_ZERO_STOCK
            r["suggested_action"] = _(
                "Fully corrupt: no GL, no SLE — full billing booked on "
                "the SI row but nothing posted to books. Cancel and "
                "investigate before restocking."
            )
        elif gl == 0 and actual > 0:
            r["bucket"] = BUCKET_ZERO_GL
            r["suggested_action"] = _(
                f"gh#267 half-submit: {actual} of {expected} stock lines "
                "posted, but no GL entries. Stock reduced without revenue "
                "booked. Cancel SI (reverses posted SLEs) + investigate."
            )
        elif actual < expected:
            r["bucket"] = BUCKET_PARTIAL
            r["suggested_action"] = _(
                f"gh#267 half-submit: {actual} of {expected} stock lines "
                "posted (GL present). Either: (a) Stock Reconciliation to "
                "match SI billing, keep SI as-is; (b) Cancel SI (reverses "
                "GL + posted SLEs), restock warehouse, re-submit."
            )
        else:
            # actual >= expected and gl > 0 → normal (batches/serials
            # can inflate SLE count; safe)
            continue

        r["sle_gap"] = expected - actual
        if bucket_filter and r["bucket"] != bucket_filter:
            continue
        out.append(r)

    return out


def _get_summary(data: list[dict]) -> list[dict]:
    from collections import Counter
    buckets = Counter(r["bucket"] for r in data)
    total_value = sum(r.get("grand_total") or 0 for r in data)
    return [
        {"label": _("Total corrupt SIs"), "value": len(data),
         "datatype": "Int"},
        {"label": _("ZERO_STOCK (no GL, no SLE)"),
         "value": buckets.get(BUCKET_ZERO_STOCK, 0), "datatype": "Int",
         "indicator": "red"},
        {"label": _("ZERO_GL (SLE posted, no GL)"),
         "value": buckets.get(BUCKET_ZERO_GL, 0), "datatype": "Int",
         "indicator": "red"},
        {"label": _("PARTIAL (some SLE missing)"),
         "value": buckets.get(BUCKET_PARTIAL, 0), "datatype": "Int",
         "indicator": "orange"},
        {"label": _("Grand Total value at risk"), "value": total_value,
         "datatype": "Currency"},
    ]
