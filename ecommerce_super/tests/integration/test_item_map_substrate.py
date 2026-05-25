"""Stage 1 tests for §8d — EasyEcom Item Map + item_master_mode flag.

NO EASYECOM CALLS in this test module — Stage 1 is local-only (a
DocType + a flag). Stage 2/3 will introduce HTTP-mocked tests; Stage
3's push is the only stage that mutates EE, and per the packet those
tests will be gated separately. Stage 1 is safe to run against any
site at any time.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.item_master_mode import flip_to_erpnext_mastered
from ecommerce_super.easyecom.doctype.easyecom_item_map.easyecom_item_map import (
    ALLOWED_LINK_DOCTYPES,
    VALID_STATUS_VALUES,
)
from ecommerce_super.tests.factories import make_account


# Test SKUs use a marker prefix so cleanup is targeted (the ee_sku
# UNIQUE constraint means a leak from one test poisons others).
PREFIX = "TEST-8D-S1-"


def _wipe_item_maps(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Item Map",
        filters={"ee_sku": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Item Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _ensure_item_group(name: str = "All Item Groups") -> str:
    if frappe.db.exists("Item Group", name):
        return name
    grp = frappe.new_doc("Item Group")
    grp.update({"item_group_name": name, "is_group": 1})
    grp.insert(ignore_permissions=True)
    return grp.name


def _ensure_uom(name: str = "Nos") -> str:
    if frappe.db.exists("UOM", name):
        return name
    u = frappe.new_doc("UOM")
    u.update({"uom_name": name, "must_be_whole_number": 1})
    u.insert(ignore_permissions=True)
    return u.name


def _ensure_hsn(code: str = "99999999") -> str:
    """India Compliance makes gst_hsn_code mandatory on Item via a
    custom validator. Seed a generic HSN row so tests don't depend on
    the production HSN library being loaded."""
    if frappe.db.exists("GST HSN Code", code):
        return code
    hsn = frappe.new_doc("GST HSN Code")
    hsn.update({"hsn_code": code, "description": "Test HSN"})
    hsn.insert(ignore_permissions=True)
    return code


def _ensure_test_item(item_code: str) -> str:
    """Ensure an Item with this item_code exists. Returns the docname."""
    if frappe.db.exists("Item", item_code):
        return item_code
    group = _ensure_item_group()
    uom = _ensure_uom()
    hsn = _ensure_hsn()
    item = frappe.new_doc("Item")
    item.update(
        {
            "item_code": item_code,
            "item_name": item_code,
            "item_group": group,
            "stock_uom": uom,
            "gst_hsn_code": hsn,
        }
    )
    item.insert(ignore_permissions=True)
    return item.name


def _ensure_test_bundle(item_code: str) -> str:
    """Ensure a Product Bundle exists whose new_item_code is `item_code`.
    Creates a NON-stock wrapper Item (ERPNext refuses to bundle a Stock
    Item) and adds 2 component Items. Returns the Bundle's docname."""
    if frappe.db.exists("Product Bundle", item_code):
        return item_code
    # Wrapper must be non-stock: ERPNext's Product Bundle validator
    # rejects stock items as the parent.
    if not frappe.db.exists("Item", item_code):
        group = _ensure_item_group()
        uom = _ensure_uom()
        hsn = _ensure_hsn()
        wrapper_doc = frappe.new_doc("Item")
        wrapper_doc.update(
            {
                "item_code": item_code,
                "item_name": item_code,
                "item_group": group,
                "stock_uom": uom,
                "gst_hsn_code": hsn,
                "is_stock_item": 0,
            }
        )
        wrapper_doc.insert(ignore_permissions=True)
    wrapper = item_code
    comp_a = _ensure_test_item(f"{item_code}-COMP-A")
    comp_b = _ensure_test_item(f"{item_code}-COMP-B")
    bundle = frappe.new_doc("Product Bundle")
    bundle.update({"new_item_code": wrapper})
    bundle.append("items", {"item_code": comp_a, "qty": 1})
    bundle.append("items", {"item_code": comp_b, "qty": 1})
    bundle.insert(ignore_permissions=True)
    return bundle.name


