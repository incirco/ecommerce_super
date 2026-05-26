"""Whitelisted endpoint for the §8.3 Supplier Master Mode flip.

Mirrors `customer_master_mode.flip_to_erpnext_mastered_customers`
exactly — same role-gating, same explicit-confirm contract, same
one-way semantics, same clean-refusal-on-already-flipped behaviour.
Independent of the Item / Customer flips: an account can flip
Supplier master without flipping Item or Customer (or vice versa) —
all three are separate switches on the same Account.

`onboarding` (default) is bidirectional — pull + push, supervised by
the FDE. `erpnext_mastered` is the steady state where ERPNext owns
the supplier master; the pull becomes drift-detection only. Once
flipped, EE-side edits to a mapped supplier show as Drift rather
than auto-overwriting ERPNext.

Reverse flips (back to onboarding) deliberately require manual
intervention (set the field via Console / DB).

Permission: EasyEcom FDE / System Manager / EasyEcom System Manager
can trigger the flip. Operator cannot.
"""

from __future__ import annotations

from typing import Any

import frappe


@frappe.whitelist()
def flip_to_erpnext_mastered_suppliers(
    account: str, confirm: str | bool = False
) -> dict[str, Any]:
    """Flip supplier_master_mode from onboarding → erpnext_mastered.

    Args:
        account: the EasyEcom Account docname.
        confirm: must be truthy — the JS dialog passes the user's
            confirmation through. A whitelisted method should never
            mutate a long-lived config flag without an explicit
            confirmation, even if the caller's role is privileged.

    Returns:
        {ok: bool, message: str, ...} — JS-friendly response.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Flipping Supplier Master Mode requires EasyEcom FDE or "
                "System Manager privilege."
            ),
            frappe.PermissionError,
        )

    if not frappe.has_permission("EasyEcom Account", "write", doc=account):
        frappe.throw(
            frappe._(
                "You don't have write permission on EasyEcom Account {0}."
            ).format(account),
            frappe.PermissionError,
        )

    if not confirm or str(confirm).lower() in ("false", "0", "no"):
        return {
            "ok": False,
            "message": frappe._(
                "Confirmation required — pass confirm=true to flip."
            ),
        }

    if not frappe.db.exists("EasyEcom Account", account):
        return {
            "ok": False,
            "message": frappe._("Account {0} not found.").format(account),
        }

    current_mode = frappe.db.get_value(
        "EasyEcom Account", account, "supplier_master_mode"
    )
    if current_mode == "erpnext_mastered":
        return {
            "ok": False,
            "message": frappe._(
                "Account {0} is already in erpnext_mastered mode "
                "(Supplier master)."
            ).format(account),
        }

    now = frappe.utils.now_datetime()
    frappe.db.set_value(
        "EasyEcom Account",
        account,
        {
            "supplier_master_mode": "erpnext_mastered",
            "supplier_master_flipped_at": now,
        },
        update_modified=True,
    )
    frappe.db.commit()
    return {
        "ok": True,
        "account": account,
        "mode": "erpnext_mastered",
        "flipped_at": str(now),
        "message": frappe._(
            "Account {0} Supplier master is now in erpnext_mastered mode. "
            "The §8.3 pull is now drift-detection only — EE-side new "
            "suppliers and edits to mapped suppliers will flag as Drift "
            "rather than create/overwrite."
        ).format(account),
    }
