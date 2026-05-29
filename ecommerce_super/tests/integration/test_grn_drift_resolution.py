"""Corrective commit 2026-05-29 (FIX 1) — FDE-driven resolution actions
for unknown-PO GRN drift.

Tests:
  - create_pr_from_grn (no PO) → STANDALONE PR (purchase_order empty
    on PR Item, Discrepancy → Resolved, GRN Map → Receipted)
  - create_pr_from_grn (with PO) → PR with PO link (purchase_order
    populated, linked_po_map set)
  - dismiss_grn_drift → GRN Map → Dismissed, Discrepancy → Dismissed,
    no PR
  - Refuses on confirm=False
  - Refuses on non-drift state (Receipted / Dismissed)
  - Refuses on missing GRN Map row
  - dismiss requires reason
  - Role gate: Operator cannot invoke create / dismiss
  - Standalone PR — purchase_order empty (no PO.per_received update)
  - With-PO PR — purchase_order populated + purchase_order_item set
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.grn_drift_resolution import (
    create_pr_from_grn,
    dismiss_grn_drift,
)
from ecommerce_super.easyecom.flows.grn_pull import process_one_grn
from ecommerce_super.tests.factories import make_account, make_location


_PREFIX = "TEST-S9-FIX1-"


def _company() -> str:
    c = frappe.db.get_value("Company", filters={}, fieldname="name")
    if c:
        return c
    raise RuntimeError("No Company exists")


def _ensure_warehouse(name: str, *, company: str) -> str:
    existing = frappe.db.get_value(
        "Warehouse", {"warehouse_name": name, "company": company}, "name"
    )
    if existing:
        return existing
    w = frappe.new_doc("Warehouse")
    w.update({"warehouse_name": name, "company": company, "is_group": 0})
    w.insert(ignore_permissions=True)
    return w.name


def _ensure_supplier_group() -> str:
    g = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if g:
        return g
    if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
        root = frappe.new_doc("Supplier Group")
        root.update({"supplier_group_name": "All Supplier Groups", "is_group": 1})
        root.insert(ignore_permissions=True)
    sg = frappe.new_doc("Supplier Group")
    sg.update(
        {
            "supplier_group_name": f"{_PREFIX}SG",
            "parent_supplier_group": "All Supplier Groups",
            "is_group": 0,
        }
    )
    sg.insert(ignore_permissions=True)
    return sg.name


def _ensure_supplier(name: str) -> str:
    if frappe.db.exists("Supplier", name):
        return name
    s = frappe.new_doc("Supplier")
    s.update(
        {
            "supplier_name": name,
            "supplier_type": "Company",
            "supplier_group": _ensure_supplier_group(),
            "country": "India",
        }
    )
    s.insert(ignore_permissions=True)
    return s.name


def _ensure_item_group() -> str:
    g = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    if g:
        return g
    if not frappe.db.exists("Item Group", "All Item Groups"):
        root = frappe.new_doc("Item Group")
        root.update({"item_group_name": "All Item Groups", "is_group": 1})
        root.insert(ignore_permissions=True)
    ig = frappe.new_doc("Item Group")
    ig.update(
        {
            "item_group_name": f"{_PREFIX}IG",
            "parent_item_group": "All Item Groups",
            "is_group": 0,
        }
    )
    ig.insert(ignore_permissions=True)
    return ig.name


def _ensure_item(code: str) -> str:
    if frappe.db.exists("Item", code):
        return code
    it = frappe.new_doc("Item")
    it.update(
        {
            "item_code": code,
            "item_name": code,
            "item_group": _ensure_item_group(),
            "stock_uom": "Nos",
            "is_stock_item": 1,
            "gst_hsn_code": "85171000",
        }
    )
    it.insert(ignore_permissions=True)
    return it.name


def _make_supplier_map(supplier: str, *, ee_vendor_c_id: str) -> str:
    # Match-on-c_id (the autoname source) AND on linked supplier — a
    # stale row from a prior test using the same c_id but a different
    # supplier would collide on the docname PK if we only checked the
    # supplier link.
    by_c_id = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"ee_vendor_c_id": ee_vendor_c_id},
        "name",
    )
    if by_c_id:
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            by_c_id,
            {
                "erpnext_doctype": "Supplier",
                "erpnext_name": supplier,
                "status": "Mapped",
            },
            update_modified=False,
        )
        return by_c_id
    existing = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": supplier},
        "name",
    )
    if existing:
        return existing
    m = frappe.new_doc("EasyEcom Supplier Map")
    m.update(
        {
            "ee_vendor_c_id": ee_vendor_c_id,
            "ee_vendor_id": f"VN-{ee_vendor_c_id}",
            "erpnext_doctype": "Supplier",
            "erpnext_name": supplier,
            "status": "Mapped",
        }
    )
    m.insert(ignore_permissions=True)
    return m.name


def _make_item_map(item_code: str, *, ee_sku: str) -> str:
    existing = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Item", "erpnext_name": item_code},
        "name",
    )
    if existing:
        return existing
    m = frappe.new_doc("EasyEcom Item Map")
    m.update(
        {
            "ee_sku": ee_sku,
            "erpnext_doctype": "Item",
            "erpnext_name": item_code,
            "status": "Mapped",
        }
    )
    m.insert(ignore_permissions=True)
    return m.name


def _make_submitted_po(
    *, supplier: str, warehouse: str, item: str, qty: int = 10, rate: float = 50
) -> Any:
    po = frappe.new_doc("Purchase Order")
    po.update(
        {
            "supplier": supplier,
            "company": _company(),
            "transaction_date": frappe.utils.today(),
            "schedule_date": frappe.utils.add_days(frappe.utils.today(), 7),
            "set_warehouse": warehouse,
            "currency": "INR",
            "conversion_rate": 1,
        }
    )
    po.append(
        "items",
        {
            "item_code": item,
            "qty": qty,
            "rate": rate,
            "warehouse": warehouse,
            "schedule_date": po.schedule_date,
        },
    )
    po.insert(ignore_permissions=True)
    po.submit()
    return po


def _wipe_for_fix1() -> None:
    """Wipe rows this test class creates — by ee_grn_id range and PRs
    with our ecs_easyecom_grn_id back-refs."""
    for n in frappe.db.get_all(
        "Purchase Receipt",
        filters={"ecs_easyecom_grn_id": ("like", "300%")},
        pluck="name",
    ):
        try:
            doc = frappe.get_doc("Purchase Receipt", n)
            if int(doc.docstatus or 0) == 1:
                doc.cancel()
            frappe.delete_doc("Purchase Receipt", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom GRN Map",
        filters={"ee_grn_id": (">=", 300000)},
        pluck="name",
    ):
        try:
            frappe.delete_doc("EasyEcom GRN Map", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom Integration Discrepancy",
        filters={"kind": "GRN for unknown PO"},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Integration Discrepancy",
                n,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"entity_doctype": "EasyEcom GRN Map"},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Sync Record", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _grn_payload_for_drift(
    *,
    grn_id: int,
    vendor_c_id: int,
    inwarded_warehouse_c_id: int,
    sku: str,
    received_quantity: int,
    grn_detail_price: float,
    qc_fail: int = 0,
) -> dict:
    """Build a GRN payload with no PO references — guarantees drift."""
    return {
        "grn_id": grn_id,
        "grn_invoice_number": f"INV-{grn_id}",
        "grn_invoice_date": frappe.utils.today(),
        "grn_created_at": f"{frappe.utils.today()} 10:00:00",
        "grn_status_id": 3,
        "total_grn_value": received_quantity * grn_detail_price,
        "inwarded_warehouse_c_id": inwarded_warehouse_c_id,
        "vendor_c_id": vendor_c_id,
        "po_ref_num": "",  # blank → drift
        "po_id": 0,  # zero → drift
        "po_status_id": 3,
        "grn_items": [
            {
                "grn_detail_id": 1,
                "sku": sku,
                "received_quantity": received_quantity,
                "qc_fail": qc_fail,
                "grn_detail_price": grn_detail_price,
            }
        ],
    }


class TestCreatePrFromGrn(FrappeTestCase):
    """FIX 1 — FDE creates a Purchase Receipt from a drifted GRN."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_for_fix1()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH", company=cls.company)
        cls.rejected_wh = _ensure_warehouse(
            f"{_PREFIX}WH-REJ", company=cls.company
        )
        make_account()
        make_location(
            location_key="711001",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            "default_rejected_warehouse",
            cls.rejected_wh,
            update_modified=False,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="511001")
        cls.item = _ensure_item(f"{_PREFIX}ITEM")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU")
        # A Submitted PO for the "optional PO link" path.
        cls.po = _make_submitted_po(
            supplier=cls.supplier, warehouse=cls.ee_wh, item=cls.item
        )
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_for_fix1()

    def setUp(self) -> None:
        # Each test starts from a fresh drift state.
        _wipe_for_fix1()
        if frappe.db.exists("EasyEcom Account", "test-account"):
            frappe.db.set_value(
                "EasyEcom Account",
                "test-account",
                {
                    "enabled": 1,
                    "default_rejected_warehouse": self.rejected_wh,
                },
                update_modified=False,
            )
            frappe.db.commit()

    def _make_drift(self, grn_id: int) -> str:
        """Pull a GRN with no PO refs → drift state. Returns the GRN
        Map docname."""
        process_one_grn(
            _grn_payload_for_drift(
                grn_id=grn_id,
                vendor_c_id=511001,
                inwarded_warehouse_c_id=711001,
                sku=f"{_PREFIX}SKU",
                received_quantity=5,
                grn_detail_price=50,
            )
        )
        return f"ECS-GRN-{grn_id}"

    def test_create_pr_no_po_yields_standalone_pr(self) -> None:
        """FDE invokes create_pr_from_grn with NO purchase_order arg →
        STANDALONE PR (purchase_order empty on every PR Item line)."""
        gm_name = self._make_drift(grn_id=300001)
        out = create_pr_from_grn(gm_name, confirm=True)

        self.assertTrue(out["ok"], out.get("message"))
        self.assertTrue(out["purchase_receipt"])
        pr = frappe.get_doc("Purchase Receipt", out["purchase_receipt"])
        self.assertEqual(int(pr.docstatus or 0), 1, "PR must be Submitted")
        # Standalone — every line has purchase_order empty.
        for line in pr.items:
            self.assertFalse(
                line.purchase_order,
                f"Standalone PR must NOT link any PO on line "
                f"{line.item_code} — got purchase_order={line.purchase_order!r}",
            )
            self.assertFalse(
                line.purchase_order_item,
                f"Standalone PR must NOT link any PO Item on line "
                f"{line.item_code} — got purchase_order_item="
                f"{line.purchase_order_item!r}",
            )
        # GRN Map flipped.
        gm = frappe.get_doc("EasyEcom GRN Map", gm_name)
        self.assertEqual(gm.status, "Receipted")
        self.assertEqual(gm.purchase_receipt, pr.name)
        self.assertFalse(gm.linked_po_map)
        # Integration Discrepancy resolved.
        disc = frappe.db.get_value(
            "EasyEcom Integration Discrepancy",
            {
                "kind": "GRN for unknown PO",
                "reference_name": gm_name,
            },
            ["status"],
            as_dict=True,
        )
        self.assertEqual(disc.status, "Resolved")

    def test_create_pr_with_po_arg_links_that_po(self) -> None:
        """FDE supplies purchase_order=cls.po.name → PR Item lines link
        the PO + purchase_order_item resolves."""
        gm_name = self._make_drift(grn_id=300002)
        out = create_pr_from_grn(
            gm_name, purchase_order=self.po.name, confirm=True
        )

        self.assertTrue(out["ok"], out.get("message"))
        pr = frappe.get_doc("Purchase Receipt", out["purchase_receipt"])
        self.assertEqual(int(pr.docstatus or 0), 1)
        # PO link populated on lines.
        for line in pr.items:
            self.assertEqual(line.purchase_order, self.po.name)
            self.assertTrue(
                line.purchase_order_item,
                "With-PO PR must populate purchase_order_item so "
                "PO.per_received updates",
            )

    def test_create_pr_refuses_without_confirm(self) -> None:
        gm_name = self._make_drift(grn_id=300003)
        out = create_pr_from_grn(gm_name)  # confirm=False default
        self.assertFalse(out["ok"])
        self.assertIn("Confirmation required", out["message"])
        # No PR created.
        prs = frappe.db.get_all(
            "Purchase Receipt",
            filters={"ecs_easyecom_grn_id": "300003"},
            pluck="name",
        )
        self.assertEqual(prs, [])

    def test_create_pr_refuses_on_missing_grn_map(self) -> None:
        out = create_pr_from_grn("ECS-GRN-999999999", confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["message"])

    def test_create_pr_refuses_on_already_receipted(self) -> None:
        """A GRN Map already at Receipted (e.g. from a prior FDE
        action) refuses a second create_pr call."""
        gm_name = self._make_drift(grn_id=300004)
        first = create_pr_from_grn(gm_name, confirm=True)
        self.assertTrue(first["ok"])
        second = create_pr_from_grn(gm_name, confirm=True)
        self.assertFalse(second["ok"])
        self.assertIn("already Receipted", second["message"])

    def test_create_pr_refuses_on_dismissed(self) -> None:
        gm_name = self._make_drift(grn_id=300005)
        dismiss_grn_drift(
            gm_name, reason="test dismissal", confirm=True
        )
        out = create_pr_from_grn(gm_name, confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("Dismissed", out["message"])

    def test_create_pr_refuses_with_unsubmitted_po_arg(self) -> None:
        """purchase_order arg must be a Submitted PO."""
        gm_name = self._make_drift(grn_id=300006)
        # Draft PO.
        draft_po = frappe.new_doc("Purchase Order")
        draft_po.update(
            {
                "supplier": self.supplier,
                "company": self.company,
                "transaction_date": frappe.utils.today(),
                "schedule_date": frappe.utils.add_days(
                    frappe.utils.today(), 7
                ),
                "set_warehouse": self.ee_wh,
                "currency": "INR",
                "conversion_rate": 1,
            }
        )
        draft_po.append(
            "items",
            {
                "item_code": self.item,
                "qty": 1,
                "rate": 1,
                "warehouse": self.ee_wh,
                "schedule_date": draft_po.schedule_date,
            },
        )
        draft_po.insert(ignore_permissions=True)
        # Don't submit.
        out = create_pr_from_grn(
            gm_name, purchase_order=draft_po.name, confirm=True
        )
        self.assertFalse(out["ok"])
        self.assertIn("not submitted", out["message"])


class TestDismissGrnDrift(FrappeTestCase):
    """FIX 1 — FDE dismisses a drifted GRN (no PR created)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_for_fix1()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-DIS", company=cls.company)
        make_account()
        make_location(
            location_key="711002",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-DIS")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="511002")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-DIS")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-DIS")
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_for_fix1()

    def setUp(self) -> None:
        _wipe_for_fix1()

    def _make_drift(self, grn_id: int) -> str:
        process_one_grn(
            _grn_payload_for_drift(
                grn_id=grn_id,
                vendor_c_id=511002,
                inwarded_warehouse_c_id=711002,
                sku=f"{_PREFIX}SKU-DIS",
                received_quantity=2,
                grn_detail_price=10,
            )
        )
        return f"ECS-GRN-{grn_id}"

    def test_dismiss_closes_drift_no_pr(self) -> None:
        gm_name = self._make_drift(grn_id=300010)
        out = dismiss_grn_drift(
            gm_name, reason="duplicate of another GRN", confirm=True
        )

        self.assertTrue(out["ok"], out.get("message"))
        gm = frappe.get_doc("EasyEcom GRN Map", gm_name)
        self.assertEqual(gm.status, "Dismissed")
        self.assertFalse(gm.purchase_receipt)
        # No PR created.
        prs = frappe.db.get_all(
            "Purchase Receipt",
            filters={"ecs_easyecom_grn_id": "300010"},
            pluck="name",
        )
        self.assertEqual(prs, [])
        # Integration Discrepancy → Dismissed.
        disc = frappe.db.get_value(
            "EasyEcom Integration Discrepancy",
            {
                "kind": "GRN for unknown PO",
                "reference_name": gm_name,
            },
            ["status", "resolution_note"],
            as_dict=True,
        )
        self.assertEqual(disc.status, "Dismissed")
        self.assertIn("duplicate", disc.resolution_note)

    def test_dismiss_refuses_without_reason(self) -> None:
        gm_name = self._make_drift(grn_id=300011)
        out = dismiss_grn_drift(gm_name, reason="", confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("Reason is required", out["message"])

    def test_dismiss_refuses_without_confirm(self) -> None:
        gm_name = self._make_drift(grn_id=300012)
        out = dismiss_grn_drift(gm_name, reason="x")
        self.assertFalse(out["ok"])
        self.assertIn("Confirmation required", out["message"])

    def test_dismiss_refuses_on_already_dismissed(self) -> None:
        gm_name = self._make_drift(grn_id=300013)
        first = dismiss_grn_drift(
            gm_name, reason="noise", confirm=True
        )
        self.assertTrue(first["ok"])
        second = dismiss_grn_drift(
            gm_name, reason="noise again", confirm=True
        )
        self.assertFalse(second["ok"])
        self.assertIn("already Dismissed", second["message"])

    def test_repulled_dismissed_drift_is_noop(self) -> None:
        """After FDE dismiss, re-pulling the same GRN must NOT undo
        the dismiss (no new PR, no re-raise of Discrepancy)."""
        gm_name = self._make_drift(grn_id=300014)
        dismiss_grn_drift(
            gm_name, reason="vendor-side cancellation", confirm=True
        )

        # Re-pull the same payload.
        outcome = process_one_grn(
            _grn_payload_for_drift(
                grn_id=300014,
                vendor_c_id=511002,
                inwarded_warehouse_c_id=711002,
                sku=f"{_PREFIX}SKU-DIS",
                received_quantity=2,
                grn_detail_price=10,
            )
        )
        self.assertEqual(outcome.operation, "noop_dismissed")
        # No PR.
        prs = frappe.db.get_all(
            "Purchase Receipt",
            filters={"ecs_easyecom_grn_id": "300014"},
            pluck="name",
        )
        self.assertEqual(prs, [])
        # GRN Map still Dismissed.
        gm = frappe.get_doc("EasyEcom GRN Map", gm_name)
        self.assertEqual(gm.status, "Dismissed")


class TestDriftResolutionRoleGate(FrappeTestCase):
    """FIX 1 — role gate. Operator cannot create_pr or dismiss."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Create an Operator-only user.
        cls.operator_user = "fix1-operator@test.local"
        if not frappe.db.exists("User", cls.operator_user):
            u = frappe.new_doc("User")
            u.update(
                {
                    "email": cls.operator_user,
                    "first_name": "FIX1Operator",
                    "send_welcome_email": 0,
                    "enabled": 1,
                }
            )
            u.append("roles", {"role": "EasyEcom Operator"})
            u.insert(ignore_permissions=True)
            frappe.db.commit()
        cls._orig_user = frappe.session.user

    @classmethod
    def tearDownClass(cls) -> None:
        frappe.set_user(cls._orig_user)
        super().tearDownClass()

    def test_operator_cannot_create_pr(self) -> None:
        frappe.set_user(self.operator_user)
        try:
            with self.assertRaises(frappe.PermissionError):
                create_pr_from_grn(
                    "ECS-GRN-300999", confirm=True
                )
        finally:
            frappe.set_user(self._orig_user)

    def test_operator_cannot_dismiss(self) -> None:
        frappe.set_user(self.operator_user)
        try:
            with self.assertRaises(frappe.PermissionError):
                dismiss_grn_drift(
                    "ECS-GRN-300999", reason="x", confirm=True
                )
        finally:
            frappe.set_user(self._orig_user)
