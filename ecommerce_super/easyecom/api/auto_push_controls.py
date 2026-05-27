"""Controlled go-live + kill-switch for the three auto-push toggles.

The three `auto_push_*_on_save` checkboxes on EasyEcom Account ship
defaulted OFF for safety: a fresh deployment with credentials wired
+ no toggle ceremony would silently push every existing Item /
Customer / Supplier the first time someone saved one. That's the
right default for onboarding (FDE manually triggers Discover +
verifies), but the wrong default for steady state (every doc-save
should propagate to EE).

This module provides the FDE-facing single-action transition between
those two worlds:

  - go_live_enable_auto_push(account, items, customers, suppliers)
    Sets the specified toggle(s) to 1. Confirms each entity is in
    erpnext_mastered mode first (the master-mode flip is the
    prerequisite — pushing while still in onboarding would race the
    pull's accept-and-create logic). Records an audit Comment on
    the Account doc with the user + timestamp + which entities.

  - pause_all_auto_push(account, reason)
    Emergency kill-switch — sets all three toggles to 0 in one
    transaction. No master-mode preconditions (you might pause
    PRECISELY because you want to roll back to manual sync mid-
    incident). Records the reason in the audit Comment so
    post-incident review can see why.

Both are role-gated to FDE / System Manager / EasyEcom System
Manager — Operator cannot toggle. Both never raise through the
whitelist boundary; they return structured {ok, message, state}
dicts so the JS handler renders cleanly.
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


@frappe.whitelist()
def go_live_enable_auto_push(
    account: str,
    items: int | bool = True,
    customers: int | bool = True,
    suppliers: int | bool = True,
    confirm: int | bool | str = False,
) -> dict[str, Any]:
    """Enable the auto-push toggles for the chosen entities in a single
    audit-logged action. Refuses cleanly if confirm is falsy.

    `items` / `customers` / `suppliers` default to truthy so the
    common path (enable all three) is the no-args invocation. Pass
    `items=0, customers=1, suppliers=0` to enable selectively (e.g.
    you've flipped Customer master to erpnext_mastered but Item is
    still onboarding).

    Returns:
      {
        "ok": True/False,
        "message": "...",
        "state": {"items": 1, "customers": 1, "suppliers": 1},
        "warnings": [...]  # only populated when steady-state checks
                           #   surfaced something the FDE should know.
      }
    """
    _check_role("Enable Auto-Push (Go Live)")

    if not account or not frappe.db.exists("EasyEcom Account", account):
        return {
            "ok": False,
            "message": f"Account {account!r} not found.",
        }

    if not _truthy(confirm):
        return {
            "ok": False,
            "message": (
                "Confirmation required — pass confirm=true to enable "
                "auto-push. This is a steady-state transition that "
                "should follow a deliberate ceremony, not an accidental "
                "click."
            ),
        }

    enable_items = _truthy(items)
    enable_customers = _truthy(customers)
    enable_suppliers = _truthy(suppliers)

    if not any((enable_items, enable_customers, enable_suppliers)):
        return {
            "ok": False,
            "message": (
                "Nothing to enable — items / customers / suppliers all "
                "falsy. Either pick one or call pause_all_auto_push if "
                "you meant to turn things OFF."
            ),
        }

    # Read master-mode state so we can warn (NOT block) when an
    # entity is being enabled while still in onboarding. The actual
    # push hook still gates per-doctype on the master_mode anyway —
    # this warning just surfaces what the FDE should already know
    # before confirming.
    modes = frappe.db.get_value(
        "EasyEcom Account",
        account,
        [
            "item_master_mode",
            "customer_master_mode",
            "supplier_master_mode",
        ],
        as_dict=True,
    )
    warnings: list[str] = []
    if enable_items and (modes.item_master_mode or "onboarding") != "erpnext_mastered":
        warnings.append(
            "Item master is still in onboarding mode — auto-push will "
            "race the §8d pull's accept-and-create logic. Flip Items "
            "to erpnext_mastered first if onboarding is complete."
        )
    if enable_customers and (modes.customer_master_mode or "onboarding") != "erpnext_mastered":
        warnings.append(
            "Customer master is still in onboarding mode — same caveat "
            "as Items. Flip Customers to erpnext_mastered first."
        )
    if enable_suppliers and (modes.supplier_master_mode or "onboarding") != "erpnext_mastered":
        warnings.append(
            "Supplier master is still in onboarding mode — same caveat "
            "as Items. Flip Suppliers to erpnext_mastered first."
        )

    updates: dict[str, Any] = {}
    transitioned: list[str] = []
    if enable_items:
        updates["auto_push_on_save"] = 1
        transitioned.append("Items")
    if enable_customers:
        updates["auto_push_customers_on_save"] = 1
        transitioned.append("Customers")
    if enable_suppliers:
        updates["auto_push_suppliers_on_save"] = 1
        transitioned.append("Suppliers")

    frappe.db.set_value(
        "EasyEcom Account", account, updates, update_modified=True
    )

    doc = frappe.get_doc("EasyEcom Account", account)
    doc.add_comment(
        comment_type="Info",
        text=(
            "<b>Go Live — Auto-Push enabled</b> by "
            f"<code>{frappe.session.user}</code> for: "
            f"{', '.join(transitioned)}.<br>"
            f"Master-mode at transition: "
            f"item={modes.item_master_mode or 'onboarding'}, "
            f"customer={modes.customer_master_mode or 'onboarding'}, "
            f"supplier={modes.supplier_master_mode or 'onboarding'}.<br>"
            "<i>Every ERPNext-side save on the enabled entities will "
            "now enqueue an EE push (via the on_update hook). To pause, "
            "use the Pause All Auto-Push button.</i>"
        ),
    )
    frappe.db.commit()

    new_state = frappe.db.get_value(
        "EasyEcom Account",
        account,
        [
            "auto_push_on_save",
            "auto_push_customers_on_save",
            "auto_push_suppliers_on_save",
        ],
        as_dict=True,
    )

    return {
        "ok": True,
        "account": account,
        "transitioned": transitioned,
        "state": {
            "items": int(new_state.auto_push_on_save or 0),
            "customers": int(new_state.auto_push_customers_on_save or 0),
            "suppliers": int(new_state.auto_push_suppliers_on_save or 0),
        },
        "warnings": warnings,
        "message": (
            f"Auto-push enabled for {', '.join(transitioned)}. "
            f"Recorded as a Comment on the Account doc for audit."
        ),
    }


@frappe.whitelist()
def pause_all_auto_push(
    account: str, reason: str | None = None, confirm: int | bool | str = False
) -> dict[str, Any]:
    """Emergency kill-switch — disable ALL three auto-push toggles in
    a single transaction. Use during incidents (EE rate-limit storm,
    a bad ERPNext config silently corrupting EE-side data, etc.) to
    stop the auto-push hook from firing.

    Idempotent — calling on an already-paused account is a no-op
    that still records the attempt in the Comment trail.

    `reason` is captured in the Comment so post-incident review can
    see why the pause was triggered. Optional but strongly
    encouraged — the JS handler prompts for one.
    """
    _check_role("Pause Auto-Push")

    if not account or not frappe.db.exists("EasyEcom Account", account):
        return {
            "ok": False,
            "message": f"Account {account!r} not found.",
        }

    if not _truthy(confirm):
        return {
            "ok": False,
            "message": (
                "Confirmation required — pass confirm=true to pause "
                "auto-push."
            ),
        }

    prior = frappe.db.get_value(
        "EasyEcom Account",
        account,
        [
            "auto_push_on_save",
            "auto_push_customers_on_save",
            "auto_push_suppliers_on_save",
        ],
        as_dict=True,
    )
    was_active = [
        label
        for label, val in (
            ("Items", prior.auto_push_on_save),
            ("Customers", prior.auto_push_customers_on_save),
            ("Suppliers", prior.auto_push_suppliers_on_save),
        )
        if int(val or 0) == 1
    ]

    frappe.db.set_value(
        "EasyEcom Account",
        account,
        {
            "auto_push_on_save": 0,
            "auto_push_customers_on_save": 0,
            "auto_push_suppliers_on_save": 0,
        },
        update_modified=True,
    )

    doc = frappe.get_doc("EasyEcom Account", account)
    reason_clean = (reason or "").strip() or "(no reason recorded)"
    doc.add_comment(
        comment_type="Info",
        text=(
            "<b>Pause All Auto-Push</b> triggered by "
            f"<code>{frappe.session.user}</code>.<br>"
            f"Was active for: <code>{', '.join(was_active) or '(none — already paused)'}</code>.<br>"
            f"Reason: <code>{frappe.utils.escape_html(reason_clean)}</code><br>"
            "<i>All three auto-push hooks now skip. Manual pushes via "
            "the FDE buttons still work. Re-enable via Go Live action.</i>"
        ),
    )
    frappe.db.commit()

    return {
        "ok": True,
        "account": account,
        "was_active": was_active,
        "state": {"items": 0, "customers": 0, "suppliers": 0},
        "message": (
            "All auto-push toggles disabled."
            + (
                f" Previously active: {', '.join(was_active)}."
                if was_active
                else " (Already paused; recorded the attempt.)"
            )
        ),
    }


def _truthy(value: Any) -> bool:
    """JS sends string for truthy args; coerce."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False
