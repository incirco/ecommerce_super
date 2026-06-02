"""Backfill Warehouse Addresses from every Live + enabled EasyEcom
Location. Must run AFTER add_address_ee_location_field. Idempotent.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    from ecommerce_super.easyecom.flows.warehouse_address_sync import (
        backfill_all,
    )

    summary = backfill_all()
    frappe.logger().info(
        f"[ecs] warehouse Address backfill from Locations: {summary}"
    )
