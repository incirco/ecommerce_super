"""gh#141 — surface intended-for-B2B SOs that Gate 0 silently rejects,
and half-mapped Warehouses that will cause the same silent-inert
downstream.

Both hooks are non-throwing observability layers. Genuine non-EE
Warehouses / SOs are untouched (§11's deliberate silent-inert path
preserved). What we catch is the AMBIGUOUS case: intent signals point
at EE but a setup gap prevents the flow.

## Resolver-key correctness (gh#162 rewrite, 2026-07-11)

§11 Gate 0 (`is_section_11_gated` → `get_ee_location_for_warehouse`)
resolves the warehouse ↔ EasyEcom Location link by the REVERSE query:

    EasyEcom Location where mapped_warehouse = <this warehouse>
                       AND workflow_state = "Live"
                       AND enabled = 1

The initial gh#141 version read `Warehouse.ecs_ee_location` as the
"is this EE-mapped?" signal, which produced (a) wrong diagnoses on
sites where the FK is empty but the reverse mapping IS wired (as on
mmpl16 "Fornt Back Factory - MMPL" ↔ ECS-LOC-en71352025924), and
(b) crashes when `ecs_ee_location` isn't materialised on the Warehouse
table. Rewrite reads the REAL resolver key.

Two hooks:

  1. Warehouse.validate → check_warehouse_half_mapping
     Only fires when EE Location's mapped_warehouse is UNSET / points
     elsewhere / is not Live — the states that actually break Gate 0.
     Non-blocking Comment on the Warehouse timeline.

  2. Sales Order.on_submit → detect_so_intent_gap
     Runs AFTER §11's push hook (which silently no-op's when Gate 0
     returns False). If the SO looks intended for B2B EE push AND
     Gate 0 rejected it → post a Comment on the SO timeline naming
     the specific gap.
"""
from __future__ import annotations

from typing import Any

import frappe


_INTENT_COMMENT_MARKER = "[gh#141 intent-detector]"
_WAREHOUSE_HALF_MAP_MARKER = "[gh#141 warehouse half-mapping]"


# ---------- Resolver helpers (mirror get_ee_location_for_warehouse) ----------


def _find_ee_location_for_warehouse(warehouse: str) -> dict | None:
    """Live + enabled EE Location whose mapped_warehouse == warehouse.

    Mirrors get_ee_location_for_warehouse in warehouse_mapping.py — the
    ACTUAL resolver §11 Gate 0 uses. Returns a minimal dict with the
    fields needed to explain Gate 0's state; None if no matching
    Location exists at all.
    """
    if not warehouse:
        return None
    try:
        loc_name = frappe.db.get_value(
            "EasyEcom Location",
            {"mapped_warehouse": warehouse},
            "name",
        )
        if not loc_name:
            return None
        loc = frappe.db.get_value(
            "EasyEcom Location",
            loc_name,
            ["name", "location_name", "workflow_state", "enabled"],
            as_dict=True,
        )
        return dict(loc) if loc else None
    except Exception:
        return None


# ---------- Warehouse.validate hook ----------


