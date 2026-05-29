"""FDE-facing resolution actions for unknown-PO GRN drift.

Corrective commit 2026-05-29 (FIX 1). The §9 Stage 3 GRN pull was
auto-submitting a PR even when no ERPNext-side PO resolved. The
locked contract (packet step 5 + Open Decision #4, both updated
2026-05-28) flips that: unknown-PO GRN is drift; the integration only
PULLS in this direction; resolution is FDE-driven ERPNext-side only.

Surface (whitelisted, FDE-facing):

  create_pr_from_grn(grn_map_name, purchase_order=None, confirm=False)
    Build a Purchase Receipt from the preserved GRN payload on a
    drifted GRN Map row. Standalone PR by default (no purchase_order
    link); optional PO link if the FDE supplies one that fits. Reuses
    the existing PR-build helpers (_build_pr_header + _append_pr_line)
    — no forked code path. On success: GRN Map status → Receipted,
    linked Integration Discrepancy → Resolved.

  dismiss_grn_drift(grn_map_name, reason, confirm=False)
    Close an unknown-PO drift the FDE deems noise / duplicate. No PR
    is created. GRN Map status → Dismissed, linked Integration
    Discrepancy → Dismissed.

Both:
  - Role-gated (FDE / System Manager / EasyEcom System Manager).
  - Refuse cleanly with structured dicts on validation failure.
  - Idempotent: re-invocation on a row already past drift state is
    a no-op with a descriptive message (not a hard error).
  - NEVER create or push a PO to EasyEcom. ERPNext-side only.
"""

from __future__ import annotations

from typing import Any

import frappe


_ROLES_ALLOWED = {
    "System Manager",
    "EasyEcom System Manager",
    "EasyEcom FDE",
}


