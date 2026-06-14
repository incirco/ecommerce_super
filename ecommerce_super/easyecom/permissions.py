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


def company_scope(user: str | None = None, doctype: str | None = None) -> str:
    """permission_query_conditions hook. Returns a SQL WHERE fragment that
    restricts the queried DocType to the Companies in the user's User
    Permissions.

    System Manager and Administrator see all Companies (no filter). Other
    users see only the Companies they're explicitly granted via User
    Permissions on the Company DocType.

    Frappe v15+ passes `doctype` as a kwarg via `frappe.call` from
    `DatabaseQuery.get_permission_query_conditions` — we use it to embed
    the actual table name in the returned SQL fragment (gh#14). The
    prior version used a literal `{doctype}` placeholder that was never
    substituted, producing broken SQL that Frappe silently swallowed
    and falling through to no-filter — so EasyEcom FDE users saw every
    Company's row instead of only their permitted Companies.

    Defensive single-quote escape on each Company name so a Company
    containing an apostrophe can't break the SQL (or worse).
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

    # Escape single quotes inside Company names (defense-in-depth — the
    # values come from User Permission rows, but trust-and-verify).
    quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in allowed)
    # `doctype` is supplied by Frappe via frappe.call; fall back to the
    # current hook's primary doctype if a caller invokes us directly
    # without one (defensive — tests, ad-hoc calls).
    target_doctype = doctype or "EasyEcom Company Settings"
    return f"`tab{target_doctype}`.company in ({quoted})"


def append_only(doc: Any, ptype: str, user: str | None = None) -> bool:
    """has_permission hook. Returns True only for read and create — every
    other ptype (write, delete, cancel, submit, etc.) is refused for ALL
    roles including System Manager (§31.7.4)."""
    if ptype in {"read", "report", "export", "create"}:
        return True
    return False


def restrict_account_write(
    doc: Any, ptype: str, user: str | None = None
) -> bool:
    """has_permission hook for EasyEcom Account (gh#14 follow-up #2).

    Backstop the DocPerm shape: only System Manager and EasyEcom System
    Manager can write/delete/create EasyEcom Account. Every other role —
    including EasyEcom FDE — is read-only.

    Frappe's DocPerm already encodes this (the FDE role's permission
    entry has only `read: 1`), but the reporter (mmpl16, 2026-06-13)
    found that the form's edit-flow UI surfaces (Actions menu, Bulk
    Edit dialog) still render and let the user "proceed toward record
    modification" before the server-side save rejection fires. Adding
    a has_permission hook means the rejection fires AT permission-check
    time — Frappe's form layer reads it via `frappe.has_permission` and
    hides edit affordances accordingly.

    Pass-throughs:
      - Administrator (Frappe convention).
      - System Manager (highest built-in role).
      - EasyEcom System Manager (the role we designate for Account
        management in production deployments).
    Everyone else: read / report / export only.
    """
    user = user or frappe.session.user
    if user in {"Administrator", None}:
        return True

    if ptype in {"read", "report", "export"}:
        return True

    roles = set(frappe.get_roles(user))
    if "System Manager" in roles or "EasyEcom System Manager" in roles:
        return True

    # FDE / Operator / Auditor / Replay Approver — read-only.
    return False


def company_scope_doc(doc: Any, ptype: str, user: str | None = None) -> bool:
    """has_permission hook — per-document Company scoping (gh#14 follow-up).

    `permission_query_conditions` (company_scope) filters list views, but
    Frappe's per-doc read/write path (e.g. opening
    `/app/easyecom-company-settings/SomeCompany` directly, or
    `frappe.get_doc(...)` from another flow) takes the DocPerm chain via
    `has_permission_hooks`. Without a hook on this surface, a user with
    DocPerm read/write at perm-level 0 could open any document, including
    those for Companies they have no User Permission for.

    Symptom (mmpl16 UAT, 2026-06-12, per garv999's reopen): an EasyEcom
    FDE with `User Permission { allow: Company, for_value: Co A }` could
    still open AND edit `EasyEcom Company Settings / Co B`.

    This hook returns False when the doc's Company isn't in the user's
    User Permission allowlist, blocking the per-doc path symmetrically
    with the list filter.

    Always allows:
      - Administrator (Frappe convention)
      - System Manager (highest built-in role; can see/edit everything)
      - any user whose User Permissions contain no Company restriction
        (matches the list filter's `allowed is None → ""` branch)
      - `create` ptype (the new-doc form has no `doc.company` yet; the
        controller's `validate` rejects mismatches at save time)
    """
    user = user or frappe.session.user
    if user in {"Administrator", None}:
        return True

    roles = set(frappe.get_roles(user))
    if "System Manager" in roles:
        return True

    # New-doc / unsaved → defer to validate-time check. Company hasn't
    # been chosen yet for `create`; refusing here would block the New
    # button entirely.
    if ptype == "create" or not getattr(doc, "company", None):
        return True

    allowed = _user_company_filter(user)
    if allowed is None:
        # No Company restrictions configured → no scoping.
        return True

    return doc.company in allowed


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


# ----- v16 apps-screen visibility -----


_APPS_SCREEN_ROLES = frozenset(
    {
        "EasyEcom Operator",
        "EasyEcom FDE",
        "EasyEcom Replay Approver",
        "EasyEcom System Manager",
        "EasyEcom Auditor",
        "System Manager",
        "Administrator",
    }
)


def has_app_screen_permission() -> bool:
    """Whether the current user sees the EasyEcom icon on /desk (the v16
    app launcher). Returns True for any user with one of the five EasyEcom
    custom roles, plus Frappe's built-in System Manager and Administrator
    (for development convenience)."""
    user = frappe.session.user
    if user in {"Administrator"}:
        return True
    return bool(set(frappe.get_roles(user)) & _APPS_SCREEN_ROLES)
