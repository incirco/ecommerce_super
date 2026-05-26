"""§8d Stage 6 tests — auto-push hook + whitelist wrappers.

The auto_push_on_save flag defaults 0 so the test suite's many Item
saves don't accidentally trigger the hook. These tests explicitly
toggle the flag to assert the hook behaviour.

The hook itself enqueues via frappe.enqueue — we DON'T let the
queued worker actually run a real EE call. Tests monkeypatch the
underlying `enqueue_item_push` to a spy that records the
(item_code, account_name) it was called with. That way we verify
the hook's gating without ever needing real EE credentials.

Whitelist endpoints (push_one_product / push_lifecycle_product /
push_all_pending_products) are tested for role gating + clean error
returns. Successful happy paths are covered by Stages 3–5's tests
that exercise push_one_item / push_lifecycle / push_all_pending
directly with MockPushClient.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

import ecommerce_super.easyecom.flows.item_push as item_push
from ecommerce_super.easyecom.flows.item_push import (
    enqueue_on_bundle_change,
    enqueue_on_item_change,
    push_all_pending_products,
    push_lifecycle_product,
    push_one_product,
)
from ecommerce_super.tests.factories import make_account


PREFIX = "TEST-8D-S6-"


# ----- Helpers -----


def _ensure_hsn(code: str = "85171000") -> str:
    if frappe.db.exists("GST HSN Code", code):
        return code
    hsn = frappe.new_doc("GST HSN Code")
    hsn.update({"hsn_code": code, "description": "Test HSN"})
    hsn.insert(ignore_permissions=True)
    return code


def _ensure_uom(name: str = "Nos") -> str:
    if frappe.db.exists("UOM", name):
        return name
    u = frappe.new_doc("UOM")
    u.update({"uom_name": name, "must_be_whole_number": 1})
    u.insert(ignore_permissions=True)
    return name


def _ensure_item_group(name: str = "All Item Groups") -> str:
    if frappe.db.exists("Item Group", name):
        return name
    g = frappe.new_doc("Item Group")
    g.update({"item_group_name": name, "is_group": 1})
    g.insert(ignore_permissions=True)
    return name


def _account_with_auto_push(enabled: int = 1, auto_push: int = 1) -> str:
    name = f"{PREFIX}auto-acct".lower()
    if not frappe.db.exists("EasyEcom Account", name):
        make_account(name=name)
    frappe.db.set_value(
        "EasyEcom Account", name,
        {"enabled": enabled, "auto_push_on_save": auto_push},
        update_modified=False,
    )
    frappe.db.commit()
    return name


def _make_item(item_code: str, **overrides) -> Any:
    """Insert a fresh Item. Uses the same minimal shape as Stage 3+
    test factories."""
    if frappe.db.exists("Item", item_code):
        for n in frappe.db.get_all(
            "EasyEcom Item Map", filters={"erpnext_name": item_code},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Item Map", n, force=True, ignore_permissions=True
            )
        frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
    item = frappe.new_doc("Item")
    item.update({
        "item_code": item_code,
        "item_name": item_code,
        "item_group": _ensure_item_group(),
        "stock_uom": _ensure_uom(),
        "gst_hsn_code": _ensure_hsn(),
        "is_stock_item": 1,
    })
    item.update(overrides)
    item.insert(ignore_permissions=True)
    return item


def _wipe() -> None:
    # Product Bundle first (FK → Item via new_item_code), then map, then Items.
    for n in frappe.db.get_all(
        "Product Bundle",
        filters={"new_item_code": ("like", f"{PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "Product Bundle", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom Item Map",
        filters={"erpnext_name": ("like", f"{PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Item Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "Item", filters={"item_code": ("like", f"{PREFIX}%")}, pluck="name"
    ):
        try:
            frappe.delete_doc("Item", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    if frappe.db.exists("EasyEcom Account", f"{PREFIX}auto-acct".lower()):
        try:
            frappe.delete_doc(
                "EasyEcom Account", f"{PREFIX}auto-acct".lower(),
                force=True, ignore_permissions=True,
            )
        except Exception:
            pass
    frappe.db.commit()


@contextmanager
def _spy_on_enqueue():
    """Monkeypatch enqueue_item_push to record calls instead of
    actually queueing. Restored on exit."""
    calls: list[tuple[str, str]] = []
    original = item_push.enqueue_item_push

    def _spy(item_code: str, *, account_name: str) -> None:
        calls.append((item_code, account_name))

    item_push.enqueue_item_push = _spy
    try:
        yield calls
    finally:
        item_push.enqueue_item_push = original


# ============================================================
# 1. Auto-push hook — gating
# ============================================================


class TestAutoPushHookGating(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()
        frappe.flags.in_easyecom_pull = False

    def tearDown(self) -> None:
        _wipe()
        frappe.flags.in_easyecom_pull = False

    def test_hook_noop_when_flag_off(self) -> None:
        """Default: auto_push_on_save=0 → no enqueue, regardless of
        Item state. This is THE safety net that keeps every existing
        test in the suite from accidentally pushing to EE."""
        _account_with_auto_push(auto_push=0)
        item = _make_item(f"{PREFIX}flagoff-1")
        with _spy_on_enqueue() as calls:
            enqueue_on_item_change(item)
            self.assertEqual(calls, [])

    def test_hook_fires_when_flag_on(self) -> None:
        account_name = _account_with_auto_push(auto_push=1)
        item = _make_item(f"{PREFIX}flagon-1")
        with _spy_on_enqueue() as calls:
            enqueue_on_item_change(item)
            self.assertEqual(calls, [(item.item_code, account_name)])

    def test_hook_skips_inside_easyecom_pull(self) -> None:
        """When the Stage-2 pull is saving an Item, the hook MUST NOT
        re-push it back to EE. The pull sets frappe.flags
        .in_easyecom_pull = True; the hook reads it and short-circuits."""
        _account_with_auto_push(auto_push=1)
        item = _make_item(f"{PREFIX}inpull-1")
        frappe.flags.in_easyecom_pull = True
        try:
            with _spy_on_enqueue() as calls:
                enqueue_on_item_change(item)
                self.assertEqual(calls, [])
        finally:
            frappe.flags.in_easyecom_pull = False

    def test_hook_skips_variant_template(self) -> None:
        """has_variants=1 means this Item is a template, not a real
        product. Variants aren't synced to EE."""
        _account_with_auto_push(auto_push=1)
        item = _make_item(f"{PREFIX}variant-tpl")
        # Set the flag after insert (avoid the variant scaffolding).
        item.has_variants = 1
        item.db_set("has_variants", 1, update_modified=False)
        with _spy_on_enqueue() as calls:
            enqueue_on_item_change(item)
            self.assertEqual(calls, [])

    def test_hook_noop_when_account_disabled(self) -> None:
        _account_with_auto_push(enabled=0, auto_push=1)
        item = _make_item(f"{PREFIX}disacct-1")
        with _spy_on_enqueue() as calls:
            enqueue_on_item_change(item)
            self.assertEqual(calls, [])

    def test_bundle_save_pushes_wrapper(self) -> None:
        """A Product Bundle save fires the bundle hook which enqueues
        the wrapper Item — push_one_item then auto-dispatches to
        push_one_bundle."""
        account_name = _account_with_auto_push(auto_push=1)
        wrapper = _make_item(f"{PREFIX}bw-1", is_stock_item=0)
        comp = _make_item(f"{PREFIX}bw-c-1")
        comp2 = _make_item(f"{PREFIX}bw-c-2")
        # Building a Product Bundle would ITSELF fire the auto-push
        # hook for the wrapper Item (Item.on_update during the
        # bundle's component-resolution save chain). Disable the
        # auto-push during construction by clearing the flag, then
        # re-enabling it just for the test.
        frappe.db.set_value(
            "EasyEcom Account", account_name,
            {"auto_push_on_save": 0}, update_modified=False,
        )
        frappe.db.commit()
        bundle = frappe.new_doc("Product Bundle")
        bundle.update({"new_item_code": wrapper.item_code})
        bundle.append("items", {"item_code": comp.item_code, "qty": 1})
        bundle.append("items", {"item_code": comp2.item_code, "qty": 1})
        bundle.insert(ignore_permissions=True)
        frappe.db.set_value(
            "EasyEcom Account", account_name,
            {"auto_push_on_save": 1}, update_modified=False,
        )
        frappe.db.commit()

        with _spy_on_enqueue() as calls:
            enqueue_on_bundle_change(bundle)
            self.assertEqual(calls, [(wrapper.item_code, account_name)])