def _check_role(action_label: str) -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(_ROLES_ALLOWED):
        frappe.throw(
            frappe._(
                "{0} requires EasyEcom FDE or System Manager."
            ).format(action_label),
            frappe.PermissionError,
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


@frappe.whitelist()
def create_pr_from_grn(
    grn_map_name: str,
    purchase_order: str | None = None,
    confirm: int | bool | str = False,
) -> dict[str, Any]:
    """FDE action — build a PR from a drifted GRN Map row.

    Returns:
      { ok, message, grn_map, purchase_receipt, discrepancy_status }
    """
    _check_role("Create PR from this GRN")

    if not confirm or not _truthy(confirm):
        return {
            "ok": False,
            "message": (
                "Confirmation required — pass confirm=true. This "
                "creates a Purchase Receipt against ERPNext (stock-"
                "affecting). Without an optional purchase_order link, "
                "the PR is STANDALONE — it will not update any PO's "
                "per_received counter."
            ),
        }

    if not grn_map_name or not frappe.db.exists("EasyEcom GRN Map", grn_map_name):
        return {
            "ok": False,
            "message": f"GRN Map {grn_map_name!r} not found.",
        }

    grn_map = frappe.get_doc("EasyEcom GRN Map", grn_map_name)

    # Idempotency: only act on the drift state.
    if grn_map.status == "Receipted" and grn_map.purchase_receipt:
        return {
            "ok": False,
            "message": (
                f"GRN Map {grn_map_name!r} is already Receipted "
                f"(PR {grn_map.purchase_receipt}). No action."
            ),
            "purchase_receipt": grn_map.purchase_receipt,
        }
    if grn_map.status == "Dismissed":
        return {
            "ok": False,
            "message": (
                f"GRN Map {grn_map_name!r} is Dismissed. Un-dismiss "
                "before creating a PR."
            ),
        }
    if grn_map.status != "Discrepancy" or grn_map.linked_po_map or grn_map.purchase_receipt:
        return {
            "ok": False,
            "message": (
                f"GRN Map {grn_map_name!r} is not in the unknown-PO "
                f"drift state (status={grn_map.status!r}, "
                f"purchase_receipt={grn_map.purchase_receipt!r}, "
                f"linked_po_map={grn_map.linked_po_map!r}). Create PR "
                "from this GRN is only valid for the drift state."
            ),
        }
    if not grn_map.ecs_grn_payload_json:
        return {
            "ok": False,
            "message": (
                f"GRN Map {grn_map_name!r} is in drift state but has "
                "no preserved GRN payload. Re-pull the GRN to "
                "repopulate, then try again."
            ),
        }

    # Optional PO link — validate it's submitted.
    if purchase_order:
        po_row = frappe.db.get_value(
            "Purchase Order",
            purchase_order,
            ["docstatus"],
            as_dict=True,
        )
        if not po_row:
            return {
                "ok": False,
                "message": f"Purchase Order {purchase_order!r} not found.",
            }
        if int(po_row.docstatus or 0) != 1:
            return {
                "ok": False,
                "message": (
                    f"Purchase Order {purchase_order!r} is not "
                    "submitted (docstatus must be 1)."
                ),
            }

    # Reconstruct grn_row and re-run resolution.
    from ecommerce_super.easyecom.flows.grn_pull import (
        _append_pr_line,
        _build_pr_header,
        _company_for_warehouse,
        _RejectedWarehouseMissingError,
        _resolve_for_receipt,
        _resolve_location_for_warehouse_c_id,
    )

    grn_row = frappe.parse_json(grn_map.ecs_grn_payload_json)
    inwarded_wh_c_id = int(grn_map.inwarded_warehouse_c_id or 0)
    location_row = _resolve_location_for_warehouse_c_id(inwarded_wh_c_id)
    if not location_row:
        return {
            "ok": False,
            "message": (
                f"Location for inwarded_warehouse_c_id="
                f"{inwarded_wh_c_id} no longer resolves. The Location "
                "may have been unmapped after the drift was raised. "
                "Re-link the Location before creating the PR."
            ),
        }
    company = _company_for_warehouse(location_row["mapped_warehouse"])
    resolution = _resolve_for_receipt(
        grn_row=grn_row,
        location_row=location_row,
        company=company,
    )

    # FDE-supplied PO overrides the drift state — the FDE explicitly
    # asserts this PO fits the GRN. Resolve its PO Map row too.
    if purchase_order:
        resolution["po_name"] = purchase_order
        resolution["po_unknown_reason"] = None
        resolution["po_map_name"] = frappe.db.get_value(
            "EasyEcom PO Map", {"purchase_order": purchase_order}, "name"
        )

    if resolution.get("supplier_missing"):
        return {
            "ok": False,
            "message": (
                "Cannot create PR — supplier resolution failed: "
                f"{resolution['error']}"
            ),
        }
    if resolution.get("line_failures"):
        return {
            "ok": False,
            "message": (
                "Cannot create PR — item resolution failed: "
                + " || ".join(resolution["line_failures"][:3])
            ),
        }

    pr_doc = _build_pr_header(
        resolution=resolution,
        grn_row=grn_row,
        location_row=location_row,
        company=company,
    )
    try:
        for line_payload, item_map_row in resolution["lines"]:
            _append_pr_line(
                pr_doc=pr_doc,
                line_payload=line_payload,
                item_map_row=item_map_row,
                resolution=resolution,
            )
    except _RejectedWarehouseMissingError as exc:
        return {
            "ok": False,
            "message": (
                f"Cannot create PR — {exc}"
            ),
        }
    try:
        pr_doc.insert(ignore_permissions=True)
        pr_doc.submit()
    except Exception as exc:
        return {
            "ok": False,
            "message": (
                f"PR submit failed: {type(exc).__name__}: {exc}"
            ),
        }

    # Update GRN Map row.
    frappe.db.set_value(
        "EasyEcom GRN Map",
        grn_map_name,
        {
            "status": "Receipted",
            "purchase_receipt": pr_doc.name,
            "linked_po_map": resolution.get("po_map_name"),
        },
        update_modified=True,
    )

    # Resolve the linked Integration Discrepancy(ies).
    disc_status = _resolve_drift_discrepancy(
        grn_map_name=grn_map_name,
        resolution_status="Resolved",
        note=(
            f"FDE created PR {pr_doc.name} from this GRN — "
            + (f"linked to PO {purchase_order}" if purchase_order
               else "standalone (no PO link)")
            + f". User: {frappe.session.user}."
        ),
    )

    grn_map_doc = frappe.get_doc("EasyEcom GRN Map", grn_map_name)
    grn_map_doc.add_comment(
        comment_type="Info",
        text=(
            f"<b>§9 Create PR from this GRN</b> by "
            f"<code>{frappe.session.user}</code>: "
            f"PR <b>{pr_doc.name}</b> "
            + (
                f"linked to PO <b>{purchase_order}</b>."
                if purchase_order
                else "as STANDALONE PR (no PO link)."
            )
        ),
    )
    frappe.db.commit()

    return {
        "ok": True,
        "message": (
            f"Created Purchase Receipt {pr_doc.name} from GRN Map "
            f"{grn_map_name}. "
            + (
                f"Linked to PO {purchase_order}."
                if purchase_order
                else "Standalone PR (no PO link)."
            )
        ),
        "grn_map": grn_map_name,
        "purchase_receipt": pr_doc.name,
        "discrepancy_status": disc_status,
    }


@frappe.whitelist()
def dismiss_grn_drift(
    grn_map_name: str,
    reason: str = "",
    confirm: int | bool | str = False,
) -> dict[str, Any]:
    """FDE action — close an unknown-PO drift the FDE deems
    not-to-be-received. No PR is created.

    Returns:
      { ok, message, grn_map, discrepancy_status }
    """
    _check_role("Dismiss GRN Drift")

    if not confirm or not _truthy(confirm):
        return {
            "ok": False,
            "message": (
                "Confirmation required — pass confirm=true. This "
                "closes the drift without creating a PR. The GRN Map "
                "row will move to Dismissed; the linked Integration "
                "Discrepancy will move to Dismissed."
            ),
        }

    if not grn_map_name or not frappe.db.exists("EasyEcom GRN Map", grn_map_name):
        return {
            "ok": False,
            "message": f"GRN Map {grn_map_name!r} not found.",
        }
    if not reason or not reason.strip():
        return {
            "ok": False,
            "message": (
                "Reason is required — dismiss leaves a note explaining "
                "why the GRN should not be received. Provide the "
                "rationale (e.g. 'duplicate of GRN 2115440 already "
                "receipted', 'EE-side noise — vendor cancelled')."
            ),
        }

    grn_map = frappe.get_doc("EasyEcom GRN Map", grn_map_name)

    # Idempotency: only act on the drift state.
    if grn_map.status == "Dismissed":
        return {
            "ok": False,
            "message": f"GRN Map {grn_map_name!r} is already Dismissed.",
        }
    if grn_map.status == "Receipted":
        return {
            "ok": False,
            "message": (
                f"GRN Map {grn_map_name!r} is Receipted (PR "
                f"{grn_map.purchase_receipt}). Cannot dismiss; "
                "cancel the PR instead if the receipt was wrong."
            ),
        }
    if grn_map.status != "Discrepancy" or grn_map.linked_po_map or grn_map.purchase_receipt:
        return {
            "ok": False,
            "message": (
                f"GRN Map {grn_map_name!r} is not in the unknown-PO "
                f"drift state (status={grn_map.status!r}). Dismiss is "
                "only valid for drift."
            ),
        }

    frappe.db.set_value(
        "EasyEcom GRN Map",
        grn_map_name,
        {"status": "Dismissed"},
        update_modified=True,
    )

    disc_status = _resolve_drift_discrepancy(
        grn_map_name=grn_map_name,
        resolution_status="Dismissed",
        note=(
            f"FDE dismissed: {reason.strip()}. "
            f"User: {frappe.session.user}."
        ),
    )

    grn_map_doc = frappe.get_doc("EasyEcom GRN Map", grn_map_name)
    grn_map_doc.add_comment(
        comment_type="Info",
        text=(
            f"<b>§9 GRN drift dismissed</b> by "
            f"<code>{frappe.session.user}</code>: "
            f"{frappe.utils.escape_html(reason.strip())[:500]}"
        ),
    )
    frappe.db.commit()

    return {
        "ok": True,
        "message": f"GRN Map {grn_map_name} dismissed.",
        "grn_map": grn_map_name,
        "discrepancy_status": disc_status,
    }


def _resolve_drift_discrepancy(
    *, grn_map_name: str, resolution_status: str, note: str
) -> str:
    """Flip the linked Integration Discrepancy (kind='GRN for unknown
    PO', reference=grn_map_name) to Resolved or Dismissed. Returns the
    final status of the Discrepancy (or 'none' if no row was found,
    which is non-fatal — the GRN Map state is still authoritative)."""
    disc_name = frappe.db.get_value(
        "EasyEcom Integration Discrepancy",
        {
            "kind": "GRN for unknown PO",
            "reference_doctype": "EasyEcom GRN Map",
            "reference_name": grn_map_name,
            "status": "Open",
        },
        "name",
    )
    if not disc_name:
        return "none"
    frappe.db.set_value(
        "EasyEcom Integration Discrepancy",
        disc_name,
        {
            "status": resolution_status,
            "resolution_note": note[:1000],
        },
        update_modified=True,
    )
    return resolution_status
