"""Stage 1 tests for §8e — EasyEcom Customer Map + customer_master_mode flag.

NO EASYECOM CALLS in this test module — Stage 1 is local-only (a
DocType + a flag). Stage 2/3+ will introduce HTTP-mocked tests; Stage
4's push is the only stage that mutates EE, and per the packet those
tests will be gated separately. Stage 1 is safe to run against any
site at any time.

Mirrors test_item_map_substrate.py exactly. Same shape, same coverage:
schema invariants, CRUD against the allowed link, link-doctype refusal,
broken-link rejection, status-change to Drift, DB UNIQUE on the natural
key, and the flip endpoint contract (default mode, confirm-required,
flips + stamps timestamp, refuses re-flip, refuses unknown account,
refuses Operator role).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.customer_master_mode import (
    flip_to_erpnext_mastered_customers,
)
from ecommerce_super.easyecom.doctype.easyecom_customer_map.easyecom_customer_map import (
    ALLOWED_LINK_DOCTYPES,
    VALID_STATUS_VALUES,
)
from ecommerce_super.tests.factories import make_account


# Test c_ids use a marker prefix so cleanup is targeted (the ee_c_id
# UNIQUE constraint means a leak from one test poisons others).
PREFIX = "TEST-8E-S1-"


def _wipe_customer_maps(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Customer Map",
        filters={"ee_c_id": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Customer Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _ensure_customer_group() -> str:
    """ERPNext usually ships 'All Customer Groups' (group) + leaf children
    like 'Commercial'. Find the first leaf, or create one."""
    leaf = frappe.db.get_value(
        "Customer Group", {"is_group": 0}, "name"
    )
    if leaf:
        return leaf
    # Need a parent group; ensure 'All Customer Groups' (root) exists, then
    # add a leaf under it.
    if not frappe.db.exists("Customer Group", "All Customer Groups"):
        root = frappe.new_doc("Customer Group")
        root.update(
            {"customer_group_name": "All Customer Groups", "is_group": 1}
        )
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Customer Group")
    leaf_doc.update(
        {
            "customer_group_name": "TEST-8E-Wholesale",
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name


def _ensure_territory() -> str:
    leaf = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if leaf:
        return leaf
    if not frappe.db.exists("Territory", "All Territories"):
        root = frappe.new_doc("Territory")
        root.update({"territory_name": "All Territories", "is_group": 1})
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Territory")
    leaf_doc.update(
        {
            "territory_name": "TEST-8E-Territory",
            "parent_territory": "All Territories",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name


def _ensure_test_customer(customer_name: str) -> str:
    """Ensure a Customer with this customer_name exists. Returns the
    AUTO-GENERATED docname (ERPNext uses a `CUST-YYYY-NNNNN` series, NOT
    customer_name as the docname). Callers must capture the returned
    name and use it as erpnext_name on the map row."""
    # Re-use existing Customer with this customer_name if present.
    existing = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
    if existing:
        return existing
    cust = frappe.new_doc("Customer")
    cust.update(
        {
            "customer_name": customer_name,
            "customer_type": "Company",
            "customer_group": _ensure_customer_group(),
            "territory": _ensure_territory(),
        }
    )
    cust.insert(ignore_permissions=True)
    return cust.name


def _cleanup_test_customer_by_name(customer_name: str) -> None:
    """Delete the test Customer by customer_name (the docname is the
    auto-generated CUST-YYYY-NNNNN series)."""
    docname = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
    if not docname:
        return
    try:
        frappe.delete_doc("Customer", docname, force=True, ignore_permissions=True)
    except Exception:
        pass
    frappe.db.commit()


def _cleanup_test_customer(customer_code: str) -> None:
    """Legacy alias — kept for tests that use it explicitly."""
    _cleanup_test_customer_by_name(customer_code)


class TestCustomerMapSchema(FrappeTestCase):
    """The DocType exists, the status enum is the expected set, and
    the link target is restricted to {Customer}."""

    def test_doctype_exists(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom Customer Map"))

    def test_status_enum_is_packet_spec(self) -> None:
        """§8.2 — same five values as Item Map (the enum is entity-agnostic)."""
        meta = frappe.get_meta("EasyEcom Customer Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(opts, VALID_STATUS_VALUES)

    def test_allowed_link_doctypes_constant(self) -> None:
        self.assertEqual(ALLOWED_LINK_DOCTYPES, frozenset({"Customer"}))

    def test_ee_c_id_field_is_unique_and_required(self) -> None:
        meta = frappe.get_meta("EasyEcom Customer Map")
        self.assertTrue(meta.get_field("ee_c_id").unique)
        self.assertTrue(meta.get_field("ee_c_id").reqd)

    def test_ee_customer_id_field_exists_separately(self) -> None:
        """ee_c_id is the read-side id (join key, unique); ee_customer_id
        is the write-side id. Both fields exist on the schema — parity
        check is deferred to Stage 3 live verification."""
        meta = frappe.get_meta("EasyEcom Customer Map")
        self.assertIsNotNone(meta.get_field("ee_customer_id"))

    def test_reuses_item_map_drift_child_doctypes(self) -> None:
        """Generic-enough — both drift child DocTypes carry field+value
        pairs with no Item-specific logic, so they're reused here to
        avoid duplication (8e inventory step finding)."""
        meta = frappe.get_meta("EasyEcom Customer Map")
        self.assertEqual(
            meta.get_field("drift_fields").options,
            "EasyEcom Item Map Drift Field",
        )
        self.assertEqual(
            meta.get_field("ecs_drift_exclude_fields").options,
            "EasyEcom Item Map Exclude Field",
        )


class TestCustomerMapCrud(FrappeTestCase):
    """Map row CRUD against a Customer target."""

    customer_name = f"{PREFIX}WHOLESALE-1"

    def setUp(self) -> None:
        # FrappeTestCase rolls back per-test; insert customer here.
        # ERPNext auto-generates Customer docnames (CUST-YYYY-NNNNN),
        # so capture the returned name — it's NOT customer_name.
        self.customer_docname = _ensure_test_customer(self.customer_name)
        _wipe_customer_maps(PREFIX)

    def tearDown(self) -> None:
        _wipe_customer_maps(PREFIX)
        _cleanup_test_customer_by_name(self.customer_name)

    def test_create_map_linking_a_customer(self) -> None:
        doc = frappe.new_doc("EasyEcom Customer Map")
        doc.update(
            {
                "ee_c_id": f"{PREFIX}CID-1",
                "erpnext_doctype": "Customer",
                "erpnext_name": self.customer_docname,
                "ee_customer_id": f"{PREFIX}CID-1",
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Customer Map", doc.name)
        self.assertEqual(reloaded.erpnext_doctype, "Customer")
        self.assertEqual(reloaded.erpnext_name, self.customer_docname)
        self.assertEqual(reloaded.ee_customer_id, f"{PREFIX}CID-1")
        self.assertEqual(reloaded.status, "Mapped")
        # autoname format
        self.assertEqual(doc.name, f"ECS-CUST-{PREFIX}CID-1")

    def test_unmapped_row_is_allowed(self) -> None:
        """A Flagged-Not-Created row may have no link target — the EE
        customer was rejected by India Compliance (invalid GSTIN) and
        no ERPNext Customer was created."""
        doc = frappe.new_doc("EasyEcom Customer Map")
        doc.update(
            {
                "ee_c_id": f"{PREFIX}CID-FNC",
                "status": "Flagged-Not-Created",
                "flag_reason": "India Compliance rejected GSTIN format",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Customer Map", doc.name)
        self.assertFalse(reloaded.erpnext_doctype)
        self.assertFalse(reloaded.erpnext_name)
        self.assertEqual(reloaded.status, "Flagged-Not-Created")

    def test_link_to_wrong_doctype_rejected(self) -> None:
        """A row with erpnext_doctype=User (not Customer) must be refused."""
        doc = frappe.new_doc("EasyEcom Customer Map")
        doc.update(
            {
                "ee_c_id": f"{PREFIX}CID-BAD-TYPE",
                "erpnext_doctype": "User",
                "erpnext_name": "Administrator",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_link_to_item_rejected(self) -> None:
        """Specifically — Item is NOT a valid Customer Map target.
        (Future entity-sync flows for Supplier / Address would each get
        their own Map DocType, not be merged into this one.)"""
        doc = frappe.new_doc("EasyEcom Customer Map")
        doc.update(
            {
                "ee_c_id": f"{PREFIX}CID-BAD-ITEM",
                "erpnext_doctype": "Item",
                "erpnext_name": "anything",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_broken_link_rejected(self) -> None:
        """Set erpnext_name to a Customer that doesn't exist."""
        doc = frappe.new_doc("EasyEcom Customer Map")
        doc.update(
            {
                "ee_c_id": f"{PREFIX}CID-BROKEN-LINK",
                "erpnext_doctype": "Customer",
                "erpnext_name": "NOT-A-REAL-CUSTOMER-XYZZY",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_update_status_to_drift(self) -> None:
        doc = frappe.new_doc("EasyEcom Customer Map")
        doc.update(
            {
                "ee_c_id": f"{PREFIX}CID-DRIFT",
                "erpnext_doctype": "Customer",
                "erpnext_name": self.customer_docname,
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        # Simulate the Stage 5 post-flip drift detector flagging this row.
        doc.status = "Drift"
        doc.flag_reason = "EE-side edit to companyname after onboarding flip"
        doc.save(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Customer Map", doc.name)
        self.assertEqual(reloaded.status, "Drift")
        self.assertIn("EE-side edit", reloaded.flag_reason)


class TestEeCIdUnique(FrappeTestCase):
    """DB UNIQUE on ee_c_id — two rows can't carry the same c_id
    (§8.2 natural key)."""

    customer_name = f"{PREFIX}WHOLESALE-DUP"

    def setUp(self) -> None:
        self.customer_docname = _ensure_test_customer(self.customer_name)
        _wipe_customer_maps(PREFIX)

    def tearDown(self) -> None:
        _wipe_customer_maps(PREFIX)
        _cleanup_test_customer_by_name(self.customer_name)

    def test_duplicate_c_id_rejected(self) -> None:
        doc1 = frappe.new_doc("EasyEcom Customer Map")
        doc1.update(
            {
                "ee_c_id": f"{PREFIX}DUPE-1",
                "erpnext_doctype": "Customer",
                "erpnext_name": self.customer_docname,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        doc2 = frappe.new_doc("EasyEcom Customer Map")
        doc2.update(
            {
                "ee_c_id": f"{PREFIX}DUPE-1",
                "erpnext_doctype": "Customer",
                "erpnext_name": self.customer_docname,
                "status": "Mapped",
            }
        )
        with self.assertRaises(
            (
                frappe.DuplicateEntryError,
                frappe.exceptions.UniqueValidationError,
                frappe.ValidationError,
            )
        ):
            doc2.insert(ignore_permissions=True)

    def test_duplicate_c_id_via_raw_sql_rejected(self) -> None:
        """Belt-and-braces: bypass Frappe validate via raw SQL. The DB
        UNIQUE index must still refuse the second insert."""
        doc1 = frappe.new_doc("EasyEcom Customer Map")
        doc1.update(
            {
                "ee_c_id": f"{PREFIX}DUPE-SQL-1",
                "erpnext_doctype": "Customer",
                "erpnext_name": self.customer_docname,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        frappe.db.commit()
        with self.assertRaises(Exception):
            frappe.db.sql(
                """INSERT INTO `tabEasyEcom Customer Map`
                   (name, ee_c_id, status, creation, modified, modified_by, owner)
                   VALUES (%s, %s, %s, NOW(), NOW(), %s, %s)""",
                (
                    f"ECS-CUST-{PREFIX}DUPE-SQL-DUP",
                    f"{PREFIX}DUPE-SQL-1",  # same c_id as doc1
                    "Mapped",
                    "Administrator",
                    "Administrator",
                ),
            )


class TestCustomerMasterModeDefaultAndFlip(FrappeTestCase):
    """The mode flag defaults to onboarding; the flip endpoint switches
    to erpnext_mastered with a timestamp."""

    ACCOUNT_NAME = "test-8e-stage1"

    def setUp(self) -> None:
        if not frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            # enabled=False because the live site has another enabled
            # Account (Harmony) and the single-Account-enabled validator
            # (§8.1) would refuse a second enabled row. Stage-1 substrate
            # tests don't need the account active — just to exist.
            make_account(name=self.ACCOUNT_NAME, enabled=False)
        # Reset the flag to onboarding for each test.
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {
                "customer_master_mode": "onboarding",
                "customer_master_flipped_at": None,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        if frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            try:
                frappe.delete_doc(
                    "EasyEcom Account",
                    self.ACCOUNT_NAME,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass
            frappe.db.commit()

    def test_mode_defaults_to_onboarding(self) -> None:
        mode = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "customer_master_mode"
        )
        self.assertEqual(mode, "onboarding")

    def test_flip_requires_explicit_confirm(self) -> None:
        result = flip_to_erpnext_mastered_customers(
            account=self.ACCOUNT_NAME, confirm=False
        )
        self.assertFalse(result["ok"])
        # Mode unchanged.
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Account", self.ACCOUNT_NAME, "customer_master_mode"
            ),
            "onboarding",
        )

    def test_flip_with_confirm_switches_mode_and_stamps_time(self) -> None:
        result = flip_to_erpnext_mastered_customers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "erpnext_mastered")
        row = frappe.db.get_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            ["customer_master_mode", "customer_master_flipped_at"],
            as_dict=True,
        )
        self.assertEqual(row.customer_master_mode, "erpnext_mastered")
        self.assertIsNotNone(row.customer_master_flipped_at)

    def test_flip_is_independent_of_item_flip(self) -> None:
        """Flipping Customer master must NOT flip Item master.
        They're independent switches on the same Account."""
        item_mode_before = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "item_master_mode"
        )
        flip_to_erpnext_mastered_customers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        item_mode_after = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "item_master_mode"
        )
        self.assertEqual(item_mode_before, item_mode_after)

    def test_flip_when_already_flipped_returns_clean_refusal(self) -> None:
        flip_to_erpnext_mastered_customers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        second = flip_to_erpnext_mastered_customers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertFalse(second["ok"])
        self.assertIn("already", second["message"].lower())

    def test_flip_on_nonexistent_account_returns_clean_refusal(self) -> None:
        result = flip_to_erpnext_mastered_customers(
            account="no-such-account-8e", confirm=True
        )
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())

    def test_flip_rejected_for_operator_role(self) -> None:
        """EasyEcom Operator is read-only; the flip endpoint must refuse."""
        email = "operator-8e@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        u = frappe.new_doc("User")
        u.update(
            {
                "email": email,
                "first_name": "Op",
                "send_welcome_email": 0,
                "enabled": 1,
            }
        )
        u.insert(ignore_permissions=True)
        u.append("roles", {"role": "EasyEcom Operator"})
        u.save(ignore_permissions=True)
        frappe.db.commit()
        original_user = frappe.session.user
        frappe.set_user(email)
        try:
            with self.assertRaises(frappe.PermissionError):
                flip_to_erpnext_mastered_customers(
                    account=self.ACCOUNT_NAME, confirm=True
                )
        finally:
            frappe.set_user(original_user)
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
            frappe.db.commit()
