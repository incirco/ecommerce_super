"""Regression for the Go Live + Pause auto-push controls.

The two whitelisted endpoints in ecommerce_super.easyecom.api.
auto_push_controls let the FDE flip all three auto_push_*_on_save
toggles in one audit-trailed ceremony. Tests verify:
  - Both endpoints role-gate (Operator refused).
  - Both refuse without confirm.
  - go_live: selective enable (just Items, just Customers, just
    Suppliers), all-three default, warnings when master-mode is
    still onboarding, audit Comment written.
  - pause_all: idempotent on already-paused account, reason captured
    in Comment, all three flip OFF.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.auto_push_controls import (
    go_live_enable_auto_push,
    pause_all_auto_push,
)


_ACCT = "test-autopush-controls"


def _make_test_account() -> str:
    """Create a disabled test Account so the single-Account
    constraint doesn't conflict with whatever is already enabled."""
    if frappe.db.exists("EasyEcom Account", _ACCT):
        return _ACCT
    doc = frappe.new_doc("EasyEcom Account")
    doc.update(
        {
            "account_name": _ACCT,
            "enabled": 0,
            "environment_badge": "Sandbox",
            "api_endpoint": "https://api.example.com",
            "x_api_key": "test-key",
            "email": "test@example.com",
            "password": "test-pwd",
            "rate_limit_tier": "Silver",
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return _ACCT


def _wipe_test_account() -> None:
    if frappe.db.exists("EasyEcom Account", _ACCT):
        try:
            frappe.delete_doc(
                "EasyEcom Account", _ACCT, force=True, ignore_permissions=True
            )
        except Exception:
            pass
        frappe.db.commit()


class TestGoLiveEnableAutoPush(FrappeTestCase):
    def setUp(self) -> None:
        _wipe_test_account()
        self.account = _make_test_account()

    def tearDown(self) -> None:
        _wipe_test_account()

    def test_refuses_without_confirm(self) -> None:
        r = go_live_enable_auto_push(account=self.account)
        self.assertFalse(r["ok"])
        self.assertIn("confirm", r["message"].lower())
        # Toggles unchanged.
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Account", self.account, "auto_push_on_save"
            ),
            0,
        )

    def test_refuses_unknown_account(self) -> None:
        r = go_live_enable_auto_push(
            account="no-such-account", confirm=1
        )
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["message"].lower())

    def test_refuses_when_all_three_falsy(self) -> None:
        # Corrective commit 2026-05-29 (FIX 2): pos=0 too so the
        # 4-toggle signature still resolves to "nothing to enable".
        r = go_live_enable_auto_push(
            account=self.account,
            items=0, customers=0, suppliers=0, pos=0,
            confirm=1,
        )
        self.assertFalse(r["ok"])
        self.assertIn("nothing to enable", r["message"].lower())

    def test_all_three_default_enables_all(self) -> None:
        r = go_live_enable_auto_push(account=self.account, confirm=1)
        self.assertTrue(r["ok"])
        # Corrective commit 2026-05-29 (FIX 2): state dict now has 4
        # keys (added 'pos'). Defaults enable all four.
        self.assertEqual(
            r["state"], {"items": 1, "customers": 1, "suppliers": 1, "pos": 1}
        )
        # DB confirms.
        row = frappe.db.get_value(
            "EasyEcom Account",
            self.account,
            [
                "auto_push_on_save",
                "auto_push_customers_on_save",
                "auto_push_suppliers_on_save",
                "auto_push_pos_on_save",
            ],
            as_dict=True,
        )
        self.assertEqual(int(row.auto_push_on_save), 1)
        self.assertEqual(int(row.auto_push_customers_on_save), 1)
        self.assertEqual(int(row.auto_push_suppliers_on_save), 1)
        self.assertEqual(int(row.auto_push_pos_on_save), 1)

    def test_selective_enable_customers_only(self) -> None:
        r = go_live_enable_auto_push(
            account=self.account,
            items=0, customers=1, suppliers=0, pos=0,
            confirm=1,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(
            r["state"], {"items": 0, "customers": 1, "suppliers": 0, "pos": 0}
        )
        self.assertEqual(r["transitioned"], ["Customers"])

    def test_warns_when_master_mode_still_onboarding(self) -> None:
        """Both master-modes default to 'onboarding' on a fresh
        Account — enabling auto-push should land but produce a
        warning per entity that's still in onboarding."""
        r = go_live_enable_auto_push(account=self.account, confirm=1)
        self.assertTrue(r["ok"])
        # All three master-modes are 'onboarding' by default on this
        # fresh test account — Items doesn't have a master-mode flag
        # in the JSON (item_master_mode field IS there with default
        # 'onboarding'). Expect 3 warnings.
        self.assertEqual(len(r["warnings"]), 3)
        self.assertTrue(any("Item master" in w for w in r["warnings"]))
        self.assertTrue(any("Customer master" in w for w in r["warnings"]))
        self.assertTrue(any("Supplier master" in w for w in r["warnings"]))

    def test_no_warning_when_master_mode_is_erpnext_mastered(self) -> None:
        """Flip Customers to erpnext_mastered, then go-live for
        Customers only — no warning."""
        frappe.db.set_value(
            "EasyEcom Account",
            self.account,
            "customer_master_mode",
            "erpnext_mastered",
            update_modified=False,
        )
        frappe.db.commit()
        r = go_live_enable_auto_push(
            account=self.account,
            items=0, customers=1, suppliers=0,
            confirm=1,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["warnings"], [])

    def test_audit_comment_written(self) -> None:
        go_live_enable_auto_push(account=self.account, confirm=1)
        comments = frappe.get_all(
            "Comment",
            filters={
                "reference_doctype": "EasyEcom Account",
                "reference_name": self.account,
            },
            fields=["content"],
        )
        self.assertTrue(comments)
        self.assertTrue(
            any("Go Live" in c.content for c in comments),
            "audit Comment must mention the Go Live action",
        )
        self.assertTrue(
            any("Items, Customers, Suppliers" in c.content for c in comments),
            "audit Comment must list which entities were enabled",
        )

    def test_refused_for_operator_role(self) -> None:
        email = "op-go-live@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
        u = frappe.new_doc("User")
        u.update({
            "email": email, "first_name": "Op",
            "send_welcome_email": 0, "enabled": 1,
        })
        u.insert(ignore_permissions=True)
        u.append("roles", {"role": "EasyEcom Operator"})
        u.save(ignore_permissions=True)
        frappe.db.commit()
        orig = frappe.session.user
        frappe.set_user(email)
        try:
            with self.assertRaises(frappe.PermissionError):
                go_live_enable_auto_push(account=self.account, confirm=1)
        finally:
            frappe.set_user(orig)
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
            frappe.db.commit()


class TestPauseAllAutoPush(FrappeTestCase):
    def setUp(self) -> None:
        _wipe_test_account()
        self.account = _make_test_account()
        # Corrective commit 2026-05-29 (FIX 2): start with all FOUR
        # ON so the pause has the §9 PO toggle to flip too. The prior
        # 3-toggle setup left auto_push_pos_on_save uncovered — fixed
        # under §9 corrective scope.
        frappe.db.set_value(
            "EasyEcom Account",
            self.account,
            {
                "auto_push_on_save": 1,
                "auto_push_customers_on_save": 1,
                "auto_push_suppliers_on_save": 1,
                "auto_push_pos_on_save": 1,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe_test_account()

    def test_refuses_without_confirm(self) -> None:
        r = pause_all_auto_push(account=self.account)
        self.assertFalse(r["ok"])
        self.assertIn("confirm", r["message"].lower())

    def test_pause_flips_all_four_off(self) -> None:
        # Corrective commit 2026-05-29 (FIX 2): pause now flips FOUR
        # toggles. Renamed from test_pause_flips_all_three_off.
        r = pause_all_auto_push(
            account=self.account, reason="Test", confirm=1
        )
        self.assertTrue(r["ok"])
        self.assertEqual(
            r["state"], {"items": 0, "customers": 0, "suppliers": 0, "pos": 0}
        )
        # Reports what was active.
        self.assertEqual(
            set(r["was_active"]),
            {"Items", "Customers", "Suppliers", "POs"},
        )

    def test_pause_idempotent_on_already_paused(self) -> None:
        # Pause once.
        pause_all_auto_push(
            account=self.account, reason="First", confirm=1
        )
        # Pause again.
        r = pause_all_auto_push(
            account=self.account, reason="Second", confirm=1
        )
        self.assertTrue(r["ok"])
        # No active entities the second time.
        self.assertEqual(r["was_active"], [])

    def test_reason_captured_in_audit_comment(self) -> None:
        reason = "EE rate-limit storm 2026-05-28"
        pause_all_auto_push(
            account=self.account, reason=reason, confirm=1
        )
        comments = frappe.get_all(
            "Comment",
            filters={
                "reference_doctype": "EasyEcom Account",
                "reference_name": self.account,
            },
            fields=["content"],
        )
        self.assertTrue(
            any(reason in c.content for c in comments),
            f"reason must land in Comment; got {[c.content[:120] for c in comments]}",
        )

    def test_refused_for_operator_role(self) -> None:
        email = "op-pause-auto@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
        u = frappe.new_doc("User")
        u.update({
            "email": email, "first_name": "Op",
            "send_welcome_email": 0, "enabled": 1,
        })
        u.insert(ignore_permissions=True)
        u.append("roles", {"role": "EasyEcom Operator"})
        u.save(ignore_permissions=True)
        frappe.db.commit()
        orig = frappe.session.user
        frappe.set_user(email)
        try:
            with self.assertRaises(frappe.PermissionError):
                pause_all_auto_push(account=self.account, confirm=1)
        finally:
            frappe.set_user(orig)
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
            frappe.db.commit()
