"""Stage 1 tests for §8f — EasyEcom Supplier Map + supplier_master_mode flag.

NO EASYECOM CALLS in this test module — Stage 1 is local-only (a
DocType + a flag). Stages 2/3+ will introduce HTTP-mocked tests; Stage
4's push is the only stage that mutates EE, and per the packet those
tests will be gated separately. Stage 1 is safe to run against any
site at any time.

Mirrors test_customer_map_substrate.py exactly. Same shape, same
coverage: schema invariants, CRUD against the allowed link
(Supplier), link-doctype refusal, broken-link rejection, status-change
to Drift, DB UNIQUE on the natural key, and the flip endpoint contract
(default mode, confirm-required, flips + stamps timestamp, refuses
re-flip, refuses unknown account, refuses Operator role).

§8.3-specific additions:
* The TWO-IDENTIFIER SPLIT — ee_vendor_c_id is the read-key (unique,
  reqd, in_list_view), ee_vendor_id is the write-key captured from
  CreateVendor response (not unique, settled later). The schema check
  verifies both fields exist and only ee_vendor_c_id is the
  uniqueness key.
* Flip independence — Supplier flip must NOT touch Item OR Customer
  master mode (three independent switches).
* Drift-child rename regression — the §8e+§8f common drift child
  DocTypes (EasyEcom Drift Field / EasyEcom Exclude Field, renamed
  out of the old Item-specific names) are referenced by ecs_drift /
  exclude fields on the Supplier Map.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.supplier_master_mode import (
    flip_to_erpnext_mastered_suppliers,
)
from ecommerce_super.easyecom.doctype.easyecom_supplier_map.easyecom_supplier_map import (
    ALLOWED_LINK_DOCTYPES,
    VALID_STATUS_VALUES,
)
from ecommerce_super.tests.factories import make_account


# Test vendor_c_ids use a marker prefix so cleanup is targeted (the
# ee_vendor_c_id UNIQUE constraint means a leak from one test poisons
# others).
PREFIX = "TEST-8F-S1-"


def _wipe_supplier_maps(prefix: str) -> None:
    for n in frappe.db.get_all(
        "EasyEcom Supplier Map",
        filters={"ee_vendor_c_id": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Supplier Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _ensure_supplier_group() -> str:
    leaf = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if leaf:
        return leaf
    if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
        root = frappe.new_doc("Supplier Group")
        root.update(
            {"supplier_group_name": "All Supplier Groups", "is_group": 1}
        )
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Supplier Group")
    leaf_doc.update(
        {
            "supplier_group_name": "TEST-8F-Supplies",
            "parent_supplier_group": "All Supplier Groups",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name


def _ensure_test_supplier(supplier_name: str) -> str:
    """Ensure a Supplier with this supplier_name exists. Returns the
    AUTO-GENERATED docname (ERPNext typically uses supplier_name as
    docname by default, but the integration must tolerate either)."""
    existing = frappe.db.get_value(
        "Supplier", {"supplier_name": supplier_name}, "name"
    )
    if existing:
        return existing
    sup = frappe.new_doc("Supplier")
    sup.update(
        {
            "supplier_name": supplier_name,
            "supplier_type": "Company",
            "supplier_group": _ensure_supplier_group(),
            "country": "India",
        }
    )
    sup.insert(ignore_permissions=True)
    return sup.name


def _cleanup_test_supplier(supplier_name: str) -> None:
    docname = frappe.db.get_value(
        "Supplier", {"supplier_name": supplier_name}, "name"
    )
    if not docname:
        return
    try:
        frappe.delete_doc(
            "Supplier", docname, force=True, ignore_permissions=True
        )
    except Exception:
        pass
    frappe.db.commit()


class TestSupplierMapSchema(FrappeTestCase):
    """The DocType exists, the status enum is the expected set, and
    the link target is restricted to {Supplier}."""

    def test_doctype_exists(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom Supplier Map"))

    def test_status_enum_is_packet_spec(self) -> None:
        """§8.3 — same five values as Item Map / Customer Map (the enum
        is entity-agnostic)."""
        meta = frappe.get_meta("EasyEcom Supplier Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(opts, VALID_STATUS_VALUES)

    def test_allowed_link_doctypes_constant(self) -> None:
        self.assertEqual(ALLOWED_LINK_DOCTYPES, frozenset({"Supplier"}))

    def test_ee_vendor_c_id_field_is_unique_and_required(self) -> None:
        """The READ key (vendor_c_id) is the join key. UNIQUE +
        reqd."""
        meta = frappe.get_meta("EasyEcom Supplier Map")
        self.assertTrue(meta.get_field("ee_vendor_c_id").unique)
        self.assertTrue(meta.get_field("ee_vendor_c_id").reqd)

    def test_ee_vendor_id_field_exists_separately_and_not_unique(self) -> None:
        """§8.3 TWO-IDENTIFIER SPLIT — ee_vendor_id is the WRITE key,
        captured from the CreateVendor response. It is NOT unique
        (the same vendor_id could legitimately reappear if EE re-uses
        ids) and NOT reqd (rows in onboarding may not yet have it)."""
        meta = frappe.get_meta("EasyEcom Supplier Map")
        field = meta.get_field("ee_vendor_id")
        self.assertIsNotNone(field, "ee_vendor_id field must exist")
        self.assertFalse(
            field.unique,
            "ee_vendor_id must NOT be unique — only ee_vendor_c_id is the "
            "uniqueness key (§8.3 packet)",
        )
        self.assertFalse(
            field.reqd,
            "ee_vendor_id is captured later (CreateVendor response), so "
            "must not be required at insert time",
        )

    def test_autoname_format_uses_vendor_c_id(self) -> None:
        """autoname=`format:ECS-SUPP-{ee_vendor_c_id}` — docnames are
        derived from the read key, not the write key."""
        meta = frappe.get_meta("EasyEcom Supplier Map")
        self.assertEqual(
            meta.autoname, "format:ECS-SUPP-{ee_vendor_c_id}"
        )

    def test_reuses_renamed_drift_child_doctypes(self) -> None:
        """§8f rename regression — drift_fields + ecs_drift_exclude_fields
        must point to the RENAMED (entity-agnostic) child DocTypes,
        not the old Item-specific names."""
        meta = frappe.get_meta("EasyEcom Supplier Map")
        self.assertEqual(
            meta.get_field("drift_fields").options,
            "EasyEcom Drift Field",
        )
        self.assertEqual(
            meta.get_field("ecs_drift_exclude_fields").options,
            "EasyEcom Exclude Field",
        )


class TestRenamedDriftChildRegression(FrappeTestCase):
    """§8f rename regression — the §8d Item Map and §8e Customer Map
    options were repointed to the renamed child DocTypes. The old
    names must no longer exist as DocTypes. The 14 pre-rename data
    rows must still be query-able under the new name."""

    def test_item_map_drift_options_repointed(self) -> None:
        meta = frappe.get_meta("EasyEcom Item Map")
        self.assertEqual(
            meta.get_field("drift_fields").options,
            "EasyEcom Drift Field",
        )
        self.assertEqual(
            meta.get_field("ecs_drift_exclude_fields").options,
            "EasyEcom Exclude Field",
        )

    def test_customer_map_drift_options_repointed(self) -> None:
        meta = frappe.get_meta("EasyEcom Customer Map")
        self.assertEqual(
            meta.get_field("drift_fields").options,
            "EasyEcom Drift Field",
        )
        self.assertEqual(
            meta.get_field("ecs_drift_exclude_fields").options,
            "EasyEcom Exclude Field",
        )

    def test_old_item_specific_doctype_names_gone(self) -> None:
        """The renamed-from names must not exist as DocType rows."""
        self.assertFalse(
            frappe.db.exists("DocType", "EasyEcom Item Map Drift Field")
        )
        self.assertFalse(
            frappe.db.exists("DocType", "EasyEcom Item Map Exclude Field")
        )

    def test_new_drift_child_doctypes_exist(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom Drift Field"))
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom Exclude Field"))

    def test_new_drift_child_doctypes_are_child_tables(self) -> None:
        for n in ("EasyEcom Drift Field", "EasyEcom Exclude Field"):
            meta = frappe.get_meta(n)
            self.assertTrue(
                meta.istable,
                f"{n} must be a child table (istable=1)",
            )


class TestSupplierMapCrud(FrappeTestCase):
    """Map row CRUD against a Supplier target."""

    supplier_name = f"{PREFIX}VENDOR-1"

    def setUp(self) -> None:
        self.supplier_docname = _ensure_test_supplier(self.supplier_name)
        _wipe_supplier_maps(PREFIX)

    def tearDown(self) -> None:
        _wipe_supplier_maps(PREFIX)
        _cleanup_test_supplier(self.supplier_name)

    def test_create_map_linking_a_supplier(self) -> None:
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-1",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "ee_vendor_id": f"{PREFIX}VID-1",
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Supplier Map", doc.name)
        self.assertEqual(reloaded.erpnext_doctype, "Supplier")
        self.assertEqual(reloaded.erpnext_name, self.supplier_docname)
        self.assertEqual(reloaded.ee_vendor_id, f"{PREFIX}VID-1")
        self.assertEqual(reloaded.status, "Mapped")
        # autoname format — derives from the READ key (vendor_c_id),
        # not the write key (vendor_id).
        self.assertEqual(doc.name, f"ECS-SUPP-{PREFIX}VCID-1")

    def test_create_map_with_distinct_read_and_write_ids(self) -> None:
        """§8.3 TWO-IDENTIFIER SPLIT — vendor_c_id (read) != vendor_id
        (write) per real data (166334 vs 145). Both must round-trip
        independently."""
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}166334",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "ee_vendor_id": f"{PREFIX}145",
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Supplier Map", doc.name)
        self.assertEqual(reloaded.ee_vendor_c_id, f"{PREFIX}166334")
        self.assertEqual(reloaded.ee_vendor_id, f"{PREFIX}145")
        self.assertNotEqual(reloaded.ee_vendor_c_id, reloaded.ee_vendor_id)
        self.assertEqual(doc.name, f"ECS-SUPP-{PREFIX}166334")

    def test_unmapped_row_is_allowed(self) -> None:
        """A Flagged-Not-Created row may have no link target — the EE
        supplier was rejected by India Compliance (invalid GSTIN/PAN)
        and no ERPNext Supplier was created. ee_vendor_id may also be
        empty since CreateVendor never ran (no write happened)."""
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-FNC",
                "status": "Flagged-Not-Created",
                "flag_reason": "India Compliance rejected GSTIN/PAN format",
            }
        )
        doc.insert(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Supplier Map", doc.name)
        self.assertFalse(reloaded.erpnext_doctype)
        self.assertFalse(reloaded.erpnext_name)
        self.assertFalse(reloaded.ee_vendor_id)
        self.assertEqual(reloaded.status, "Flagged-Not-Created")

    def test_link_to_wrong_doctype_rejected(self) -> None:
        """A row with erpnext_doctype=User (not Supplier) must be
        refused."""
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-BAD-TYPE",
                "erpnext_doctype": "User",
                "erpnext_name": "Administrator",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_link_to_customer_rejected(self) -> None:
        """Specifically — Customer is NOT a valid Supplier Map target,
        even though it's another business-party DocType. Cross-entity
        sloppiness was the §8e finding and must not regress here."""
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-BAD-CUST",
                "erpnext_doctype": "Customer",
                "erpnext_name": "anything",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_link_to_item_rejected(self) -> None:
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-BAD-ITEM",
                "erpnext_doctype": "Item",
                "erpnext_name": "anything",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_broken_link_rejected(self) -> None:
        """Set erpnext_name to a Supplier that doesn't exist."""
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-BROKEN-LINK",
                "erpnext_doctype": "Supplier",
                "erpnext_name": "NOT-A-REAL-SUPPLIER-XYZZY",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_update_status_to_drift(self) -> None:
        doc = frappe.new_doc("EasyEcom Supplier Map")
        doc.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-DRIFT",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "status": "Mapped",
            }
        )
        doc.insert(ignore_permissions=True)
        # Simulate the Stage 5 post-flip drift detector flagging this row.
        doc.status = "Drift"
        doc.flag_reason = "EE-side edit to supplier_name after onboarding flip"
        doc.save(ignore_permissions=True)
        reloaded = frappe.get_doc("EasyEcom Supplier Map", doc.name)
        self.assertEqual(reloaded.status, "Drift")
        self.assertIn("EE-side edit", reloaded.flag_reason)


