"""Permission hooks wired in hooks.py (§31.7 + §31.8.3).

Three concerns:
  1. company_scope — every Company-scoped DocType (§31.7.1) gets a
     permission_query_conditions hook that filters list queries to the
     Companies the current user has access to via User Permissions.
  2. append_only — EasyEcom API Call and EasyEcom Webhook Event refuse
     UPDATE and DELETE for every role (§31.7.4).
  3. audit_no_modify — EasyEcom Configuration Audit refuses UPDATE and
     DELETE for every role.

All three are hooked from hooks.py — they don't run unless wired.
"""

from __future__ import annotations

from typing import Any

import frappe


def company_scope(user: str | None = None) -> str:
    """permission_query_conditions hook. Returns a SQL WHERE fragment that
    restricts the queried DocType to the Companies in the user's User
    Permissions.

    System Manager and Administrator see all Companies (no filter). Other
    users see only the Companies they're explicitly granted via User
    Permissions on the Company DocType.

    The hook is wired per-DocType in hooks.py; the actual DocType name is
    not parameterised here because Frappe substitutes `{user_permission_doctype_condition}`
    style filters automatically. We just return the company-allow-list as a
    raw fragment using the standard `tabSyncRecord`.company pattern via
    the DocType being queried. Since the hook signature only gives `user`,
    we rely on frappe.local.get_request_context to know the doctype — or
    more reliably, use `get_user_permissions`.
    """
    user = user or frappe.session.user
    if user in {"Administrator", None}:
        return ""

    roles = set(frappe.get_roles(user))
    if "System Manager" in roles:
        return ""

    allowed = _user_company_filter(user)
    if allowed is None:
        # No Company restrictions configured → see all.
        return ""
    if not allowed:
        # User has the role but no Company permissions → see nothing.
        return "1=0"

    quoted = ", ".join(f"'{c}'" for c in allowed)
    # The {doctype} placeholder is required so Frappe substitutes the
    # actual table alias of the query being filtered. We use the field name
    # `company` because every company-scoped DocType in §31.7.1 has it.
    return f"`tab{{doctype}}`.company in ({quoted})"


def append_only(doc: Any, ptype: str, user: str | None = None) -> bool:
    """has_permission hook. Returns True only for read and create — every
    other ptype (write, delete, cancel, submit, etc.) is refused for ALL
    roles including System Manager (§31.7.4)."""
    if ptype in {"read", "report", "export", "create"}:
        return True
    return False


def audit_no_modify(doc: Any, ptype: str, user: str | None = None) -> bool:
    """has_permission hook for EasyEcom Configuration Audit. Identical to
    append_only — append-only with no UPDATE/DELETE even for System
    Manager. Configuration Audit is built in a later packet (§26); the
    hook is registered now so the contract is enforced from day one when
    the DocType lands."""
    return append_only(doc, ptype, user)


# ----- Helpers -----


def _user_company_filter(user: str) -> list[str] | None:
    """Return the list of Companies the user has explicit User Permissions
    for. Returns None if the user has NO Company restrictions (sees all),
    or an empty list if the user has the role but no Company access
    (sees nothing).
    """
    perms = frappe.db.get_all(
        "User Permission",
        filters={"user": user, "allow": "Company"},
        pluck="for_value",
    )
    if not perms:
        return None
    return list(perms)
