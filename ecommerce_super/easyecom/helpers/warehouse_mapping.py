"""Warehouse → EasyEcom Location / Account resolution helpers (§8a).

§11 build needs to resolve from `SO.set_warehouse` to the EE Account
that should receive the createOrder push. §10 has a private function
(`_location_key_for_warehouse` in transfer_push.py) doing the same
Warehouse → Location lookup but returning the location_key string;
keeping that one stable for §10 callers. This module exposes the
shared logic in a §11/future-friendly shape: returns the full
Document so callers can read `easyecom_account`, location_key,
or any other field they need without a second query.

Filter is identical to §10's: workflow_state="Live" AND enabled=1.
Mapped-but-not-Live Locations are intentionally invisible to flows
— they're set up by the FDE but haven't been Go-Lived yet.
"""

from __future__ import annotations

from typing import Any

import frappe


def get_ee_location_for_warehouse(warehouse: str) -> Any | None:
    """Return the Live + enabled EasyEcom Location whose mapped_warehouse
    is this ERPNext Warehouse, or None if no such Location exists.

    Returns the full Document so callers can inspect location_key,
    easyecom_account, ee_company_id, etc. without an extra fetch.
    """
    if not warehouse:
        return None
    name = frappe.db.get_value(
        "EasyEcom Location",
        {
            "mapped_warehouse": warehouse,
            "workflow_state": "Live",
            "enabled": 1,
        },
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("EasyEcom Location", name)


def get_ee_account_for_warehouse(warehouse: str) -> Any | None:
    """Return the EasyEcom Account that owns the Live Location mapped
    to this Warehouse, or None if the Warehouse is not EE-mapped or
    its Location row doesn't point at an Account.
    """
    location = get_ee_location_for_warehouse(warehouse)
    if not location:
        return None
    account_name = getattr(location, "easyecom_account", None)
    if not account_name:
        return None
    return frappe.get_doc("EasyEcom Account", account_name)