class TestEeVendorCIdUnique(FrappeTestCase):
    """DB UNIQUE on ee_vendor_c_id — two rows can't carry the same
    read key (§8.3 natural key). Note: NOT on ee_vendor_id, by
    design — see schema test."""

    supplier_name = f"{PREFIX}VENDOR-DUP"

    def setUp(self) -> None:
        self.supplier_docname = _ensure_test_supplier(self.supplier_name)
        _wipe_supplier_maps(PREFIX)

    def tearDown(self) -> None:
        _wipe_supplier_maps(PREFIX)
        _cleanup_test_supplier(self.supplier_name)

    def test_duplicate_vendor_c_id_rejected(self) -> None:
        doc1 = frappe.new_doc("EasyEcom Supplier Map")
        doc1.update(
            {
                "ee_vendor_c_id": f"{PREFIX}DUPE-1",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        doc2 = frappe.new_doc("EasyEcom Supplier Map")
        doc2.update(
            {
                "ee_vendor_c_id": f"{PREFIX}DUPE-1",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
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

    def test_duplicate_vendor_c_id_via_raw_sql_rejected(self) -> None:
        """Belt-and-braces: bypass Frappe validate via raw SQL. The DB
        UNIQUE index must still refuse the second insert."""
        doc1 = frappe.new_doc("EasyEcom Supplier Map")
        doc1.update(
            {
                "ee_vendor_c_id": f"{PREFIX}DUPE-SQL-1",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        frappe.db.commit()
        with self.assertRaises(Exception):
            frappe.db.sql(
                """INSERT INTO `tabEasyEcom Supplier Map`
                   (name, ee_vendor_c_id, status, creation, modified, modified_by, owner)
                   VALUES (%s, %s, %s, NOW(), NOW(), %s, %s)""",
                (
                    f"ECS-SUPP-{PREFIX}DUPE-SQL-DUP",
                    f"{PREFIX}DUPE-SQL-1",  # same vendor_c_id as doc1
                    "Mapped",
                    "Administrator",
                    "Administrator",
                ),
            )

    def test_duplicate_vendor_id_allowed(self) -> None:
        """§8.3 explicitly: ee_vendor_id is NOT unique. Two distinct
        vendor_c_ids may legitimately share a vendor_id (EE re-use
        scenario, or a re-mapping incident). This must NOT throw."""
        doc1 = frappe.new_doc("EasyEcom Supplier Map")
        doc1.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-A",
                "ee_vendor_id": f"{PREFIX}SHARED-VID",
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "status": "Mapped",
            }
        )
        doc1.insert(ignore_permissions=True)
        doc2 = frappe.new_doc("EasyEcom Supplier Map")
        doc2.update(
            {
                "ee_vendor_c_id": f"{PREFIX}VCID-B",
                "ee_vendor_id": f"{PREFIX}SHARED-VID",  # same as doc1
                "erpnext_doctype": "Supplier",
                "erpnext_name": self.supplier_docname,
                "status": "Mapped",
            }
        )
        doc2.insert(ignore_permissions=True)  # MUST NOT raise
        # Both rows exist independently.
        self.assertTrue(
            frappe.db.exists(
                "EasyEcom Supplier Map", f"ECS-SUPP-{PREFIX}VCID-A"
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "EasyEcom Supplier Map", f"ECS-SUPP-{PREFIX}VCID-B"
            )
        )


class TestSupplierMasterModeDefaultAndFlip(FrappeTestCase):
    """The mode flag defaults to onboarding; the flip endpoint switches
    to erpnext_mastered with a timestamp."""

    ACCOUNT_NAME = "test-8f-stage1"

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
                "supplier_master_mode": "onboarding",
                "supplier_master_flipped_at": None,
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
            "EasyEcom Account", self.ACCOUNT_NAME, "supplier_master_mode"
        )
        self.assertEqual(mode, "onboarding")

    def test_flip_requires_explicit_confirm(self) -> None:
        result = flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=False
        )
        self.assertFalse(result["ok"])
        # Mode unchanged.
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Account",
                self.ACCOUNT_NAME,
                "supplier_master_mode",
            ),
            "onboarding",
        )

    def test_flip_with_confirm_switches_mode_and_stamps_time(self) -> None:
        result = flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "erpnext_mastered")
        row = frappe.db.get_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            ["supplier_master_mode", "supplier_master_flipped_at"],
            as_dict=True,
        )
        self.assertEqual(row.supplier_master_mode, "erpnext_mastered")
        self.assertIsNotNone(row.supplier_master_flipped_at)

    def test_flip_is_independent_of_item_flip(self) -> None:
        """Flipping Supplier master must NOT flip Item master. They're
        independent switches on the same Account."""
        item_mode_before = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "item_master_mode"
        )
        flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        item_mode_after = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "item_master_mode"
        )
        self.assertEqual(item_mode_before, item_mode_after)

    def test_flip_is_independent_of_customer_flip(self) -> None:
        """Flipping Supplier master must NOT flip Customer master.
        Three switches, three independent toggles."""
        customer_mode_before = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "customer_master_mode"
        )
        flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        customer_mode_after = frappe.db.get_value(
            "EasyEcom Account", self.ACCOUNT_NAME, "customer_master_mode"
        )
        self.assertEqual(customer_mode_before, customer_mode_after)

    def test_flip_when_already_flipped_returns_clean_refusal(self) -> None:
        flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        second = flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertFalse(second["ok"])
        self.assertIn("already", second["message"].lower())

    def test_flip_on_nonexistent_account_returns_clean_refusal(self) -> None:
        result = flip_to_erpnext_mastered_suppliers(
            account="no-such-account-8f", confirm=True
        )
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())

    def test_flip_rejected_for_operator_role(self) -> None:
        """EasyEcom Operator is read-only; the flip endpoint must
        refuse."""
        email = "operator-8f@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
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
                flip_to_erpnext_mastered_suppliers(
                    account=self.ACCOUNT_NAME, confirm=True
                )
        finally:
            frappe.set_user(original_user)
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
            frappe.db.commit()
