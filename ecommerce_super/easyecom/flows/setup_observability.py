"""gh#141 — surface intended-for-B2B SOs that Gate 0 silently rejects,
and half-mapped Warehouses that will cause the same silent-inert
downstream.

Both hooks are non-throwing observability layers. Genuine non-EE
Warehouses / SOs are untouched (§11's deliberate silent-inert path
preserved). What we catch is the AMBIGUOUS case: intent signals point
at EE but a setup gap prevents the flow.

Two hooks:

  1. Warehouse.validate → check_warehouse_half_mapping
     If `ecs_ee_location_label` is populated but `ecs_ee_location` FK
     is empty → soft warning (Frappe warning message, non-blocking) +
     Comment on the Warehouse timeline. This is the "someone intended
     to wire this to EE but didn't finish" case that killed SO-2610380.

  2. Sales Order.on_submit → detect_so_intent_gap
     Runs AFTER §11's push hook (which silently no-op's when Gate 0
     returns False). If the SO looks intended for B2B EE push AND
     Gate 0 rejected it → post a Comment on the SO timeline with
     the specific reason. Non-throwing.
"""
from __future__ import annotations

from typing import Any

import frappe


_INTENT_COMMENT_MARKER = "[gh#141 intent-detector]"
_WAREHOUSE_HALF_MAP_MARKER = "[gh#141 warehouse half-mapping]"


# ---------- Warehouse.validate hook ----------


def check_warehouse_half_mapping(doc: Any, method: str | None = None) -> None:
    """Detect Warehouse.ecs_ee_location_label populated but
    Warehouse.ecs_ee_location FK empty — a broken half-mapping that
    will silently break §11 Gate 0.

    Non-throwing. Emits a Frappe warning message (visible in the desk
    save toast) and adds a Comment to the Warehouse timeline. Idempotent:
    a previous Comment with the same marker suppresses re-posting.
    """
    if doc.doctype != "Warehouse":
        return
    label = (getattr(doc, "ecs_ee_location_label", "") or "").strip()
    fk = (getattr(doc, "ecs_ee_location", "") or "").strip()
    if not label or fk:
        return
    # Half-mapping detected.
    reason = (
        f"Warehouse {doc.name!r} has ecs_ee_location_label set "
        f"({label!r}) but ecs_ee_location FK is empty. This is a "
        "half-configured EE mapping — §11 Gate 0 will silently reject "
        "SOs on this warehouse. Fix by setting ecs_ee_location to the "
        "matching EasyEcom Location doc, or clear ecs_ee_location_label "
        "if this warehouse is not meant for EE."
    )
    # Save-time toast (non-blocking).
    try:
        frappe.msgprint(
            reason,
            title="EasyEcom Location half-mapping",
            indicator="orange",
            alert=True,
        )
    except Exception:
        pass
    _post_marker_comment_once(
        doctype="Warehouse",
        docname=doc.name,
        marker=_WAREHOUSE_HALF_MAP_MARKER,
        body=reason,
    )


# ---------- Sales Order.on_submit hook (runs after §11 push) ----------


def detect_so_intent_gap(doc: Any, method: str | None = None) -> None:
    """Detect SOs that look B2B-intended but §11 Gate 0 silently
    rejected. Non-throwing. Adds a Comment on the SO timeline with
    the specific gap.

    Signals of B2B intent (any of):
      - Customer.customer_group contains "B2B"
      - Customer.ecs_ee_c_id populated
      - Warehouse has any populated ecs_ee_* field (indicating an
        attempted EE wire-up)

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
        # Diagnose the specific Gate 0 failure.
        reason = _diagnose_gate0_failure(doc)
        body = (
            f"This SO looks intended for §11 B2B push (signals: "
            f"{', '.join(signals)}) but Gate 0 rejected it. Reason: "
            f"{reason} Fix the setup + create a new SO to trigger the "
            "push."
        )
        _post_marker_comment_once(
            doctype="Sales Order",
            docname=doc.name,
            marker=_INTENT_COMMENT_MARKER,
            body=body,
        )
    except Exception:
        # Observability must never break the SO save.
        try:
            frappe.log_error(
                title=f"gh#141 detect_so_intent_gap failed for {doc.name}",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass


def _b2b_intent_signals(so: Any) -> list[str]:
    """Return the specific B2B-intent signals this SO carries."""
    signals: list[str] = []
    customer_group = frappe.db.get_value(
        "Customer", so.customer, "customer_group"
    ) or ""
    if "B2B" in customer_group:
        signals.append(f"Customer group '{customer_group}'")
    ee_c_id = frappe.db.get_value(
        "Customer", so.customer, "ecs_ee_c_id"
    )
    if ee_c_id:
        signals.append(f"Customer.ecs_ee_c_id={ee_c_id!r}")
    if so.set_warehouse:
        label = frappe.db.get_value(
            "Warehouse", so.set_warehouse, "ecs_ee_location_label"
        )
        fk = frappe.db.get_value(
            "Warehouse", so.set_warehouse, "ecs_ee_location"
        )
        if label or fk:
            signals.append(
                f"Warehouse.ecs_ee_location_label={label!r}"
                f" / ecs_ee_location={fk!r}"
            )
    return signals


def _diagnose_gate0_failure(so: Any) -> str:
    """Explain which specific piece of setup would have unblocked §11
    Gate 0 for this SO."""
    if not so.set_warehouse:
        return "SO has no set_warehouse."
    fk = frappe.db.get_value(
        "Warehouse", so.set_warehouse, "ecs_ee_location"
    )
    if not fk:
        return (
            f"Warehouse {so.set_warehouse!r} has no ecs_ee_location FK "
            "set. Wire it to the matching EasyEcom Location."
        )
    if not frappe.db.exists("EasyEcom Location", fk):
        return (
            f"Warehouse.ecs_ee_location={fk!r} references a "
            "non-existent EasyEcom Location doc."
        )
    workflow_state = frappe.db.get_value(
        "EasyEcom Location", fk, "workflow_state"
    )
    if workflow_state != "Live":
        return (
            f"EasyEcom Location {fk!r} is in workflow_state "
            f"{workflow_state!r}, not 'Live'. Take it live to enable §11."
        )
    return "Unknown — Gate 0 returned False but all obvious checks pass."


# ---------- Shared helper ----------


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
