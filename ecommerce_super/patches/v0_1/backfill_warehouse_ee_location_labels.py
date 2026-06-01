"""Backfill `ecs_ee_location_label` on every existing Warehouse.

Must run AFTER add_warehouse_ee_location_label (which creates the
field). Idempotent — re-running is a no-op when labels are already
in sync.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    from ecommerce_super.easyecom.flows.warehouse_label_sync import (
        backfill_all,
    )

    summary = backfill_all()
    frappe.logger().info(
        f"[ecs] warehouse EE-label backfill: {summary}"
    )
