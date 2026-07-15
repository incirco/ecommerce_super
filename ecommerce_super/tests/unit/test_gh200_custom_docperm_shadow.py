"""gh#200 regression — `_ensure_role_permissions` must not shadow
standard DocPerms on doctypes that have no prior Custom DocPerms.

The prior implementation inserted `Custom DocPerm` rows directly via
`frappe.new_doc("Custom DocPerm")`. Frappe's rule: if ANY Custom
DocPerm exists for a DocType, ALL standard DocPerms are ignored. On
doctypes with no prior Custom rows (Territory, Customer Group, Print
Format, and every core master), our insert became the only perm row —
wiping every other role's access.

Fix (this suite locks): use `setup_custom_perms(doctype)` first to
copy standard DocPerms into Custom DocPerms (preserving all other
roles), then add ours via `add_permission()` +
`update_permission_property()`.

Live symptom this suite guards against re-appearing:
  Territory, Customer Group, Print Format permissions vanish for
  Sales User, System Manager, etc. on any deployed site after
  `bench migrate` runs.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

import frappe


class TestEnsureRolePermissionsCallsSetupCustomPermsFirst(unittest.TestCase):
    """The cornerstone test: setup_custom_perms MUST be called BEFORE
    any add_permission / update_permission_property. Any refactor that
    reverses this order re-introduces the shadow bug."""

    def test_setup_custom_perms_called_before_add_permission(self) -> None:
        from ecommerce_super.patches.v0_1 import create_easyecom_integration_user as mod

        call_log: list[str] = []

        def _setup(doctype):
            call_log.append(f"setup:{doctype}")

        def _add(doctype, role, permlevel=0, ptype=None):
            call_log.append(f"add:{doctype}")

        def _update(doctype, role, permlevel, ptype, value=None, validate=True, if_owner=0):
            call_log.append(f"update:{doctype}:{ptype}")

        # Only stub the doctype existence check for a subset so the
        # loop is bounded and predictable.
        _subset = {"Sales Invoice": {"read": 1, "write": 1}}

        with (
            patch.object(mod, "_PERMISSIONS", _subset),
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe.db, "get_value", return_value=None),
            patch("frappe.permissions.setup_custom_perms", side_effect=_setup),
            patch("frappe.permissions.add_permission", side_effect=_add),
            patch("frappe.permissions.update_permission_property", side_effect=_update),
            patch.object(frappe, "clear_cache"),
        ):
            mod._ensure_role_permissions()

        # setup:Sales Invoice must appear BEFORE add:Sales Invoice.
        # This is the critical ordering that prevents the shadow bug.
        self.assertIn("setup:Sales Invoice", call_log)
        self.assertIn("add:Sales Invoice", call_log)
        self.assertLess(
            call_log.index("setup:Sales Invoice"),
            call_log.index("add:Sales Invoice"),
            f"setup_custom_perms MUST come before add_permission — got: {call_log}",
        )

    def test_setup_custom_perms_called_even_when_our_row_already_exists(self) -> None:
        """Idempotent re-run: setup_custom_perms is a no-op when
        Custom DocPerms exist, but it MUST still be called on every
        doctype so a partial prior run (some doctypes shadowed, others
        not) converges to safe state on the next migrate."""
        from ecommerce_super.patches.v0_1 import create_easyecom_integration_user as mod

        setup_calls: list[str] = []

        with (
            patch.object(mod, "_PERMISSIONS", {"Sales Invoice": {"read": 1}}),
            patch.object(frappe.db, "exists", return_value=True),
            # get_value returns a name → our row already exists,
            # add_permission should NOT be called.
            patch.object(frappe.db, "get_value", return_value="existing-name"),
            patch(
                "frappe.permissions.setup_custom_perms",
                side_effect=lambda dt: setup_calls.append(dt),
            ),
            patch("frappe.permissions.add_permission") as add_mock,
            patch("frappe.permissions.update_permission_property"),
            patch.object(frappe, "clear_cache"),
        ):
            mod._ensure_role_permissions()

        self.assertEqual(setup_calls, ["Sales Invoice"])
        add_mock.assert_not_called()  # already-exists path skips add

    def test_missing_doctype_skipped_no_perm_calls(self) -> None:
        """Doctypes not present on the site (e.g. India Compliance not
        installed) must be skipped silently. No setup/add/update calls
        for them."""
        from ecommerce_super.patches.v0_1 import create_easyecom_integration_user as mod

        # Two doctypes: one present, one absent.
        _subset = {
            "Sales Invoice": {"read": 1},
            "Some Missing DocType": {"read": 1},
        }

        def _exists(doctype, name):
            # doctype=DocType, name=<the checked one>
            return name == "Sales Invoice"

        setup_calls: list[str] = []
        with (
            patch.object(mod, "_PERMISSIONS", _subset),
            patch.object(frappe.db, "exists", side_effect=_exists),
            patch.object(frappe.db, "get_value", return_value=None),
            patch(
                "frappe.permissions.setup_custom_perms",
                side_effect=lambda dt: setup_calls.append(dt),
            ),
            patch("frappe.permissions.add_permission"),
            patch("frappe.permissions.update_permission_property"),
            patch.object(frappe, "clear_cache"),
        ):
            mod._ensure_role_permissions()

        self.assertEqual(setup_calls, ["Sales Invoice"])
        # "Some Missing DocType" was skipped — no setup call for it.
        self.assertNotIn("Some Missing DocType", setup_calls)

    def test_all_declared_flags_reconciled_including_zeros(self) -> None:
        """Idempotent narrowing: a flag NOT in the allowlist must be
        explicitly set to 0. Otherwise a re-run after we tightened
        permissions (removed a flag from the dict) would leave the old
        wider grant in place."""
        from ecommerce_super.patches.v0_1 import create_easyecom_integration_user as mod

        # Only 'read' granted — 'write', 'delete', etc. must be forced to 0.
        _subset = {"Sales Invoice": {"read": 1}}
        update_calls: list[tuple] = []

        def _update(doctype, role, permlevel, ptype, value=None, validate=True, if_owner=0):
            update_calls.append((ptype, value))

        with (
            patch.object(mod, "_PERMISSIONS", _subset),
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe.db, "get_value", return_value=None),
            patch("frappe.permissions.setup_custom_perms"),
            patch("frappe.permissions.add_permission"),
            patch("frappe.permissions.update_permission_property", side_effect=_update),
            patch.object(frappe, "clear_cache"),
        ):
            mod._ensure_role_permissions()

        # read set to 1
        self.assertIn(("read", 1), update_calls)
        # write must be forced to 0 (allowlist narrowing)
        self.assertIn(("write", 0), update_calls)
        # delete must be forced to 0
        self.assertIn(("delete", 0), update_calls)
        # cancel, submit, amend, etc. — all forced to 0 explicitly
        for flag in ("submit", "cancel", "amend", "export", "email", "print", "share"):
            self.assertIn(
                (flag, 0), update_calls,
                f"Flag '{flag}' not reconciled to 0 — silent widening risk",
            )

    def test_if_owner_filter_included_in_existing_check(self) -> None:
        """Frappe uniques Custom DocPerm on (parent, role, permlevel,
        if_owner). Leaving out the if_owner=0 filter would false-match
        an if-owner row and skip add_permission when it should fire."""
        from ecommerce_super.patches.v0_1 import create_easyecom_integration_user as mod

        captured_filters: list[dict] = []

        def _get_value(doctype, filters, field):
            captured_filters.append(dict(filters))
            return None

        with (
            patch.object(mod, "_PERMISSIONS", {"Sales Invoice": {"read": 1}}),
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe.db, "get_value", side_effect=_get_value),
            patch("frappe.permissions.setup_custom_perms"),
            patch("frappe.permissions.add_permission"),
            patch("frappe.permissions.update_permission_property"),
            patch.object(frappe, "clear_cache"),
        ):
            mod._ensure_role_permissions()

        # The existence check must filter on if_owner=0 to distinguish
        # the base perm row from any if-owner variant.
        self.assertEqual(len(captured_filters), 1)
        self.assertEqual(captured_filters[0].get("if_owner"), 0)


class TestRepairPatchUndoesShadowThenReAppliesSafely(unittest.TestCase):
    """The repair patch flow — critical for MMPL and any deployed site
    that ran the pre-fix patch. It must:
      1. Delete our Custom DocPerm rows on every affected doctype
         (so standard perms are restored on freshly-shadowed doctypes).
      2. Clear meta cache so subsequent lookups see the fresh state.
      3. Commit before re-adding — mid-run crash leaves standard perms
         restored (safer state) rather than double-shadowed.
      4. Re-run the FIXED _ensure_role_permissions.
    """

    def test_delete_precedes_reensure_call(self) -> None:
        from ecommerce_super.patches.v0_1 import repair_gh200_unshadow_standard_perms as repair

        call_log: list[str] = []

        def _delete(doctype, filters):
            # frappe.db.delete(<Custom DocPerm>, {parent: <target dt>, role: ...})
            # Log the target parent doctype (from filters), not the deleted-row's DocType.
            call_log.append(
                f"delete:{filters.get('parent')}:{filters.get('role')}"
            )

        def _clear(doctype=None):
            call_log.append(f"clear:{doctype}")

        def _commit():
            call_log.append("commit")

        def _reensure():
            call_log.append("reensure")

        _subset = {"Sales Invoice": {"read": 1, "write": 1}}

        with (
            patch(
                "ecommerce_super.patches.v0_1.create_easyecom_integration_user._PERMISSIONS",
                _subset,
            ),
            patch(
                "ecommerce_super.patches.v0_1.create_easyecom_integration_user."
                "_ensure_role_permissions",
                side_effect=_reensure,
            ),
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe.db, "delete", side_effect=_delete),
            patch.object(frappe.db, "commit", side_effect=_commit),
            patch.object(frappe, "clear_cache", side_effect=_clear),
        ):
            repair.execute()

        # Ordering assertions — the critical safety invariants.
        delete_idx = call_log.index("delete:Sales Invoice:EasyEcom Integration")
        commit_idx = [i for i, x in enumerate(call_log) if x == "commit"][0]
        reensure_idx = call_log.index("reensure")

        self.assertLess(delete_idx, commit_idx,
            "Delete must precede commit — otherwise a mid-run crash leaves "
            "our row in place with no commit to release it")
        self.assertLess(commit_idx, reensure_idx,
            "Commit must precede _ensure_role_permissions — otherwise "
            "the reensure sees the pre-delete state via row cache")

    def test_delete_filters_only_our_role(self) -> None:
        """We must NEVER delete Custom DocPerm rows for other roles.
        A site may have pre-existing customizations (Case B) that
        MUST survive the repair."""
        from ecommerce_super.patches.v0_1 import repair_gh200_unshadow_standard_perms as repair

        captured_deletes: list[dict] = []

        def _delete(doctype, filters):
            captured_deletes.append(dict(filters))

        with (
            patch(
                "ecommerce_super.patches.v0_1.create_easyecom_integration_user._PERMISSIONS",
                {"Sales Invoice": {"read": 1}},
            ),
            patch(
                "ecommerce_super.patches.v0_1.create_easyecom_integration_user."
                "_ensure_role_permissions",
            ),
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe.db, "delete", side_effect=_delete),
            patch.object(frappe.db, "commit"),
            patch.object(frappe, "clear_cache"),
        ):
            repair.execute()

        self.assertEqual(len(captured_deletes), 1)
        self.assertEqual(
            captured_deletes[0].get("role"), "EasyEcom Integration",
            "Delete filter MUST scope to our role — deleting other-role "
            "Custom DocPerms would wipe pre-existing site customizations",
        )

    def test_missing_doctype_skipped_no_delete(self) -> None:
        """Doctypes absent from the site (India Compliance not
        installed, etc.) must be skipped — no delete against
        non-existent parent."""
        from ecommerce_super.patches.v0_1 import repair_gh200_unshadow_standard_perms as repair

        captured_deletes: list[str] = []

        def _exists(doctype, name):
            return name == "Sales Invoice"

        def _delete(doctype, filters):
            captured_deletes.append(filters.get("parent"))

        _subset = {
            "Sales Invoice": {"read": 1},
            "Some IC DocType": {"read": 1},  # pretend IC not installed
        }
        with (
            patch(
                "ecommerce_super.patches.v0_1.create_easyecom_integration_user._PERMISSIONS",
                _subset,
            ),
            patch(
                "ecommerce_super.patches.v0_1.create_easyecom_integration_user."
                "_ensure_role_permissions",
            ),
            patch.object(frappe.db, "exists", side_effect=_exists),
            patch.object(frappe.db, "delete", side_effect=_delete),
            patch.object(frappe.db, "commit"),
            patch.object(frappe, "clear_cache"),
        ):
            repair.execute()

        self.assertIn("Sales Invoice", captured_deletes)
        self.assertNotIn("Some IC DocType", captured_deletes)


class TestPermissionsAllowlistDoesNotIncludeSensitiveDoctypes(unittest.TestCase):
    """gh#166 principle — the EasyEcom Integration role must NEVER
    have permissions on: User, Role, System Settings, Server Script,
    Custom DocPerm, DocPerm. Any expansion of _PERMISSIONS that adds
    one of these is a security regression.
    """

    _FORBIDDEN = frozenset({
        "User", "Role", "System Settings", "Server Script",
        "Custom DocPerm", "DocPerm", "Role Permission Manager",
        "Custom Script", "Client Script", "Property Setter",
    })

    def test_no_forbidden_doctypes_in_allowlist(self) -> None:
        from ecommerce_super.patches.v0_1.create_easyecom_integration_user import (
            _PERMISSIONS,
        )
        granted = set(_PERMISSIONS.keys())
        overlap = granted & self._FORBIDDEN
        self.assertEqual(
            overlap, set(),
            f"Security-sensitive doctypes leaked into allowlist: {overlap}",
        )


if __name__ == "__main__":
    unittest.main()
