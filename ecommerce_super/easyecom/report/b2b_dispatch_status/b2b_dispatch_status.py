"""§11.6 — B2B Dispatch Status report.

Operations dashboard for the §11.6 dispatch fields on Sales Invoice.
Shows every B2B-linked SI (identified by ecs_easyecom_invoice_id) with
its current EE-side fulfilment state, dispatch timestamps, and an
age-in-days computed from posting_date.

Filters: Company (mandatory), Date range (posting_date), Dispatch
Status (multi-select).

Default sort: oldest pending first — so ops sees stuck orders at the top.
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime


def execute(filters: dict | None = None) -> tuple[list[dict], list[dict]]:
    filters = filters or {}
    columns = _get_columns()
    data = _get_data(filters)
    return columns, data


def _get_columns() -> list[dict]:
    return [
        {
            "label": _("Sales Invoice"),
            "fieldname": "sales_invoice",
            "fieldtype": "Link",
            "options": "Sales Invoice",
            "width": 160,
        },
        {
            "label": _("Customer"),
            "fieldname": "customer",
            "fieldtype": "Link",
            "options": "Customer",
            "width": 200,
        },
        {
            "label": _("Posting Date"),
            "fieldname": "posting_date",
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "label": _("Dispatch Status"),
            "fieldname": "dispatch_status",
            "fieldtype": "Data",
            "width": 110,
        },
        {
            "label": _("Dispatched At"),
            "fieldname": "dispatched_at",
            "fieldtype": "Datetime",
            "width": 150,
        },
        {
            "label": _("Delivered At"),
            "fieldname": "delivered_at",
            "fieldtype": "Datetime",
            "width": 150,
        },
        {
            "label": _("Age (days)"),
            "fieldname": "age_days",
            "fieldtype": "Int",
            "width": 90,
        },
        {
            "label": _("Tracking"),
            "fieldname": "tracking_url",
            "fieldtype": "Data",
            "width": 220,
        },
        {
            "label": _("EE Invoice ID"),
            "fieldname": "ee_invoice_id",
            "fieldtype": "Data",
            "width": 130,
        },
        {
            "label": _("Grand Total"),
            "fieldname": "grand_total",
            "fieldtype": "Currency",
            "width": 130,
        },
    ]


def _get_data(filters: dict) -> list[dict]:
    conditions = ["si.docstatus != 2", "si.ecs_easyecom_invoice_id IS NOT NULL"]
    params: dict[str, Any] = {}

    if filters.get("company"):
        conditions.append("si.company = %(company)s")
        params["company"] = filters["company"]

    if filters.get("from_date"):
        conditions.append("si.posting_date >= %(from_date)s")
        params["from_date"] = filters["from_date"]

    if filters.get("to_date"):
        conditions.append("si.posting_date <= %(to_date)s")
        params["to_date"] = filters["to_date"]

    if filters.get("dispatch_status"):
        statuses = filters["dispatch_status"]
        if isinstance(statuses, str):
            statuses = [statuses]
        conditions.append("si.ecs_easyecom_dispatch_status IN %(statuses)s")
        params["statuses"] = tuple(statuses)

    where = " AND ".join(conditions)
    rows = frappe.db.sql(
        f"""
        SELECT
            si.name                              AS sales_invoice,
            si.customer                          AS customer,
            si.posting_date                      AS posting_date,
            COALESCE(si.ecs_easyecom_dispatch_status, '') AS dispatch_status,
            si.ecs_easyecom_dispatched_at        AS dispatched_at,
            si.ecs_easyecom_delivered_at         AS delivered_at,
            si.ecs_easyecom_tracking_url         AS tracking_url,
            si.ecs_easyecom_invoice_id           AS ee_invoice_id,
            si.grand_total                       AS grand_total
        FROM `tabSales Invoice` si
        WHERE {where}
        ORDER BY
            CASE COALESCE(si.ecs_easyecom_dispatch_status, '')
                WHEN ''          THEN 0
                WHEN 'Pending'   THEN 1
                WHEN 'Shipped'   THEN 2
                WHEN 'Delivered' THEN 3
                WHEN 'Returned'  THEN 4
                WHEN 'Cancelled' THEN 5
                ELSE 9
            END,
            si.posting_date ASC
        """,
        params,
        as_dict=True,
    )

    today = getdate(now_datetime())
    for r in rows:
        pd = r.get("posting_date")
        r["age_days"] = (today - getdate(pd)).days if pd else None

    return rows
