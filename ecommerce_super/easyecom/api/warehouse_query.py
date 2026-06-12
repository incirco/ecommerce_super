"""Warehouse autocomplete + §10 branch prediction — UX helpers.

Two whitelisted callables:

  - `warehouse_with_ee_label` — Frappe Link autocomplete query that
    returns Warehouses with their `ecs_ee_location_label` as the
    description column. EE-mapped warehouses sort first so the FDE
    sees them above non-EE candidates when picking a transfer source.

  - `predict_section10_branch` — given (source_warehouse,
    target_warehouse), returns the §10 branch the DN would route to
    if submitted now (STN / PO / B2B / Inert) so the form can warn
    the user before they commit. Mirrors the live decision logic in
    transfer_push.push_one_transfer.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import frappe


# gh#26: the `ecs_ee_location_label` Custom Field on Warehouse is shipped
# by `patches.v0_1.add_warehouse_ee_location_label`. On mmpl16 (and
# potentially other Frappe Cloud deploys) that patch silently no-op'd
# during a previous deploy — Patch Log records "executed" but the
# column doesn't exist, so any reference to it raises
#   MySQLdb.OperationalError: Unknown column 'ecs_ee_location_label'
# until the rescue patch (`add_warehouse_ee_location_label_inline`)
# runs and creates the column.
#
# Every database access in this module guards against the missing
# column via `_warehouse_has_label_column()` and a fallback that
# returns empty labels. The §10 branch prediction still works —
# `_is_ee_mapped_warehouse` reads `tabEasyEcom Location`, not the
# Warehouse label — so the FDE form continues to render even on a
# pre-patch site.


@lru_cache(maxsize=1)
def _warehouse_has_label_column() -> bool:
    """Return True iff `Warehouse.ecs_ee_location_label` exists in the
    site's schema. Memoised per process — the underlying schema is
    stable within a bench worker lifetime, and `bench restart` rebuilds
    the worker. After the rescue patch lands, the next worker pickup
    sees the column."""
    try:
        return bool(frappe.db.has_column("Warehouse", "ecs_ee_location_label"))
    except Exception:
        return False


# ============================================================
# Link autocomplete query
# ============================================================


@frappe.whitelist()
def warehouse_with_ee_label(
    doctype: str,
    txt: str,
    searchfield: str,
    start: int,
    page_len: int,
    filters: dict[str, Any] | None = None,
) -> list[tuple]:
    """Return [(name, ee_location_label)] for the Warehouse Link
    autocomplete. EE-mapped warehouses sort first.

    Frappe calls this with positional args; the signature must match
    standard query_link conventions (see frappe.desk.search). Filters
    are honored: `company`, `is_group`, `disabled` etc. pass-through.
    """
    conditions: list[str] = ["w.disabled = 0"]
    params: dict[str, Any] = {
        "txt": f"%{(txt or '').strip()}%",
        "start": int(start or 0),
        "page_len": int(page_len or 20),
    }

    # Honor caller-provided filters — these come from frm.set_query's
    # `filters` dict (e.g. {"company": "Smoke Test Co"}).
    if filters:
        if filters.get("company"):
            conditions.append("w.company = %(company)s")
            params["company"] = filters["company"]
        if "is_group" in filters:
            conditions.append("w.is_group = %(is_group)s")
            params["is_group"] = int(filters["is_group"])
        if filters.get("warehouse_type"):
            conditions.append("w.warehouse_type = %(warehouse_type)s")
            params["warehouse_type"] = filters["warehouse_type"]

    where = " AND ".join(conditions)

    # gh#26: graceful degrade when the label column is missing — return
    # the autocomplete with empty labels rather than 500'ing every
    # warehouse picker on the site.
    if not _warehouse_has_label_column():
        rows = frappe.db.sql(
            f"""
            SELECT
                w.name,
                '' AS ee_label
            FROM `tabWarehouse` w
            WHERE {where}
              AND (
                  w.name LIKE %(txt)s
                  OR w.warehouse_name LIKE %(txt)s
              )
            ORDER BY w.name
            LIMIT %(start)s, %(page_len)s
            """,
            params,
        )
        return rows

    rows = frappe.db.sql(
        f"""
        SELECT
            w.name,
            COALESCE(w.ecs_ee_location_label, '') AS ee_label
        FROM `tabWarehouse` w
        WHERE {where}
          AND (
              w.name LIKE %(txt)s
              OR w.warehouse_name LIKE %(txt)s
              OR COALESCE(w.ecs_ee_location_label, '') LIKE %(txt)s
          )
        ORDER BY
            CASE WHEN COALESCE(w.ecs_ee_location_label, '') = '' THEN 1
                 ELSE 0 END,
            w.name
        LIMIT %(start)s, %(page_len)s
        """,
        params,
    )
    return rows


# ============================================================
# Branch prediction (§10 decision matrix)
# ============================================================


@frappe.whitelist()
def predict_section10_branch(
    source_warehouse: str, target_warehouse: str
) -> dict[str, Any]:
    """Predict the §10 routing branch for a (source, target) pair.

    Mirrors the live decision in
    `transfer_push.push_one_transfer` so the DN form can show the
    consequence of warehouse choice before submit. Returns:

      {
        "source_ee_mapped": bool,
        "target_ee_mapped": bool,
        "source_label": str,    # ecs_ee_location_label
        "target_label": str,
        "branch": "STN" | "PO" | "B2B" | "Inert" | "Unknown",
        "explanation": str,
        "color": "green" | "blue" | "orange" | "gray" | "red",
      }
    """
    from ecommerce_super.easyecom.flows.transfer_push import (
        _is_ee_mapped_warehouse,
    )

    src = (source_warehouse or "").strip()
    tgt = (target_warehouse or "").strip()
    if not src or not tgt:
        return {
            "branch": "Unknown",
            "color": "gray",
            "explanation": "Pick both warehouses to see the §10 branch.",
        }

    src_ee = _is_ee_mapped_warehouse(src)
    tgt_ee = _is_ee_mapped_warehouse(tgt)
    # gh#26: skip the label lookup when the column doesn't exist yet.
    # `_is_ee_mapped_warehouse` reads `tabEasyEcom Location` (a separate
    # table), so branch resolution still works — only the description
    # strings are blanked.
    if _warehouse_has_label_column():
        src_label = (
            frappe.db.get_value("Warehouse", src, "ecs_ee_location_label") or ""
        )
        tgt_label = (
            frappe.db.get_value("Warehouse", tgt, "ecs_ee_location_label") or ""
        )
    else:
        src_label = ""
        tgt_label = ""

    if src_ee and tgt_ee:
        branch = "STN"
        color = "green"
        explanation = (
            "Both warehouses are EE-mapped → STN createOrder "
            "(orderType=stocktransferorder) will fire on DN submit."
        )
    elif src_ee and not tgt_ee:
        branch = "B2B"
        color = "blue"
        explanation = (
            "Source EE-mapped, target not → B2B createOrder "
            "(orderType=businessorder, customer=Internal Customer) "
            "will fire on DN submit. Stock leaves EE's books."
        )
    elif not src_ee and tgt_ee:
        branch = "PO"
        color = "blue"
        explanation = (
            "Target EE-mapped, source not → CreatePurchaseOrder "
            "(vendor=Internal Supplier) will fire on DN submit. "
            "Stock enters EE's books via the PO inbound path."
        )
    else:
        branch = "Inert"
        color = "gray"
        explanation = (
            "Neither warehouse is EE-mapped → no EE call; pure "
            "ERPNext stock movement. No Transfer Map row created."
        )

    return {
        "source_ee_mapped": src_ee,
        "target_ee_mapped": tgt_ee,
        "source_label": src_label,
        "target_label": tgt_label,
        "branch": branch,
        "color": color,
        "explanation": explanation,
    }