# ============================================================
# 2. Whitelist permission gates
# ============================================================


class TestWhitelistPermissionGates(FrappeTestCase):
    """Each manual-trigger whitelist refuses non-FDE / non-System-Manager
    callers. The frappe-tests admin always has all roles by default;
    we create an Operator-only user to prove the gate rejects."""

    OPERATOR_EMAIL = f"{PREFIX}operator@test.local".lower()

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()
        if frappe.db.exists("User", self.OPERATOR_EMAIL):
            frappe.delete_doc(
                "User", self.OPERATOR_EMAIL, force=True, ignore_permissions=True
            )
        user = frappe.new_doc("User")
        user.update({
            "email": self.OPERATOR_EMAIL,
            "first_name": "Op",
            "send_welcome_email": 0,
            "enabled": 1,
        })
        user.insert(ignore_permissions=True)
        user.append("roles", {"role": "EasyEcom Operator"})
        user.save(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe()
        if frappe.db.exists("User", self.OPERATOR_EMAIL):
            try:
                frappe.delete_doc(
                    "User", self.OPERATOR_EMAIL,
                    force=True, ignore_permissions=True,
                )
            except Exception:
                pass
            frappe.db.commit()

    def _as_operator(self):
        return _set_user(self.OPERATOR_EMAIL)

    def test_push_one_product_refuses_operator(self) -> None:
        item = _make_item(f"{PREFIX}perm-1")
        with self._as_operator():
            with self.assertRaises(frappe.PermissionError):
                push_one_product(item_code=item.item_code)

    def test_push_lifecycle_refuses_operator(self) -> None:
        item = _make_item(f"{PREFIX}perm-2")
        with self._as_operator():
            with self.assertRaises(frappe.PermissionError):
                push_lifecycle_product(item_code=item.item_code)

    def test_push_all_pending_refuses_operator(self) -> None:
        account = _account_with_auto_push(auto_push=0)
        with self._as_operator():
            with self.assertRaises(frappe.PermissionError):
                push_all_pending_products(account=account)


# ============================================================
# 3. Whitelist clean-error returns
# ============================================================


class TestWhitelistCleanErrorReturns(FrappeTestCase):
    """The whitelists should NEVER raise through the boundary on
    user-facing errors (missing item, account ambiguity, etc.) —
    they return {ok: False, message: ...} so the JS handler can
    render a clean dialog."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_push_one_product_missing_item_returns_ok_false(self) -> None:
        result = push_one_product(item_code=f"{PREFIX}NOT-A-REAL-ITEM")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())

    def test_push_one_product_empty_item_returns_ok_false(self) -> None:
        result = push_one_product(item_code="")
        self.assertFalse(result["ok"])

    def test_push_all_pending_missing_account_returns_ok_false(self) -> None:
        result = push_all_pending_products(account="")
        self.assertFalse(result["ok"])


# ----- Internal helpers -----


@contextmanager
def _set_user(email: str):
    """Switch session user for the duration of the with-block."""
    prior = frappe.session.user
    frappe.set_user(email)
    try:
        yield
    finally:
        frappe.set_user(prior)
