"""gh#166 followup (2026-07-14): re-run the EasyEcom Integration role
permission setup so the DocTypes added in the same-day update land
on already-deployed sites.

Live symptom on mmpl16: SO-2610397 inbound /einvoice/update returned
'PermissionError: User don't have permissions to select/read this
account.' — the integration user had no `Account: read` perm, and
SI insert validates the tax rows' account_head against Account.

The original patch (create_easyecom_integration_user) already ran
and only registered the initial DocType allowlist. Rather than
rewrite that patch, this one re-invokes its `_ensure_role_permissions`
which is idempotent: existing perms get realigned, new DocTypes get
new Custom DocPerm rows created.
"""
from __future__ import annotations


def execute() -> None:
    from ecommerce_super.patches.v0_1.create_easyecom_integration_user import (
        _ensure_role_permissions,
    )
    _ensure_role_permissions()
