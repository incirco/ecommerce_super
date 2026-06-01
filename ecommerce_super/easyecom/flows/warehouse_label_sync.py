"""Sync the `ecs_ee_location_label` field on Warehouse from EasyEcom
Location state.

Triggered by:
  - EasyEcom Location after_save  (mapping create / update / state change)
  - EasyEcom Location on_trash    (mapping removed)

Label format: "EE: {location_name} (#{location_key})" when the EE
Location is workflow_state=Live AND enabled=1. Empty string otherwise
— Mapped-but-not-Live and Skipped locations do not advertise as
EE-mapped, matching transfer_push._is_ee_mapped_warehouse's gate.

When a Location's mapped_warehouse changes (FDE re-points it), the
PRIOR warehouse also needs its label cleared. Same applies on
workflow transitions Live → not-Live and on trash.
"""

from __future__ import annotations

from typing import Any

import frappe

LABEL_FIELDNAME: str = "ecs_ee_location_label"
LIVE_STATE: str = "Live"


def _label_for_location(loc: Any) -> str:
    """Return the label this Location would advertise. Empty when the
    Location is not Live + enabled (the same gate used by
    transfer_push._is_ee_mapped_warehouse — keep them in lockstep)."""
    if not loc:
        return ""
    if (loc.workflow_state or "") != LIVE_STATE:
        return ""
    if not int(loc.enabled or 0):
        return ""
    name = (loc.location_name or "").strip()
    key = (loc.location_key or "").strip()
    if name and key:
        return f"EE: {name} (#{key})"
    return f"EE: {name or key}".strip()


def _recompute_label_for_warehouse(warehouse: str) -> str:
    """Re-derive the label from scratch by scanning all Live+enabled
    Locations pointing at this Warehouse. Multiple Locations can map
    to the same Warehouse in principle; we pick the first match
    deterministically (by name) and concatenate keys if more than one.

    Returning the recomputed value lets the caller set it without
    re-reading.
    """
    if not warehouse:
        return ""
    rows = frappe.db.get_all(
        "EasyEcom Location",
        filters={
            "mapped_warehouse": warehouse,
            "workflow_state": LIVE_STATE,
            "enabled": 1,
        },
        fields=["location_name", "location_key"],
        order_by="name asc",
    )
    if not rows:
        return ""
    if len(rows) == 1:
        r = rows[0]
        nm = (r.get("location_name") or "").strip()
        key = (r.get("location_key") or "").strip()
        if nm and key:
            return f"EE: {nm} (#{key})"
        return f"EE: {nm or key}".strip()
    # Multi-Location-per-Warehouse: rare but legal. Show the first
    # name and the count so the FDE knows there's >1.
    first = rows[0]
    return (
        f"EE: {(first.get('location_name') or first.get('location_key') or '').strip()} "
        f"(+{len(rows) - 1} more)"
    )


def _set_warehouse_label(warehouse: str, label: str) -> None:
    """Write the label via db.set_value to bypass Warehouse.validate
    (the field is read-only and computed; no need to round-trip the
    full controller)."""
    if not warehouse:
        return
    current = frappe.db.get_value("Warehouse", warehouse, LABEL_FIELDNAME)
    if (current or "") == (label or ""):
        return
    frappe.db.set_value(
        "Warehouse", warehouse, LABEL_FIELDNAME, label or "",
        update_modified=False,
    )


# ============================================================
# Hook entrypoints — wired in hooks.py doc_events.
# ============================================================


def sync_on_location_save(doc: Any, method: str | None = None) -> None:
    """Run after the Location is saved. Refresh the CURRENT
    mapped_warehouse, and (if the mapping moved) also the PRIOR
    one — otherwise the old Warehouse keeps a stale label."""
    current_wh = doc.get("mapped_warehouse")
    prior = doc.get_doc_before_save()
    prior_wh = prior.get("mapped_warehouse") if prior else None

    # Touch the prior warehouse first (so a clear lands before the
    # new one's write — keeps any concurrent read sane).
    if prior_wh and prior_wh != current_wh:
        _set_warehouse_label(
            prior_wh, _recompute_label_for_warehouse(prior_wh)
        )

    if current_wh:
        _set_warehouse_label(
            current_wh, _recompute_label_for_warehouse(current_wh)
        )


def sync_on_location_trash(doc: Any, method: str | None = None) -> None:
    """Run after the Location is trashed. The just-removed mapping
    must trigger a recompute on the Warehouse — there may still be
    OTHER Locations pointing at it."""
    wh = doc.get("mapped_warehouse")
    if not wh:
        return
    # At this point the Location is being deleted; recompute now
    # finds the survivors (or empty if this was the last one).
    _set_warehouse_label(wh, _recompute_label_for_warehouse(wh))


# ============================================================
# Backfill — called by the one-shot patch.
# ============================================================


def backfill_all() -> dict[str, int]:
    """Walk every Warehouse and recompute its label. Idempotent.
    Returns a summary count for the patch log."""
    warehouses = frappe.db.get_all(
        "Warehouse", pluck="name", filters={"disabled": 0}
    )
    updated = 0
    for wh in warehouses:
        label = _recompute_label_for_warehouse(wh)
        before = frappe.db.get_value("Warehouse", wh, LABEL_FIELDNAME)
        if (before or "") != (label or ""):
            frappe.db.set_value(
                "Warehouse", wh, LABEL_FIELDNAME, label or "",
                update_modified=False,
            )
            updated += 1
    frappe.db.commit()
    return {"warehouses_scanned": len(warehouses), "labels_updated": updated}
