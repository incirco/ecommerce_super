"""gh#153 — add ecs_correlation_id column to EasyEcom B2B Order Map.

Adds the field that stores the cross-boundary correlation ID stamped
on outbound createOrder push. Sent as X-ECS-Correlation-Id HTTP header
so EE can echo it back on inbound /einvoice/update, linking the 3
legs of the flow.

Idempotent — no-op if column already exists.
"""
from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.has_column(
        "EasyEcom B2B Order Map", "ecs_correlation_id"
    ):
        frappe.reload_doctype("EasyEcom B2B Order Map", force=True)