def check_warehouse_half_mapping(doc: Any, method: str | None = None) -> None:
    """Detect broken / partial EE-location wiring at Warehouse save time.

    Non-throwing. Only warns when signals of INTENT (either the legacy
    display-only `ecs_ee_location_label` field OR an EE Location that
    references this warehouse but is disabled / not-Live) combine with
    a state that would silently fail §11 Gate 0.
    """
    if doc.doctype != "Warehouse":
        return
    try:
        label = (getattr(doc, "ecs_ee_location_label", "") or "").strip()
        loc = _find_ee_location_for_warehouse(doc.name)
        # Case A — label set (intent) but no EE Location references
        # this warehouse at all → the whole reverse link is missing.
        if label and not loc:
            reason = (
                f"Warehouse {doc.name!r} has ecs_ee_location_label set "
                f"({label!r}) but NO EasyEcom Location has "
                f"mapped_warehouse={doc.name!r}. §11 Gate 0 will silently "
                "reject SOs on this warehouse. Fix: on the intended EE "
                "Location doc, set mapped_warehouse to this warehouse; "
                "or clear ecs_ee_location_label if this warehouse is "
                "not meant for EE."
            )
            _emit_warning(doc.name, "Warehouse", _WAREHOUSE_HALF_MAP_MARKER, reason)
            return
        # Case B — Location references the warehouse but is not-Live or
        # disabled → mapping intent exists but Gate 0 will still fail.
        if loc:
            if (loc.get("workflow_state") or "") != "Live":
                reason = (
                    f"EasyEcom Location {loc.get('name')!r} points at "
                    f"Warehouse {doc.name!r} but workflow_state is "
                    f"{loc.get('workflow_state')!r} (not 'Live'). "
                    "§11 Gate 0 requires Live. Take the Location live "
                    "to enable §11 pushes."
                )
                _emit_warning(doc.name, "Warehouse", _WAREHOUSE_HALF_MAP_MARKER, reason)
                return
            if not loc.get("enabled"):
                reason = (
                    f"EasyEcom Location {loc.get('name')!r} points at "
                    f"Warehouse {doc.name!r} but enabled=0. §11 Gate 0 "
                    "requires enabled=1."
                )
                _emit_warning(doc.name, "Warehouse", _WAREHOUSE_HALF_MAP_MARKER, reason)
                return
    except Exception:
        try:
            frappe.log_error(
                title=f"gh#141 warehouse detector failed for {doc.name}",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass


# ---------- Sales Order.on_submit hook (runs after §11 push) ----------


def detect_so_intent_gap(doc: Any, method: str | None = None) -> None:
    """Detect SOs that look B2B-intended but §11 Gate 0 silently
    rejected. Non-throwing. Adds a Comment on the SO timeline with
    the specific gap.

    Signals of B2B intent (any of):
      - Customer.customer_group contains "B2B"
      - Customer.ecs_ee_c_id populated
      - An EE Location whose mapped_warehouse is this SO's warehouse

    Gate 0 rejected iff: no B2B Order Map row was created by §11's
    on_submit_push hook (which fires BEFORE this one).
    """
    if doc.doctype != "Sales Order":
        return
    try:
        # If a B2B Order Map exists for this SO, §11 accepted — silent.
        if frappe.db.exists("EasyEcom B2B Order Map", {"sales_order": doc.name}):
            return
        signals = _b2b_intent_signals(doc)
        if not signals:
            return
        reason = _diagnose_gate0_failure(doc)
        body = (
            f"This SO looks intended for §11 B2B push (signals: "
            f"{', '.join(signals)}) but Gate 0 rejected it. Reason: "
            f"{reason} Fix the setup + re-submit or create a new SO "
            "to trigger the push."
        )
        _post_marker_comment_once(
            doctype="Sales Order",
            docname=doc.name,
            marker=_INTENT_COMMENT_MARKER,
            body=body,
        )
    except Exception:
        try:
            frappe.log_error(
                title=f"gh#141 detect_so_intent_gap failed for {doc.name}",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass


def _b2b_intent_signals(so: Any) -> list[str]:
    """Return the specific B2B-intent signals this SO carries.

    Uses the REAL resolver key (EE Location.mapped_warehouse) instead
    of the display-only Warehouse.ecs_ee_location field so the signal
    matches §11 Gate 0's semantics exactly.
    """
    signals: list[str] = []
    try:
        customer_group = frappe.db.get_value(
            "Customer", so.customer, "customer_group"
        ) or ""
        if "B2B" in customer_group:
            signals.append(f"Customer group {customer_group!r}")
    except Exception:
        pass
    try:
        ee_c_id = frappe.db.get_value(
            "Customer", so.customer, "ecs_ee_c_id"
        )
        if ee_c_id:
            signals.append(f"Customer.ecs_ee_c_id={ee_c_id!r}")
    except Exception:
        pass
    if so.set_warehouse:
        loc = _find_ee_location_for_warehouse(so.set_warehouse)
        if loc:
            signals.append(
                f"EE Location {loc.get('name')!r} maps to this warehouse"
            )
        else:
            try:
                label = frappe.db.get_value(
                    "Warehouse", so.set_warehouse, "ecs_ee_location_label"
                )
                if label:
                    signals.append(f"Warehouse.ecs_ee_location_label={label!r}")
            except Exception:
                pass
    return signals


def _diagnose_gate0_failure(so: Any) -> str:
    """Explain which specific piece of setup would have unblocked §11
    Gate 0 for this SO.

    Reads the REAL resolver key: EE Location where
    mapped_warehouse=so.set_warehouse.
    """
    if not so.set_warehouse:
        return "SO has no set_warehouse."
    loc = _find_ee_location_for_warehouse(so.set_warehouse)
    if not loc:
        try:
            label = frappe.db.get_value(
                "Warehouse", so.set_warehouse, "ecs_ee_location_label"
            ) or ""
        except Exception:
            label = ""
        return (
            f"No EasyEcom Location has mapped_warehouse={so.set_warehouse!r}. "
            + (
                f"(Warehouse label suggests intent: {label!r}.) "
                if label else ""
            )
            + "Fix on the EE Location doc, not on the Warehouse."
        )
    ws = loc.get("workflow_state") or ""
    if ws != "Live":
        return (
            f"EasyEcom Location {loc.get('name')!r} points at this "
            f"warehouse but workflow_state is {ws!r}, not 'Live'. "
            "Take the Location live."
        )
    if not loc.get("enabled"):
        return (
            f"EasyEcom Location {loc.get('name')!r} points at this "
            f"warehouse but enabled=0."
        )
    return (
        "Unknown — the EE Location is Live and enabled and points at "
        "this warehouse. Gate 0 SHOULD have passed. Investigate whether "
        "on_submit_push threw silently OR the Queue Job was orphaned "
        "(see gh#120 resweep)."
    )


# ---------- Shared helpers ----------


def _emit_warning(docname: str, doctype: str, marker: str, reason: str) -> None:
    """Save-time toast + timeline Comment. Idempotent via the marker."""
    try:
        frappe.msgprint(
            reason,
            title="EasyEcom setup gap",
            indicator="orange",
            alert=True,
        )
    except Exception:
        pass
    _post_marker_comment_once(
        doctype=doctype, docname=docname, marker=marker, body=reason
    )


def _post_marker_comment_once(
    *,
    doctype: str,
    docname: str,
    marker: str,
    body: str,
) -> None:
    """Post a Comment on doctype/docname timeline iff no prior Comment
    on that doc carries the same marker string. Keeps repeated saves
    from spamming the timeline."""
    try:
        content = f"{marker}\n\n{body}"
        prior = frappe.db.exists(
            "Comment",
            {
                "reference_doctype": doctype,
                "reference_name": docname,
                "content": ["like", f"%{marker}%"],
            },
        )
        if prior:
            return
        c = frappe.new_doc("Comment")
        c.update({
            "comment_type": "Comment",
            "reference_doctype": doctype,
            "reference_name": docname,
            "content": content,
        })
        c.insert(ignore_permissions=True)
    except Exception:
        pass
