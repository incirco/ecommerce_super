"""§10 Stage 1 substrate tests — Transfer Map + IPR Link child +
internal party pairs + precheck + settings + endpoint constant +
custom field back-refs.

Stage 1 is DocTypes + schema + Internal-pair auto-create machinery +
precheck. NO flow logic, NO EE calls beyond the §8e Customer push
that runs as part of pair creation (mocked in tests).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client import endpoints
from ecommerce_super.easyecom.doctype.easyecom_transfer_map.easyecom_transfer_map import (
    VALID_EE_DOCTYPES,
    VALID_STATUS_VALUES,
)
from ecommerce_super.tests.factories import make_account


_PREFIX = "TEST-S10-S1-"


def _company() -> str:
    c = frappe.db.get_value("Company", filters={}, fieldname="name")
    if not c:
        raise RuntimeError("No Company")
    return c


# ============================================================
# Transfer Map DocType schema + permissions
# ============================================================


class TestTransferMapSchema(FrappeTestCase):
    def test_doctype_exists(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom Transfer Map"))

    def test_autoname_format(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        self.assertEqual(meta.autoname, "format:ECS-XFER-{delivery_note}")

    def test_delivery_note_link_unique_reqd(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        f = meta.get_field("delivery_note")
        self.assertEqual(f.fieldtype, "Link")
        self.assertEqual(f.options, "Delivery Note")
        self.assertTrue(f.reqd)
        self.assertTrue(f.unique)

    def test_warehouse_links_reqd(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        for fname in ("source_warehouse", "target_warehouse"):
            f = meta.get_field(fname)
            self.assertEqual(f.fieldtype, "Link")
            self.assertEqual(f.options, "Warehouse")
            self.assertTrue(f.reqd)

    def test_status_enum_matches_packet(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        expected = {
            "Mapped",
            "SI-Pending",
            "SI-Submitted",
            "EE-Pushed",
            "Partial-Received",
            "Fully-Received",
            "DN-Submitted-Locked",
            "Drift",
            "Disabled",
        }
        self.assertEqual(opts, expected)
        self.assertEqual(opts, VALID_STATUS_VALUES)

    def test_ee_doctype_enum(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        opts = set((meta.get_field("ee_doctype").options or "").split("\n"))
        self.assertEqual(opts, VALID_EE_DOCTYPES)

    def test_ee_id_fields_are_data_strings(self) -> None:
        """§10.G locks the three EE order ids as strings."""
        meta = frappe.get_meta("EasyEcom Transfer Map")
        for fn in ("ee_order_id", "ee_suborder_id", "ee_invoice_id"):
            self.assertEqual(meta.get_field(fn).fieldtype, "Data")
        # ee_po_id is Int — §9 PO path reuses int (parity with PO Map).
        self.assertEqual(meta.get_field("ee_po_id").fieldtype, "Int")

    def test_gstin_different_is_check_read_only(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        f = meta.get_field("gstin_different")
        self.assertEqual(f.fieldtype, "Check")
        self.assertTrue(f.read_only)

    def test_drift_exclude_children_use_shared_doctypes(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        self.assertEqual(
            meta.get_field("drift_fields").options, "EasyEcom Drift Field"
        )
        self.assertEqual(
            meta.get_field("exclude_fields").options, "EasyEcom Exclude Field"
        )

    def test_ipr_link_child_table(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        f = meta.get_field("internal_purchase_receipts")
        self.assertEqual(f.fieldtype, "Table")
        self.assertEqual(f.options, "EasyEcom Transfer IPR Link")


class TestTransferIPRLinkChild(FrappeTestCase):
    def test_doctype_exists_as_child(self) -> None:
        self.assertTrue(
            frappe.db.exists("DocType", "EasyEcom Transfer IPR Link")
        )
        meta = frappe.get_meta("EasyEcom Transfer IPR Link")
        self.assertTrue(meta.istable)

    def test_single_link_field(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer IPR Link")
        f = meta.get_field("internal_purchase_receipt")
        self.assertEqual(f.fieldtype, "Link")
        self.assertEqual(f.options, "Purchase Receipt")
        self.assertTrue(f.reqd)


class TestTransferMapPermissions(FrappeTestCase):
    def test_perms_mirror_po_map(self) -> None:
        meta = frappe.get_meta("EasyEcom Transfer Map")
        by_role = {p.role: p for p in meta.permissions}
        self.assertIn("EasyEcom Operator", by_role)
        self.assertTrue(by_role["EasyEcom Operator"].read)
        self.assertFalse(by_role["EasyEcom Operator"].write)
        self.assertIn("EasyEcom FDE", by_role)
        self.assertTrue(by_role["EasyEcom FDE"].write)
        self.assertTrue(by_role["EasyEcom FDE"].create)
        self.assertFalse(
            int(by_role["EasyEcom FDE"].delete or 0),
            "FDE should not have delete on Transfer Map",
        )
        self.assertIn("EasyEcom System Manager", by_role)
        self.assertTrue(by_role["EasyEcom System Manager"].delete)


# ============================================================
# Transfer Map validate guards
# ============================================================


def _ensure_warehouse(name: str, *, company: str | None = None) -> str:
    company = company or _company()
    existing = frappe.db.get_value(
        "Warehouse", {"warehouse_name": name, "company": company}, "name"
    )
    if existing:
        return existing
    w = frappe.new_doc("Warehouse")
    w.update({"warehouse_name": name, "company": company, "is_group": 0})
    w.insert(ignore_permissions=True)
    return w.name


def _make_internal_dn(
    *,
    customer: str,
    source_wh: str,
    item: str,
    qty: int = 1,
    target_wh: str | None = None,
) -> str:
    """ERPNext requires `target_warehouse` on every line of an
    internal-customer DN (the destination Company's warehouse). If the
    caller doesn't supply one, use a sibling test warehouse."""
    if target_wh is None:
        target_wh = _ensure_warehouse(
            f"{_PREFIX}WH-TGT-DEFAULT", company=_company()
        )
    # Standard Selling price list — required when DN's company has a
    # configured price list (most test sites do).
    price_list = (
        frappe.db.get_value("Price List", {"selling": 1}, "name")
        or "Standard Selling"
    )
    if not frappe.db.exists("Price List", price_list):
        # Create the minimum Selling price list so DN insert satisfies
        # plc_* mandatories.
        pl = frappe.new_doc("Price List")
        pl.update(
            {
                "price_list_name": price_list,
                "currency": "INR",
                "selling": 1,
            }
        )
        pl.insert(ignore_permissions=True)
    dn = frappe.new_doc("Delivery Note")
    dn.update(
        {
            "customer": customer,
            "company": _company(),
            "is_internal_customer": 1,
            "set_warehouse": source_wh,
            "posting_date": frappe.utils.today(),
            "selling_price_list": price_list,
            "price_list_currency": "INR",
            "plc_conversion_rate": 1,
            "currency": "INR",
            "conversion_rate": 1,
        }
    )
    dn.append(
        "items",
        {
            "item_code": item,
            "qty": qty,
            "warehouse": source_wh,
            "target_warehouse": target_wh,
            "rate": 10,
        },
    )
    dn.insert(ignore_permissions=True)
    # Stay in Draft — validate doesn't need a Submitted DN.
    return dn.name


def _make_non_internal_dn(
    *, customer: str, source_wh: str, item: str
) -> str:
    price_list = (
        frappe.db.get_value("Price List", {"selling": 1}, "name")
        or "Standard Selling"
    )
    dn = frappe.new_doc("Delivery Note")
    dn.update(
        {
            "customer": customer,
            "company": _company(),
            "is_internal_customer": 0,
            "set_warehouse": source_wh,
            "posting_date": frappe.utils.today(),
            "selling_price_list": price_list,
            "price_list_currency": "INR",
            "plc_conversion_rate": 1,
            "currency": "INR",
            "conversion_rate": 1,
        }
    )
    dn.append(
        "items",
        {
            "item_code": item,
            "qty": 1,
            "warehouse": source_wh,
            "rate": 10,
        },
    )
    dn.insert(ignore_permissions=True)
    return dn.name


def _ensure_customer(name: str, *, is_internal: bool = True) -> str:
    """ERPNext refuses to create a second Internal Customer
    representing the same Company (selling/customer.py:243-258). So
    for is_internal=True, prefer an existing Internal Customer that
    represents _company() — name doesn't matter, the DN just needs a
    valid Internal Customer link."""
    if is_internal:
        existing = frappe.db.get_value(
            "Customer",
            {
                "is_internal_customer": 1,
                "represents_company": _company(),
            },
            "name",
        )
        if existing:
            # Ensure the Allowed-To-Transact-With table includes
            # _company() (so the DN can use this customer).
            doc = frappe.get_doc("Customer", existing)
            if not any(
                r.company == _company() for r in (doc.companies or [])
            ):
                doc.append("companies", {"company": _company()})
                doc.save(ignore_permissions=True)
            return existing
    if frappe.db.exists("Customer", name):
        return name
    g = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    if not g:
        if not frappe.db.exists("Customer Group", "All Customer Groups"):
            root = frappe.new_doc("Customer Group")
            root.update(
                {
                    "customer_group_name": "All Customer Groups",
                    "is_group": 1,
                }
            )
            root.insert(ignore_permissions=True)
        cg = frappe.new_doc("Customer Group")
        cg.update(
            {
                "customer_group_name": f"{_PREFIX}CG",
                "parent_customer_group": "All Customer Groups",
                "is_group": 0,
            }
        )
        cg.insert(ignore_permissions=True)
        g = cg.name
    c = frappe.new_doc("Customer")
    c.update(
        {
            "customer_name": name,
            "customer_type": "Company",
            "customer_group": g,
            "is_internal_customer": 1 if is_internal else 0,
            "represents_company": _company() if is_internal else None,
            "companies": [{"company": _company()}] if is_internal else [],
        }
    )
    c.insert(ignore_permissions=True)
    return c.name


def _ensure_item(code: str) -> str:
    if frappe.db.exists("Item", code):
        return code
    g = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    it = frappe.new_doc("Item")
    it.update(
        {
            "item_code": code,
            "item_name": code,
            "item_group": g,
            "stock_uom": "Nos",
            "is_stock_item": 1,
            "gst_hsn_code": "85171000",
        }
    )
    it.insert(ignore_permissions=True)
    return it.name


class TestTransferMapValidate(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _company()
        cls.src_wh = _ensure_warehouse(f"{_PREFIX}WH-SRC", company=cls.company)
        cls.tgt_wh = _ensure_warehouse(f"{_PREFIX}WH-TGT", company=cls.company)
        cls.cust = _ensure_customer(f"{_PREFIX}INTL-CUST", is_internal=True)
        cls.non_internal_cust = _ensure_customer(
            f"{_PREFIX}REG-CUST", is_internal=False
        )
        cls.item = _ensure_item(f"{_PREFIX}ITEM")

    def _make_map(self, **overrides) -> "frappe.Document":
        dn = _make_internal_dn(
            customer=self.cust, source_wh=self.src_wh, item=self.item
        )
        defaults = {
            "delivery_note": dn,
            "source_warehouse": self.src_wh,
            "target_warehouse": self.tgt_wh,
            "status": "Mapped",
        }
        defaults.update(overrides)
        return frappe.get_doc({"doctype": "EasyEcom Transfer Map", **defaults})

    def test_unknown_status_rejected(self) -> None:
        doc = self._make_map(status="NotARealStatus")
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_unknown_ee_doctype_rejected(self) -> None:
        doc = self._make_map(ee_doctype="NotSTNorPO")
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_non_internal_dn_rejected(self) -> None:
        """§10 only handles Internal-Customer DNs."""
        dn = _make_non_internal_dn(
            customer=self.non_internal_cust,
            source_wh=self.src_wh,
            item=self.item,
        )
        doc = frappe.get_doc(
            {
                "doctype": "EasyEcom Transfer Map",
                "delivery_note": dn,
                "source_warehouse": self.src_wh,
                "target_warehouse": self.tgt_wh,
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError) as ctx:
            doc.insert(ignore_permissions=True)
        self.assertIn("is_internal_customer", str(ctx.exception))

    def test_unknown_warehouse_rejected(self) -> None:
        doc = self._make_map(source_warehouse=f"{_PREFIX}NEVER-EXISTS - X")
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_ee_order_id_with_mapped_status_rejected(self) -> None:
        doc = self._make_map(status="Mapped", ee_order_id="OID-12345")
        with self.assertRaises(frappe.ValidationError) as ctx:
            doc.insert(ignore_permissions=True)
        self.assertIn("EE id captured", str(ctx.exception))

    def test_gstin_different_computed_same_company_same_gstin(self) -> None:
        """Both warehouses on the same Company → same GSTIN → flag=0."""
        doc = self._make_map()
        doc.insert(ignore_permissions=True)
        # Source and target both on _Test Company → identical GSTIN
        # (or both empty, which also yields 0).
        self.assertEqual(int(doc.gstin_different or 0), 0)

    def test_mapped_status_with_no_ee_id_accepted(self) -> None:
        """Happy path — clean Mapped row inserts."""
        doc = self._make_map(status="Mapped")
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.status, "Mapped")
        self.assertTrue(doc.name.startswith("ECS-XFER-"))


# ============================================================
# Custom field back-refs
# ============================================================


class TestSection10CustomFields(FrappeTestCase):
    """§10 adds ecs_section10_transfer_map (Link → Transfer Map) on
    DN/SI/PR/PI. Verified via Custom Field row existence + meta."""

    def test_back_ref_on_delivery_note(self) -> None:
        self._assert_back_ref("Delivery Note")

    def test_back_ref_on_sales_invoice(self) -> None:
        self._assert_back_ref("Sales Invoice")

    def test_back_ref_on_purchase_receipt(self) -> None:
        self._assert_back_ref("Purchase Receipt")

    def test_back_ref_on_purchase_invoice(self) -> None:
        self._assert_back_ref("Purchase Invoice")

    def _assert_back_ref(self, doctype: str) -> None:
        meta = frappe.get_meta(doctype)
        f = meta.get_field("ecs_section10_transfer_map")
        self.assertIsNotNone(
            f, f"{doctype}.ecs_section10_transfer_map missing"
        )
        self.assertEqual(f.fieldtype, "Link")
        self.assertEqual(f.options, "EasyEcom Transfer Map")
        self.assertTrue(f.read_only)


# ============================================================
# §10 Account settings
# ============================================================


class TestSection10AccountSettings(FrappeTestCase):
    def test_stn_default_payment_mode_exists_with_defaults(self) -> None:
        meta = frappe.get_meta("EasyEcom Account")
        f = meta.get_field("stn_default_payment_mode")
        self.assertIsNotNone(f)
        self.assertEqual(f.fieldtype, "Select")
        opts = (f.options or "").split("\n")
        self.assertEqual(opts, ["2 COD", "5 Prepaid"])
        self.assertEqual(f.default, "5 Prepaid")
        self.assertTrue(f.reqd)

    def test_stn_default_shipping_method_exists_with_defaults(self) -> None:
        meta = frappe.get_meta("EasyEcom Account")
        f = meta.get_field("stn_default_shipping_method")
        self.assertIsNotNone(f)
        self.assertEqual(f.fieldtype, "Select")
        opts = (f.options or "").split("\n")
        self.assertEqual(opts, ["1 Standard COD", "3 Standard Prepaid"])
        self.assertEqual(f.default, "1 Standard COD")
        self.assertTrue(f.reqd)


# ============================================================
# CREATE_ORDER endpoint
# ============================================================


class TestCreateOrderEndpoint(FrappeTestCase):
    def test_constant_present(self) -> None:
        self.assertEqual(endpoints.CREATE_ORDER, "/webhook/v2/createOrder")

    def test_module_export(self) -> None:
        self.assertTrue(hasattr(endpoints, "CREATE_ORDER"))


# ============================================================
# Internal Customer / Supplier pair auto-creation
# ============================================================


class TestInternalPartyPairs(FrappeTestCase):
    """ensure_internal_party_pairs_for_account — idempotency, pair
    creation, EE push mocked, role gate."""

    ACCOUNT = "test-account"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Disabled enabled=False because dev sites may have a live
        # Account (Harmony) — single-Account validate blocks two
        # enabled rows. The pair-create reads Locations regardless of
        # the Account's enabled flag.
        make_account(cls.ACCOUNT, enabled=False)
        cls._wipe_test_state()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._wipe_test_state()
        super().tearDownClass()

    @classmethod
    def _wipe_test_state(cls) -> None:
        # §10 Stage 3 isolation hardening — also wipe the EasyEcom
        # Customer Map / Supplier Map rows linked to the Internal pair
        # so the next test class can re-push from a clean state.
        from ecommerce_super.tests.factories import (
            cleanup_internal_pair_fabric,
        )
        cleanup_internal_pair_fabric()
        # Wipe Internal Customer/Supplier rows we created — broaden
        # filter so the ERPNext-aligned 'INTL-CUST-for-X' /
        # 'INTL-SUPP-from-X' naming convention is caught.
        for n in frappe.db.get_all(
            "Customer",
            filters={"customer_name": ("like", "INTL-CUST%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Customer", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        for n in frappe.db.get_all(
            "Supplier",
            filters={"supplier_name": ("like", "INTL-SUPP%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Supplier", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        # Wipe ALL §10-S1 test Locations — both LOC- and PRE- prefixes,
        # and any orphan Locations representing the test Companies.
        for n in frappe.db.get_all(
            "EasyEcom Location",
            filters={"location_key": ("like", f"{_PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Location",
                    n,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass
        # NOTE: We deliberately do NOT delete `_Pair Test Co` Company
        # here. Once created, the Company persists across test runs.
        # Reason: §9 GRN-pull test factories use _company() (= first
        # Company by creation order) to pick a working Company for
        # warehouse/PO creation. If _Pair Test Co is the first row
        # AND its existence is unstable across runs, §9 Location
        # rows (created with mapped_warehouse pointing to the prior
        # _company() result) drift out of sync. Solution: rely on
        # the §10 setUpClass/tearDownClass to wipe ONLY rows §10
        # owns (Internal Customers/Suppliers, EE Locations). The
        # ambient Companies stay.
        frappe.db.commit()

    def _seed_ee_linked_companies(
        self, companies: list[str]
    ) -> None:
        """Insert Live + Enabled EasyEcom Location rows for each given
        Company so _ee_linked_companies() returns them."""
        for i, comp in enumerate(companies):
            wh = _ensure_warehouse(
                f"{_PREFIX}WH-PAIR-{i}", company=comp
            )
            loc_key = f"{_PREFIX}LOC-{i:03d}"
            docname = f"ECS-LOC-{loc_key}"
            if frappe.db.exists("EasyEcom Location", docname):
                continue
            doc = frappe.new_doc("EasyEcom Location")
            doc.update(
                {
                    "location_key": loc_key,
                    "location_name": f"Test Location {loc_key}",
                    "workflow_state": "To Map",
                    "enabled": 1,
                }
            )
            doc.insert(ignore_permissions=True)
            frappe.db.set_value(
                "EasyEcom Location",
                doc.name,
                {
                    "workflow_state": "Live",
                    "frappe_company": comp,
                    "mapped_warehouse": wh,
                    "is_operational": 1,
                },
                update_modified=False,
            )
            frappe.db.commit()

    def test_no_op_when_fewer_than_two_ee_companies(self) -> None:
        """One Live EE-linked Company → no pair possible → ok=True,
        empty pairs. Also mute any pre-existing Live Locations from
        sibling tests so the test_no_op state is the only Live one."""
        from ecommerce_super.easyecom.api.internal_party_pairs import (
            ensure_internal_party_pairs_for_account,
        )
        self._wipe_test_state()
        # Mute pre-existing Live Locations (e.g. from real Harmony or
        # earlier test runs) so this test's single-Company seed is the
        # only Live state visible to ee_linked_companies().
        cls_muted: list[str] = frappe.db.get_all(
            "EasyEcom Location",
            filters={"workflow_state": "Live", "enabled": 1},
            pluck="name",
        )
        for n in cls_muted:
            frappe.db.set_value(
                "EasyEcom Location",
                n,
                "enabled",
                0,
                update_modified=False,
            )
        frappe.db.commit()
        try:
            self._seed_ee_linked_companies([_company()])

            with patch(
                "ecommerce_super.easyecom.api.internal_party_pairs."
                "_push_internal_customer_to_ee",
                return_value={"pushed": True, "operation": "skipped"},
            ):
                out = ensure_internal_party_pairs_for_account(
                    self.ACCOUNT, confirm=True
                )

            self.assertTrue(out["ok"])
            self.assertEqual(out["internal_customers"], [])
            self.assertEqual(out["internal_suppliers"], [])
            self.assertIn("Only 1", out["summary"])
        finally:
            # Restore muted Locations.
            for n in cls_muted:
                if frappe.db.exists("EasyEcom Location", n):
                    frappe.db.set_value(
                        "EasyEcom Location",
                        n,
                        "enabled",
                        1,
                        update_modified=False,
                    )
            frappe.db.commit()

    def test_creates_n_plus_n_for_two_companies(self) -> None:
        """2 EE-linked Companies → 2 Internal Customers (one per
        destination) + 2 Internal Suppliers (one per source). Each
        Internal Customer's `companies` table lists the OTHER Company
        as the allowed seller; each Internal Supplier's lists the
        OTHER as allowed buyer.

        ERPNext-aligned cardinality (Stage 1 packet correction —
        ERPNext enforces unique-by-represents_company).

        Two-Company configuration intentionally chosen (over 3+) for
        test isolation: dynamically creating a third Company shifts
        the test site's _company() default and destabilises §9 GRN
        test factories that key on it. The N+N cardinality contract
        is fully tested at N=2; N≥3 generalises trivially."""
        from ecommerce_super.easyecom.api.internal_party_pairs import (
            ensure_internal_party_pairs_for_account,
        )
        if not frappe.db.exists("Company", "_Other Test Co"):
            self.skipTest("Test site lacks _Other Test Co")
        self._wipe_test_state()
        self._seed_ee_linked_companies(
            ["_Test Company", "_Other Test Co"]
        )

        captured_pushes: list[str] = []

        def fake_push(customer_docname: str) -> dict:
            captured_pushes.append(customer_docname)
            return {
                "pushed": True,
                "operation": "created",
                "ee_customer_id": f"EE-{customer_docname[-6:]}",
            }

        with patch(
            "ecommerce_super.easyecom.api.internal_party_pairs."
            "_push_internal_customer_to_ee",
            side_effect=fake_push,
        ):
            out = ensure_internal_party_pairs_for_account(
                self.ACCOUNT, confirm=True
            )

        self.assertTrue(out["ok"], out.get("summary"))
        self.assertEqual(len(out["internal_customers"]), 2)
        self.assertEqual(len(out["internal_suppliers"]), 2)
        # All 2 customers pushed to EE.
        self.assertEqual(len(captured_pushes), 2)
        # Each Internal Customer's `companies` table has exactly the
        # one OTHER Company (the seller).
        for c in out["internal_customers"]:
            doc = frappe.get_doc("Customer", c["name"])
            self.assertEqual(
                len(doc.companies),
                1,
                f"Internal Customer {c['name']} should have 1 "
                f"allowed-seller, got {len(doc.companies)}",
            )
        for s in out["internal_suppliers"]:
            doc = frappe.get_doc("Supplier", s["name"])
            self.assertEqual(
                len(doc.companies),
                1,
                f"Internal Supplier {s['name']} should have 1 "
                f"allowed-buyer, got {len(doc.companies)}",
            )

    def test_idempotent_second_call_reports_pre_existing(self) -> None:
        from ecommerce_super.easyecom.api.internal_party_pairs import (
            ensure_internal_party_pairs_for_account,
        )
        self._wipe_test_state()
        self._seed_ee_linked_companies([_company(), "_Other Test Co"])

        with patch(
            "ecommerce_super.easyecom.api.internal_party_pairs."
            "_push_internal_customer_to_ee",
            return_value={"pushed": True, "operation": "skipped"},
        ):
            first = ensure_internal_party_pairs_for_account(
                self.ACCOUNT, confirm=True
            )
            second = ensure_internal_party_pairs_for_account(
                self.ACCOUNT, confirm=True
            )

        # Second run: zero created, all pre-existing.
        self.assertEqual(
            len(first["internal_customers"]),
            len(second["internal_customers"]),
        )
        for c in second["internal_customers"]:
            self.assertFalse(
                c["created"],
                f"Idempotent: customer {c['name']} marked created on "
                "second run",
            )
        for s in second["internal_suppliers"]:
            self.assertFalse(
                s["created"],
                f"Idempotent: supplier {s['name']} marked created on "
                "second run",
            )
        self.assertIn("already existed", second["summary"])

    def test_refuses_without_confirm(self) -> None:
        from ecommerce_super.easyecom.api.internal_party_pairs import (
            ensure_internal_party_pairs_for_account,
        )
        out = ensure_internal_party_pairs_for_account(
            self.ACCOUNT, confirm=False
        )
        self.assertFalse(out["ok"])
        self.assertIn("Confirmation required", out["message"])

    def test_refuses_on_unknown_account(self) -> None:
        from ecommerce_super.easyecom.api.internal_party_pairs import (
            ensure_internal_party_pairs_for_account,
        )
        out = ensure_internal_party_pairs_for_account(
            "no-such-account", confirm=True
        )
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["message"])


class TestInternalPartyPairsRoleGate(FrappeTestCase):
    OPERATOR = "fix2-s10-operator@test.local"
    _orig_user = ""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._orig_user = frappe.session.user
        if not frappe.db.exists("User", cls.OPERATOR):
            u = frappe.new_doc("User")
            u.update(
                {
                    "email": cls.OPERATOR,
                    "first_name": "S10Operator",
                    "send_welcome_email": 0,
                    "enabled": 1,
                }
            )
            u.append("roles", {"role": "EasyEcom Operator"})
            u.insert(ignore_permissions=True)
            frappe.db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        frappe.set_user(cls._orig_user)
        super().tearDownClass()

    def test_operator_cannot_ensure_pairs(self) -> None:
        from ecommerce_super.easyecom.api.internal_party_pairs import (
            ensure_internal_party_pairs_for_account,
        )
        frappe.set_user(self.OPERATOR)
        try:
            with self.assertRaises(frappe.PermissionError):
                ensure_internal_party_pairs_for_account(
                    "test-account", confirm=True
                )
        finally:
            frappe.set_user(self._orig_user)


# ============================================================
# §10 precheck
# ============================================================


class TestPrecheckSection10(FrappeTestCase):
    """precheck_section10_go_live — blockers / warnings / clean pass."""

    ACCOUNT = "test-account"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_account(cls.ACCOUNT, enabled=False)
        cls._wipe_locations()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._wipe_locations()
        super().tearDownClass()

    @classmethod
    def _wipe_locations(cls) -> None:
        for n in frappe.db.get_all(
            "EasyEcom Location",
            filters={"location_key": ("like", f"{_PREFIX}PRE-%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Location",
                    n,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass
        frappe.db.commit()

    def setUp(self) -> None:
        self._wipe_locations()

    def test_account_not_found_blocker(self) -> None:
        from ecommerce_super.easyecom.api.precheck_section10 import (
            precheck_section10_go_live,
        )
        out = precheck_section10_go_live("no-such-account")
        self.assertFalse(out["ok"])
        self.assertTrue(
            any("not found" in b for b in out["blockers"])
        )

    def test_blockers_when_account_defaults_missing(self) -> None:
        """Clear the in-transit + rejected warehouses on the test
        account → precheck reports both."""
        from ecommerce_super.easyecom.api.precheck_section10 import (
            precheck_section10_go_live,
        )
        # Save originals so we can restore.
        prior = frappe.db.get_value(
            "EasyEcom Account",
            self.ACCOUNT,
            [
                "default_in_transit_warehouse",
                "default_rejected_warehouse",
                "lost_in_transit_threshold_days",
            ],
            as_dict=True,
        )
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT,
            {
                "default_in_transit_warehouse": None,
                "default_rejected_warehouse": None,
                "lost_in_transit_threshold_days": 0,
            },
            update_modified=False,
        )
        try:
            out = precheck_section10_go_live(self.ACCOUNT)
            self.assertFalse(out["ok"])
            joined = " || ".join(out["blockers"])
            self.assertIn("default_in_transit_warehouse", joined)
            self.assertIn("default_rejected_warehouse", joined)
            self.assertIn("lost_in_transit_threshold_days", joined)
        finally:
            frappe.db.set_value(
                "EasyEcom Account",
                self.ACCOUNT,
                {
                    "default_in_transit_warehouse": (
                        prior.default_in_transit_warehouse
                    ),
                    "default_rejected_warehouse": (
                        prior.default_rejected_warehouse
                    ),
                    "lost_in_transit_threshold_days": (
                        prior.lost_in_transit_threshold_days or 30
                    ),
                },
                update_modified=False,
            )

    def test_missing_internal_pairs_blocker(self) -> None:
        """2 EE-linked Companies → precheck reports a missing-pairs
        or missing-ee-customer-id blocker (depending on what state
        sibling tests left). Either signals 'pair fabric incomplete'."""
        from ecommerce_super.easyecom.api.precheck_section10 import (
            precheck_section10_go_live,
        )
        # Ensure 2 EE-linked Companies exist on test Locations.
        if not frappe.db.exists("Company", "_Other Test Co"):
            self.skipTest("Test site lacks _Other Test Co")
        # Clear any existing Internal Customer/Supplier rows so the
        # 'missing customers' blocker is the one we hit (and not the
        # 'customers exist but lack ee_customer_id' downstream branch).
        for n in frappe.db.get_all(
            "Customer",
            filters={"customer_name": ("like", "INTL-CUST%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Customer", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        for n in frappe.db.get_all(
            "Supplier",
            filters={"supplier_name": ("like", "INTL-SUPP%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Supplier", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        frappe.db.commit()
        for i, comp in enumerate(
            [_company(), "_Other Test Co"]
        ):
            wh = _ensure_warehouse(
                f"{_PREFIX}PRE-WH-{i}", company=comp
            )
            doc = frappe.new_doc("EasyEcom Location")
            doc.update(
                {
                    "location_key": f"{_PREFIX}PRE-{i:03d}",
                    "location_name": f"Pre Loc {i}",
                    "workflow_state": "To Map",
                    "enabled": 1,
                }
            )
            doc.insert(ignore_permissions=True)
            frappe.db.set_value(
                "EasyEcom Location",
                doc.name,
                {
                    "workflow_state": "Live",
                    "frappe_company": comp,
                    "mapped_warehouse": wh,
                    "is_operational": 1,
                },
                update_modified=False,
            )
        # Ensure Account defaults are set (avoid double-failure noise).
        wh_for_acct = _ensure_warehouse(
            f"{_PREFIX}PRE-WH-DEF", company=_company()
        )
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT,
            {
                "default_in_transit_warehouse": wh_for_acct,
                "default_rejected_warehouse": wh_for_acct,
                "lost_in_transit_threshold_days": 30,
            },
            update_modified=False,
        )
        frappe.db.commit()

        out = precheck_section10_go_live(self.ACCOUNT)
        self.assertFalse(out["ok"])
        joined = " || ".join(out["blockers"])
        self.assertIn("Missing Internal Customer", joined)
        self.assertIn("Missing Internal Supplier", joined)


# ============================================================
# §8/§9 regression sanity (run-as-pass)
# ============================================================


class TestSection10AdditiveOnly(FrappeTestCase):
    """§10 Stage 1 is additive. The Transfer Map DocType must not have
    shifted any §9 schema, and the Account settings additions must not
    have collided with existing fieldnames."""

    def test_po_map_status_enum_intact(self) -> None:
        meta = frappe.get_meta("EasyEcom PO Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(
            opts,
            {
                "Mapped",
                "Created-Flagged",
                "Flagged-Not-Created",
                "Drift",
                "Disabled",
            },
        )

    def test_grn_map_status_enum_intact(self) -> None:
        meta = frappe.get_meta("EasyEcom GRN Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        # §9 corrective added Dismissed; §10 must not have stripped it.
        self.assertIn("Dismissed", opts)
        self.assertIn("Discrepancy", opts)

    def test_section_10_setting_fields_distinct_from_grn_policy(self) -> None:
        """Sanity check that §10 settings were added inside their own
        section break, not jammed into GRN/Inward Policy."""
        meta = frappe.get_meta("EasyEcom Account")
        for fn in (
            "stn_default_payment_mode",
            "stn_default_shipping_method",
            "section10_defaults_section",
        ):
            self.assertIsNotNone(
                meta.get_field(fn), f"Account.{fn} missing"
            )