class TestItemMapSchema(FrappeTestCase):
    """The DocType exists, the status enum is the expected set, and
    the link target is restricted to {Item, Product Bundle}."""

    def test_doctype_exists(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom Item Map"))

    def test_status_enum_is_packet_spec(self) -> None:
        """§8.1.9 enumerates Mapped / Created-Flagged / Flagged-Not-Created
        / Drift / Disabled. Lock that set in code."""
        meta = frappe.get_meta("EasyEcom Item Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(opts, VALID_STATUS_VALUES)

    def test_allowed_link_doctypes_constant(self) -> None:
        self.assertEqual(ALLOWED_LINK_DOCTYPES, frozenset({"Item", "Product Bundle"}))

    def test_ee_sku_field_is_unique(self) -> None:
        meta = frappe.get_meta("EasyEcom Item Map")
        self.assertTrue(meta.get_field("ee_sku").unique)
        self.assertTrue(meta.get_field("ee_sku").reqd)


class TestItemMapCrud(FrappeTestCase):
    """Map row CRUD against both Item and Product Bundle targets."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.item_code = f"{PREFIX}WIDGET-1"
        _ensure_test_item(cls.item_code)
        cls.bundle_code = f"{PREFIX}KIT-1"
        _ensure_test_bundle(cls.bundle_code)

    def setUp(self) -> None:
        _wipe_item_maps(PREFIX)

    def tearDown(self) -> None:
        _wipe_item_maps(PREFIX)

    def test_create_map_linking_an_item(self) -> None:
        doc = frappe.new_doc("EasyEcom Item Map")
        doc.update(
            {
                "ee_sku": f"{PREFIX}SKU-ITEM-1",
                "erpnext_doctype": "Item",
                "erpnext_name": self.item_code,
                "ee_product_id": "12345",
                "ee_cp_id": "67890",
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        # Re-load: the dynamic link round-trips cleanly.
        reloaded = frappe.get_doc("EasyEcom Item Map", doc.name)
        self.assertEqual(reloaded.erpnext_doctype, "Item")
        self.assertEqual(reloaded.erpnext_name, self.item_code)
        self.assertEqual(reloaded.ee_product_id, "12345")
        self.assertEqual(reloaded.status, "Mapped")

    def test_create_map_linking_a_product_bundle(self) -> None:
        """The dual-object link MUST handle a Product Bundle target.
        Stage 4 bundle component-resolution depends on this."""
        doc = frappe.new_doc("EasyEcom Item Map")
        doc.update(
            {
                "ee_sku": f"{PREFIX}SKU-BUNDLE-1",
                "erpnext_doctype": "Product Bundle",
                "erpnext_name": self.bundle_code,
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Item Map", doc.name)
        self.assertEqual(reloaded.erpnext_doctype, "Product Bundle")
        self.assertEqual(reloaded.erpnext_name, self.bundle_code)

    def test_unmapped_row_is_allowed(self) -> None:
        """A Flagged-Not-Created row may have no link target — the EE
        SKU exists but no ERPNext-side row was created."""
        doc = frappe.new_doc("EasyEcom Item Map")
        doc.update(
            {
                "ee_sku": f"{PREFIX}SKU-FLAGGED-1",
                "status": "Flagged-Not-Created",
                "flag_reason": "unsupported product_type=variant_parent",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Item Map", doc.name)
        self.assertFalse(reloaded.erpnext_doctype)
        self.assertFalse(reloaded.erpnext_name)
        self.assertEqual(reloaded.status, "Flagged-Not-Created")

    def test_link_to_wrong_doctype_rejected(self) -> None:
        """A row with erpnext_doctype=User (not Item / Product Bundle)
        must be refused — only the two object types make sense."""
        doc = frappe.new_doc("EasyEcom Item Map")
        doc.update(
            {
                "ee_sku": f"{PREFIX}SKU-BAD-TYPE",
                "erpnext_doctype": "User",
                "erpnext_name": "Administrator",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_broken_link_rejected(self) -> None:
        """Set erpnext_name to a docname that doesn't exist."""
        doc = frappe.new_doc("EasyEcom Item Map")
        doc.update(
            {
                "ee_sku": f"{PREFIX}SKU-BROKEN-LINK",
                "erpnext_doctype": "Item",
                "erpnext_name": "NOT-A-REAL-ITEM-CODE-XYZZY",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_update_status_to_drift(self) -> None:
        doc = frappe.new_doc("EasyEcom Item Map")
        doc.update(
            {
                "ee_sku": f"{PREFIX}SKU-DRIFT-1",
                "erpnext_doctype": "Item",
                "erpnext_name": self.item_code,
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        # Simulate the §8.1.8 post-flip drift detector flagging this row.
        doc.status = "Drift"
        doc.flag_reason = "EE-side edit to product_name after onboarding flip"
        doc.save(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Item Map", doc.name)
        self.assertEqual(reloaded.status, "Drift")
        self.assertIn("EE-side edit", reloaded.flag_reason)


class TestEeSkuUnique(FrappeTestCase):
    """DB UNIQUE on ee_sku — two rows can't carry the same SKU
    (§8.1.2 natural key). Frappe maps `unique: 1` on a Data field to
    a column UNIQUE constraint at the DB level."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.item_code = f"{PREFIX}WIDGET-DUP"
        _ensure_test_item(cls.item_code)

    def setUp(self) -> None:
        _wipe_item_maps(PREFIX)

    def tearDown(self) -> None:
        _wipe_item_maps(PREFIX)

    def test_duplicate_sku_rejected(self) -> None:
        doc1 = frappe.new_doc("EasyEcom Item Map")
        doc1.update(
            {
                "ee_sku": f"{PREFIX}DUPE-1",
                "erpnext_doctype": "Item",
                "erpnext_name": self.item_code,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        # Second row with same ee_sku.
        doc2 = frappe.new_doc("EasyEcom Item Map")
        doc2.update(
            {
                "ee_sku": f"{PREFIX}DUPE-1",
                "erpnext_doctype": "Item",
                "erpnext_name": self.item_code,
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

    def test_duplicate_sku_via_raw_sql_rejected(self) -> None:
        """Belt-and-braces: bypass the Frappe validate layer with raw
        SQL. The DB UNIQUE index must still refuse the second insert."""
        doc1 = frappe.new_doc("EasyEcom Item Map")
        doc1.update(
            {
                "ee_sku": f"{PREFIX}DUPE-SQL-1",
                "erpnext_doctype": "Item",
                "erpnext_name": self.item_code,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        frappe.db.commit()
        # Attempt to INSERT a row with the same ee_sku directly via SQL.
        with self.assertRaises(Exception):
            frappe.db.sql(
                """INSERT INTO `tabEasyEcom Item Map`
                   (name, ee_sku, status, creation, modified, modified_by, owner)
                   VALUES (%s, %s, %s, NOW(), NOW(), %s, %s)""",
                (
                    f"ECS-ITM-{PREFIX}DUPE-SQL-DUP",
                    f"{PREFIX}DUPE-SQL-1",  # same SKU as doc1
                    "Mapped",
                    "Administrator",
                    "Administrator",
                ),
            )


class TestItemMasterModeDefaultAndFlip(FrappeTestCase):
    """The mode flag defaults to onboarding; the flip endpoint switches
    to erpnext_mastered with a timestamp."""

    ACCOUNT_NAME = "test-8d-stage1"

    def setUp(self) -> None:
        # cleanup_easyecom_state was wiping all accounts in tests; create
        # afresh for this test class.
        if not frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            make_account(name=self.ACCOUNT_NAME)
        # Reset the flag to onboarding for each test.
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {"item_master_mode": "onboarding", "item_master_flipped_at": None},
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
            "EasyEcom Account", self.ACCOUNT_NAME, "item_master_mode"
        )
        self.assertEqual(mode, "onboarding")

    def test_flip_requires_explicit_confirm(self) -> None:
        result = flip_to_erpnext_mastered(account=self.ACCOUNT_NAME, confirm=False)
        self.assertFalse(result["ok"])
        # Mode unchanged.
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Account", self.ACCOUNT_NAME, "item_master_mode"
            ),
            "onboarding",
        )

    def test_flip_with_confirm_switches_mode_and_stamps_time(self) -> None:
        result = flip_to_erpnext_mastered(account=self.ACCOUNT_NAME, confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "erpnext_mastered")
        # Persisted.
        row = frappe.db.get_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            ["item_master_mode", "item_master_flipped_at"],
            as_dict=True,
        )
        self.assertEqual(row.item_master_mode, "erpnext_mastered")
        self.assertIsNotNone(row.item_master_flipped_at)

    def test_flip_when_already_flipped_returns_clean_refusal(self) -> None:
        # First flip.
        flip_to_erpnext_mastered(account=self.ACCOUNT_NAME, confirm=True)
        # Second flip — should refuse cleanly.
        second = flip_to_erpnext_mastered(account=self.ACCOUNT_NAME, confirm=True)
        self.assertFalse(second["ok"])
        self.assertIn("already", second["message"].lower())

    def test_flip_on_nonexistent_account_returns_clean_refusal(self) -> None:
        result = flip_to_erpnext_mastered(account="no-such-account", confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())

    def test_flip_rejected_for_operator_role(self) -> None:
        """EasyEcom Operator is read-only; the flip endpoint must refuse."""
        email = "operator-8d@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        u = frappe.new_doc("User")
        u.update(
            {"email": email, "first_name": "Op", "send_welcome_email": 0, "enabled": 1}
        )
        u.insert(ignore_permissions=True)
        u.append("roles", {"role": "EasyEcom Operator"})
        u.save(ignore_permissions=True)
        frappe.db.commit()
        original_user = frappe.session.user
        frappe.set_user(email)
        try:
            with self.assertRaises(frappe.PermissionError):
                flip_to_erpnext_mastered(account=self.ACCOUNT_NAME, confirm=True)
        finally:
            frappe.set_user(original_user)
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
            frappe.db.commit()
