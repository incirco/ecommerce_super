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
    """Return the enabled EasyEcom Account when the Warehouse is
    EE-mapped, or None.

    Combines Gate 0 (Warehouse → Live EE Location) with the
    singleton-Account lookup. Per CLAUDE.md §3.1 / the foundation
    packet, there is exactly one enabled Account per deployment —
    the integration uses `frappe.db.get_value("EasyEcom Account",
    {"enabled": 1}, "name")` everywhere (§9 grn_pull, §10
    transfer_push, auth.py, queue/concurrency, etc.). EE Location
    does NOT carry a per-Location Account FK (that was a survey
    miscall during §11 Stage 2 build — corrected here).
    """
    location = get_ee_location_for_warehouse(warehouse)
    if not location:
        return None
    account_name = frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1}, "name"
    )
    if not account_name:
        return None
    return frappe.get_doc("EasyEcom Account", account_name)
