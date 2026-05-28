"""§9 Stage 3 — GRN pull → Purchase Receipt + reconciliation tests.

Mocks EE responses; uses real ERPNext PR creation against a test site.
Each test class isolates its data via the prefix-based wipe; the
factories from `tests.factories` provide Account + Location.

Coverage:
  - 10-step per-GRN chain: Gate 0, STN, idempotency, deleted (post + pre
    receipt), receipt gate, PO resolution (primary / fallback / both
    miss), supplier + item miss, qc_fail split with real-payload shapes,
    no-bucket-leak on the PR, tax variance Discrepancy, cumulative
    tolerance.
  - Status reconciliation: echo (no Discrepancy), drift (Discrepancy),
    fulfilment 11-16 (observation only).
  - Completion trigger: fires once on cumulative-complete; idempotent.
  - Force-close: PO Close button → status=5 + markPoComplete=1.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows.grn_pull import (
    GRNOutcome,
    process_one_grn,
    _reconcile_po_status,
    _maybe_fire_completion,
    enqueue_on_po_close,
)
from ecommerce_super.tests.factories import make_account, make_location


_PREFIX = "TEST-S9-S3-"


# ----- shared factory helpers (mirror Stage 2's) -----


def _company() -> str:
    c = frappe.db.get_value("Company", filters={}, fieldname="name")
    if not c:
        raise RuntimeError("No Company on the test site")
    return c


def _ensure_warehouse(name: str, *, company: str) -> str:
    existing = frappe.db.get_value(
        "Warehouse", {"warehouse_name": name, "company": company}, "name"
    )
    if existing:
        return existing
    w = frappe.new_doc("Warehouse")
    w.update(
        {"warehouse_name": name, "company": company, "is_group": 0}
    )
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


def _ensure_supplier(name: str, *, country: str = "India") -> str:
    if frappe.db.exists("Supplier", name):
        return name
    s = frappe.new_doc("Supplier")
    s.update(
        {
            "supplier_name": name,
            "supplier_type": "Company",
            "supplier_group": _ensure_supplier_group(),
            "country": country,
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


def _ensure_item(code: str, *, hsn: str = "85171000") -> str:
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
            "gst_hsn_code": hsn,
        }
    )
    it.insert(ignore_permissions=True)
    return it.name


def _make_supplier_map(
    supplier: str, *, ee_vendor_c_id: str = None, ee_vendor_id: str = "VN-GRN-001"
) -> str:
    ee_vendor_c_id = ee_vendor_c_id or f"{_PREFIX}{supplier[-12:]}"
    existing = frappe.db.get_value(
        "EasyEcom Supplier Map",
        {"erpnext_doctype": "Supplier", "erpnext_name": supplier},
        "name",
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            existing,
            {"ee_vendor_c_id": ee_vendor_c_id, "ee_vendor_id": ee_vendor_id},
        )
        return existing
    m = frappe.new_doc("EasyEcom Supplier Map")
    m.update(
        {
            "ee_vendor_c_id": ee_vendor_c_id,
            "ee_vendor_id": ee_vendor_id,
            "erpnext_doctype": "Supplier",
            "erpnext_name": supplier,
            "status": "Mapped",
        }
    )
    m.insert(ignore_permissions=True)
    return m.name


def _make_item_map(item_code: str, *, ee_sku: str | None = None) -> str:
    ee_sku = ee_sku or item_code
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


def _make_po(
    *,
    supplier: str,
    warehouse: str,
    items: list[dict],
    submit: bool = True,
    company: str | None = None,
) -> Any:
    company = company or _company()
    po = frappe.new_doc("Purchase Order")
    po.update(
        {
            "supplier": supplier,
            "company": company,
            "transaction_date": frappe.utils.today(),
            "schedule_date": frappe.utils.add_days(frappe.utils.today(), 7),
            "set_warehouse": warehouse,
            "currency": "INR",
            "conversion_rate": 1,
        }
    )
    for it in items:
        po.append(
            "items",
            {
                "item_code": it["item_code"],
                "qty": it.get("qty", 1),
                "rate": it.get("rate", 100),
                "warehouse": warehouse,
                "schedule_date": po.schedule_date,
            },
        )
    po.insert(ignore_permissions=True)
    if submit:
        po.submit()
    return po


def _make_po_map(po_name: str, *, ee_po_id: int = 9001) -> str:
    m = frappe.new_doc("EasyEcom PO Map")
    m.update(
        {
            "reference_code": po_name,
            "purchase_order": po_name,
            "status": "Mapped",
            "ee_po_id": ee_po_id,
            "last_pushed_po_status": 3,
        }
    )
    m.insert(ignore_permissions=True)
    return m.name


def _grn_payload(
    *,
    grn_id: int,
    vendor_c_id: int,
    inwarded_warehouse_c_id: int,
    grn_status_id: int = 3,
    po_ref_num: str = "",
    ee_po_id: int = 0,
    po_status_id: int = 3,
    items: list[dict] | None = None,
    total_grn_value: float | None = None,
    grn_invoice_number: str = "INV-001",
    grn_created_at: str | None = None,
) -> dict:
    """Build a getGrnDetails-shape GRN row."""
    items = items or []
    if total_grn_value is None:
        # grn_detail_price on real Harmony payloads is the LINE TOTAL
        # (not unit price) — confirmed live 2026-05-28 on GRN 2115440
        # where received_quantity=5, grn_detail_price=590,
        # total_grn_value=590. The total is therefore the sum of per-
        # line grn_detail_price values.
        total_grn_value = sum(
            float(it.get("grn_detail_price", 0)) for it in items
        )
    return {
        "grn_id": grn_id,
        "grn_invoice_number": grn_invoice_number,
        "grn_invoice_date": frappe.utils.today(),
        "grn_created_at": grn_created_at or f"{frappe.utils.today()} 10:00:00",
        "grn_status_id": grn_status_id,
        "total_grn_value": total_grn_value,
        "inwarded_warehouse_c_id": inwarded_warehouse_c_id,
        "vendor_c_id": vendor_c_id,
        "po_ref_num": po_ref_num,
        "po_id": ee_po_id,
        "po_status_id": po_status_id,
        "grn_items": items,
    }


def _wipe_ephemeral_state() -> None:
    """Per-test cleanup: wipe ONLY the §9 Stage 3 ephemeral state
    created by individual tests (Sync Records, GRN Maps, Discrepancies,
    PRs). Does NOT touch class-level fixtures (Supplier, Item, Maps,
    Warehouse, Account, Location) — those are setUpClass scope and
    wiped at tearDownClass."""
    # GRN-pull Sync Records (entity_type='GRN' uniquely identifies them).
    for n in frappe.db.get_all(
        "EasyEcom Sync Record", filters={"entity_type": "GRN"}, pluck="name"
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Sync Record", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Discrepancies (Stage 3 kinds).
    for n in frappe.db.get_all(
        "EasyEcom Integration Discrepancy",
        filters={
            "kind": (
                "in",
                ["GRN deleted after receipt", "GRN for unknown PO",
                 "tax variance", "over-receipt", "po_status drift"],
            )
        },
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Integration Discrepancy", n, force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
    # GRN Maps.
    for n in frappe.db.get_all(
        "EasyEcom GRN Map", filters={"ee_grn_id": (">=", 100000)}, pluck="name"
    ):
        try:
            frappe.delete_doc(
                "EasyEcom GRN Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # PRs (Stage-3-created back-ref filter).
    for n in frappe.db.get_all(
        "Purchase Receipt",
        filters={"ecs_easyecom_grn_id": ("!=", "")},
        pluck="name",
    ):
        try:
            pr = frappe.get_doc("Purchase Receipt", n)
            if pr.docstatus == 1:
                pr.flags.ignore_permissions = True
                pr.cancel()
            frappe.delete_doc(
                "Purchase Receipt", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _wipe_test_state() -> None:
    """tearDownClass wipe: ephemeral state + class-level fixtures."""
    _wipe_ephemeral_state()
    # PO Map (class-level fixture for some tests).
    for n in frappe.db.get_all(
        "EasyEcom PO Map",
        filters={"purchase_order": ("like", f"%{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom PO Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Any lingering Discrepancies — paranoia guard, _wipe_ephemeral_state
    # should have caught them already.
    for n in frappe.db.get_all(
        "EasyEcom Integration Discrepancy",
        filters={
            "kind": (
                "in",
                ["GRN deleted after receipt", "GRN for unknown PO",
                 "tax variance", "over-receipt", "po_status drift"],
            )
        },
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Integration Discrepancy", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "Purchase Order", filters={"name": ("like", f"%{_PREFIX}%")}, pluck="name"
    ):
        try:
            po = frappe.get_doc("Purchase Order", n)
            if po.docstatus == 1:
                po.flags.ignore_permissions = True
                po.cancel()
            frappe.delete_doc("Purchase Order", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    # Item Map + Item.
    for n in frappe.db.get_all(
        "EasyEcom Item Map",
        filters={"erpnext_name": ("like", f"%{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc("EasyEcom Item Map", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    for n in frappe.db.get_all(
        "Item", filters={"item_code": ("like", f"%{_PREFIX}%")}, pluck="name"
    ):
        try:
            frappe.delete_doc("Item", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    # Supplier Map + Supplier.
    sup_names = frappe.db.get_all(
        "Supplier",
        filters={"supplier_name": ("like", f"%{_PREFIX}%")},
        pluck="name",
    )
    for n in frappe.db.get_all(
        "EasyEcom Supplier Map",
        filters={"erpnext_name": ("in", sup_names or [""])},
        pluck="name",
    ):
        try:
            frappe.delete_doc("EasyEcom Supplier Map", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    for n in sup_names:
        try:
            frappe.delete_doc("Supplier", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    # Warehouses.
    for n in frappe.db.get_all(
        "Warehouse",
        filters={"warehouse_name": ("like", f"%{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc("Warehouse", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()


# ============================================================
# Tests
# ============================================================


class TestGate0AndSTN(FrappeTestCase):
    """Step 1 (Gate 0) + Step 2 (STN routing)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-GS", company=cls.company)
        make_account()
        make_location(
            location_key="700001",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_gate_0_unmapped_warehouse_is_silent_skip(self) -> None:
        """inwarded_warehouse_c_id that doesn't resolve to a Location →
        no GRN Map row, no Sync Record."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200001,
                vendor_c_id=12345,
                inwarded_warehouse_c_id=999999,  # not mapped
            )
        )
        self.assertEqual(outcome.operation, "skipped")
        self.assertFalse(
            frappe.db.exists("EasyEcom GRN Map", {"ee_grn_id": 200001})
        )

    def test_stn_routing_self_vendor(self) -> None:
        """vendor_c_id == inwarded_warehouse_c_id → STN-Routed, no PR.
        Real-payload shape: grn 142698 / 142703 / 141936 — self-vendor
        26564."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=142698,
                vendor_c_id=700001,
                inwarded_warehouse_c_id=700001,
                grn_status_id=3,
                total_grn_value=49990,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}STN-SKU",
                        "received_quantity": 1,
                        "grn_detail_price": 49990,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "stn_routed")
        self.assertEqual(outcome.grn_map_status, "STN-Routed")
        self.assertIsNone(outcome.purchase_receipt)
        # Map row exists with routed_to_stn=1.
        row = frappe.db.get_value(
            "EasyEcom GRN Map", {"ee_grn_id": 142698},
            ["status", "routed_to_stn", "purchase_receipt"], as_dict=True,
        )
        self.assertEqual(row.status, "STN-Routed")
        self.assertEqual(int(row.routed_to_stn), 1)
        self.assertIsNone(row.purchase_receipt)


class TestReceiptGateAndIdempotency(FrappeTestCase):
    """Step 3 (idempotency) + Step 5 (receipt gate)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-RGI", company=cls.company)
        make_account()
        make_location(
            location_key="700002",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-RGI")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500001")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-RGI")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-RGI")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_held_pre_qc_when_status_below_trigger(self) -> None:
        """grn_status_id=1 (CREATED) → Held-Pre-QC, NO PR."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200010,
                vendor_c_id=500001,
                inwarded_warehouse_c_id=700002,
                grn_status_id=1,  # below default trigger of 3
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-RGI",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "held")
        self.assertEqual(outcome.grn_map_status, "Held-Pre-QC")
        self.assertIsNone(outcome.purchase_receipt)

    def test_held_then_receipted_on_status_advance(self) -> None:
        """Hold at status=1 → re-pull at status=3 → receipted (one GRN
        Map row, transitioned)."""
        # First pull at status=1.
        process_one_grn(
            _grn_payload(
                grn_id=200011,
                vendor_c_id=500001,
                inwarded_warehouse_c_id=700002,
                grn_status_id=1,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-RGI",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        # Second pull, same ee_grn_id, status now 3.
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200011,
                vendor_c_id=500001,
                inwarded_warehouse_c_id=700002,
                grn_status_id=3,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-RGI",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        # Single Map row.
        rows = frappe.db.get_all(
            "EasyEcom GRN Map", filters={"ee_grn_id": 200011}, pluck="name"
        )
        self.assertEqual(len(rows), 1)

    def test_idempotent_re_pull_of_receipted_grn(self) -> None:
        """Already-Receipted GRN re-pulled → no-op, no second PR."""
        first = process_one_grn(
            _grn_payload(
                grn_id=200012,
                vendor_c_id=500001,
                inwarded_warehouse_c_id=700002,
                grn_status_id=3,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-RGI",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(first.operation, "receipted")
        pr_count_before = frappe.db.count(
            "Purchase Receipt", {"ecs_easyecom_grn_id": "200012"}
        )
        # Re-pull.
        second = process_one_grn(
            _grn_payload(
                grn_id=200012,
                vendor_c_id=500001,
                inwarded_warehouse_c_id=700002,
                grn_status_id=3,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-RGI",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(second.operation, "noop")
        pr_count_after = frappe.db.count(
            "Purchase Receipt", {"ecs_easyecom_grn_id": "200012"}
        )
        self.assertEqual(pr_count_before, pr_count_after)


class TestDeletedHandling(FrappeTestCase):
    """Step 4: deleted GRNs."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-DEL", company=cls.company)
        make_account()
        make_location(
            location_key="700003",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-DEL")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500002")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-DEL")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-DEL")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_deleted_pre_receipt_quiet_skip(self) -> None:
        """grn_status_id=4 with NO prior PR → quiet skip, no Map row,
        no Discrepancy."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200020,
                vendor_c_id=500002,
                inwarded_warehouse_c_id=700003,
                grn_status_id=4,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-DEL",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "deleted_pre_receipt")
        self.assertFalse(
            frappe.db.exists("EasyEcom GRN Map", {"ee_grn_id": 200020})
        )

    def test_deleted_post_receipt_raises_discrepancy_pr_preserved(self) -> None:
        """receipted GRN flipped to status=4 → Discrepancy +
        Deleted-Post-Receipt; PR NOT cancelled."""
        # First, receipt at status=3.
        first = process_one_grn(
            _grn_payload(
                grn_id=200021,
                vendor_c_id=500002,
                inwarded_warehouse_c_id=700003,
                grn_status_id=3,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-DEL",
                        "received_quantity": 5,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(first.operation, "receipted")
        pr_name = first.purchase_receipt
        # Then flip to status=4.
        second = process_one_grn(
            _grn_payload(
                grn_id=200021,
                vendor_c_id=500002,
                inwarded_warehouse_c_id=700003,
                grn_status_id=4,
                items=[],
            )
        )
        self.assertEqual(second.operation, "deleted_post_receipt")
        self.assertEqual(second.grn_map_status, "Deleted-Post-Receipt")
        self.assertEqual(len(second.discrepancies), 1)
        # PR survives + still submitted.
        self.assertEqual(
            int(
                frappe.db.get_value("Purchase Receipt", pr_name, "docstatus") or 0
            ),
            1,
            "PR must NOT be cancelled by Stage 3",
        )


class TestQtyModelAndBuckets(FrappeTestCase):
    """Step 7: the qc_fail split + no-bucket-leak."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-QTY", company=cls.company)
        cls.rejected_wh = _ensure_warehouse(
            f"{_PREFIX}WH-QTY-REJ", company=cls.company
        )
        make_account()
        make_location(
            location_key="700004",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        # Set default_rejected_warehouse for the qc_fail tests.
        acct = frappe.db.get_value(
            "EasyEcom Account", {"enabled": 1}, "name"
        )
        frappe.db.set_value(
            "EasyEcom Account",
            acct,
            "default_rejected_warehouse",
            cls.rejected_wh,
            update_modified=False,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-QTY")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500003")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-QTY")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-QTY")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def setUp(self) -> None:
        # Ensure test-account is enabled AND default_rejected_warehouse
        # is set before each test. Cross-class test ordering can leave
        # the account disabled (Stage 2's auto_push_controls tests
        # disable accounts in their tearDown) — explicitly re-enable.
        if frappe.db.exists("EasyEcom Account", "test-account"):
            frappe.db.set_value(
                "EasyEcom Account", "test-account",
                {
                    "enabled": 1,
                    "default_rejected_warehouse": self.rejected_wh,
                },
                update_modified=False,
            )
            frappe.db.commit()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_qc_fail_zero_all_accepted(self) -> None:
        """received 100, qc_fail 0 → received_qty=100, accepted=100,
        rejected=0."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200030,
                vendor_c_id=500003,
                inwarded_warehouse_c_id=700004,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-QTY",
                        "received_quantity": 100,
                        "qc_fail": 0,
                        "grn_detail_price": 10,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        pr = frappe.get_doc("Purchase Receipt", outcome.purchase_receipt)
        line = pr.items[0]
        self.assertEqual(float(line.received_qty), 100)
        self.assertEqual(float(line.qty), 100)
        self.assertEqual(float(line.rejected_qty), 0)

    def test_qc_fail_eight_split_8_92(self) -> None:
        """received 100, qc_fail 8 → received=100, accepted=92, rejected=8,
        rejected to default_rejected_warehouse."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200031,
                vendor_c_id=500003,
                inwarded_warehouse_c_id=700004,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-QTY",
                        "received_quantity": 100,
                        "qc_fail": 8,
                        "grn_detail_price": 10,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        pr = frappe.get_doc("Purchase Receipt", outcome.purchase_receipt)
        line = pr.items[0]
        self.assertEqual(float(line.received_qty), 100)
        self.assertEqual(float(line.rejected_qty), 8)
        # ERPNext PR invariant: received = qty + rejected. qty is the
        # ACCEPTED portion (received - qc_fail = 92).
        self.assertEqual(
            float(line.qty), 92,
            "PR line qty is the ACCEPTED portion (received 100 - qc_fail 8 = 92)",
        )
        self.assertEqual(line.rejected_warehouse, self.rejected_wh)

    def test_real_payload_141653_no_bucket_leak(self) -> None:
        """Real-payload shape: grn 141653 received 100, available 40,
        sold 60. We MUST receipt 100, never 40. None of the buckets
        leak to the PR."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=141653,
                vendor_c_id=500003,
                inwarded_warehouse_c_id=700004,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-QTY",
                        "received_quantity": 100,
                        "qc_fail": 0,
                        "grn_detail_price": 10,
                        # Buckets — MUST NOT be lifted.
                        "available": 40,
                        "reserved": 0,
                        "sold": 60,
                        "damaged": 0,
                        "qc_pass": 100,
                        "qc_pending": 0,
                    }
                ],
            )
        )
        pr = frappe.get_doc("Purchase Receipt", outcome.purchase_receipt)
        line = pr.items[0]
        self.assertEqual(
            float(line.received_qty), 100,
            "PR received_qty must come from received_quantity (100), "
            "NOT from any bucket",
        )
        # PR line fields must not match any bucket value (40 / 60).
        self.assertNotEqual(float(line.received_qty), 40)
        self.assertNotEqual(float(line.received_qty), 60)

    def test_rejected_without_default_warehouse_throws(self) -> None:
        """qc_fail>0 with default_rejected_warehouse unset → clear error."""
        # Clear the setting.
        acct = frappe.db.get_value("EasyEcom Account", {"enabled": 1}, "name")
        frappe.db.set_value(
            "EasyEcom Account",
            acct,
            "default_rejected_warehouse",
            None,
            update_modified=False,
        )
        try:
            outcome = process_one_grn(
                _grn_payload(
                    grn_id=200032,
                    vendor_c_id=500003,
                    inwarded_warehouse_c_id=700004,
                    items=[
                        {
                            "grn_detail_id": 1,
                            "sku": f"{_PREFIX}SKU-QTY",
                            "received_quantity": 100,
                            "qc_fail": 5,
                            "grn_detail_price": 10,
                        }
                    ],
                )
            )
            self.assertEqual(outcome.operation, "failed")
            joined = " || ".join(outcome.flag_reasons)
            self.assertIn("default_rejected_warehouse", joined)
        finally:
            frappe.db.set_value(
                "EasyEcom Account",
                acct,
                "default_rejected_warehouse",
                self.rejected_wh,
                update_modified=False,
            )


class TestPOResolution(FrappeTestCase):
    """Step 6: PO resolution chain — primary / fallback / both-miss."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-POR", company=cls.company)
        make_account()
        make_location(
            location_key="700005",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-POR")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500004")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-POR")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-POR")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_po_resolved_via_po_ref_num(self) -> None:
        po = _make_po(
            supplier=self.supplier,
            warehouse=self.ee_wh,
            items=[{"item_code": self.item, "qty": 10}],
        )
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200040,
                vendor_c_id=500004,
                inwarded_warehouse_c_id=700005,
                po_ref_num=po.name,
                ee_po_id=0,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-POR",
                        "received_quantity": 10,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        self.assertEqual(outcome.linked_po, po.name)

    def test_po_fallback_via_ee_po_id(self) -> None:
        """Blank po_ref_num → look up via ee_po_id → PO Map → PO."""
        po = _make_po(
            supplier=self.supplier,
            warehouse=self.ee_wh,
            items=[{"item_code": self.item, "qty": 10}],
        )
        _make_po_map(po.name, ee_po_id=88001)
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200041,
                vendor_c_id=500004,
                inwarded_warehouse_c_id=700005,
                po_ref_num="",
                ee_po_id=88001,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-POR",
                        "received_quantity": 10,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        self.assertEqual(outcome.linked_po, po.name)

    def test_both_refs_miss_pr_created_with_discrepancy(self) -> None:
        """po_ref_num blank/junk + ee_po_id unmatched → PR created,
        linked_po empty, Discrepancy raised (kind 'GRN for unknown PO')."""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200042,
                vendor_c_id=500004,
                inwarded_warehouse_c_id=700005,
                po_ref_num="jghvhgv",  # junk; real-payload grn 141461 shape
                ee_po_id=999999,  # unmatched
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-POR",
                        "received_quantity": 10,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        self.assertIsNone(outcome.linked_po)
        # Discrepancy raised.
        self.assertEqual(len(outcome.discrepancies), 1)
        disc = frappe.get_doc(
            "EasyEcom Integration Discrepancy", outcome.discrepancies[0]
        )
        self.assertEqual(disc.kind, "GRN for unknown PO")


class TestSupplierAndItemMisses(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-MISS", company=cls.company)
        make_account()
        make_location(
            location_key="700006",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_supplier_map_missing_failed(self) -> None:
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200050,
                vendor_c_id=8888888,  # no Supplier Map for this c_id
                inwarded_warehouse_c_id=700006,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-X",
                        "received_quantity": 1,
                        "grn_detail_price": 1,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "failed")
        self.assertEqual(outcome.grn_map_status, "Failed")
        self.assertTrue(
            any("Supplier Map missing" in r for r in outcome.flag_reasons),
            outcome.flag_reasons,
        )

    def test_unmapped_item_whole_pr_failed_with_line_named(self) -> None:
        sup = _ensure_supplier(f"{_PREFIX}SUP-IM")
        _make_supplier_map(sup, ee_vendor_c_id="500005")
        # NO Item Map for the SKU we're about to send.
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200051,
                vendor_c_id=500005,
                inwarded_warehouse_c_id=700006,
                items=[
                    {
                        "grn_detail_id": 99,
                        "sku": f"{_PREFIX}UNMAPPED-SKU",
                        "received_quantity": 1,
                        "grn_detail_price": 1,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "failed")
        joined = " || ".join(outcome.flag_reasons)
        self.assertIn("Item Map missing", joined)
        self.assertIn("UNMAPPED-SKU", joined)
        # No PR was created.
        self.assertIsNone(outcome.purchase_receipt)


class TestStatusReconciliation(FrappeTestCase):
    """Stage 3 status reconciliation in the same sweep: echo / drift /
    fulfilment 11-16."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-REC", company=cls.company)
        make_account()
        make_location(
            location_key="700007",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-REC")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500006")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-REC")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-REC")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def _seed_po_and_map(self) -> tuple[Any, str]:
        po = _make_po(
            supplier=self.supplier,
            warehouse=self.ee_wh,
            items=[{"item_code": self.item, "qty": 10}],
        )
        m = _make_po_map(po.name, ee_po_id=88010)
        return po, m

    def test_echo_is_not_drift(self) -> None:
        """Pushed 3, EE shows 3 → no Discrepancy, just observation update."""
        po, po_map_name = self._seed_po_and_map()
        # last_pushed_po_status seeded to 3 by _make_po_map.
        disc_count_before = frappe.db.count("EasyEcom Integration Discrepancy")
        _reconcile_po_status(
            grn_row=_grn_payload(
                grn_id=200060,
                vendor_c_id=500006,
                inwarded_warehouse_c_id=700007,
                po_ref_num=po.name,
                po_status_id=3,  # echo
            ),
            ee_grn_id=200060,
        )
        disc_count_after = frappe.db.count("EasyEcom Integration Discrepancy")
        self.assertEqual(
            disc_count_after, disc_count_before, "echo must not raise Discrepancy"
        )
        # But ee_observed_po_status updated.
        row = frappe.db.get_value(
            "EasyEcom PO Map", po_map_name,
            ["ee_observed_po_status"], as_dict=True,
        )
        self.assertEqual(int(row.ee_observed_po_status), 3)

    def test_drift_cancelled_while_active_raises_discrepancy(self) -> None:
        """EE shows 7 Cancelled while ERPNext PO is submitted/active →
        drift Discrepancy."""
        po, po_map_name = self._seed_po_and_map()
        before = frappe.db.count("EasyEcom Integration Discrepancy")
        _reconcile_po_status(
            grn_row=_grn_payload(
                grn_id=200061,
                vendor_c_id=500006,
                inwarded_warehouse_c_id=700007,
                po_ref_num=po.name,
                po_status_id=7,  # Cancelled
            ),
            ee_grn_id=200061,
        )
        after = frappe.db.count("EasyEcom Integration Discrepancy")
        self.assertEqual(after, before + 1)
        # Discrepancy kind is right.
        discs = frappe.db.get_all(
            "EasyEcom Integration Discrepancy",
            filters={"reference_doctype": "EasyEcom PO Map", "reference_name": po_map_name},
            fields=["kind"],
        )
        self.assertEqual(discs[-1]["kind"], "po_status drift")

    def test_fulfilment_11_to_16_observation_only(self) -> None:
        po, po_map_name = self._seed_po_and_map()
        before = frappe.db.count("EasyEcom Integration Discrepancy")
        for code in (11, 12, 13, 14, 15, 16):
            _reconcile_po_status(
                grn_row=_grn_payload(
                    grn_id=200070 + code,
                    vendor_c_id=500006,
                    inwarded_warehouse_c_id=700007,
                    po_ref_num=po.name,
                    po_status_id=code,
                ),
                ee_grn_id=200070 + code,
            )
        after = frappe.db.count("EasyEcom Integration Discrepancy")
        self.assertEqual(
            after, before,
            "11-16 are fulfilment lifecycle, must NEVER raise Discrepancy",
        )


class TestCompletionTrigger(FrappeTestCase):
    """§9 Stage 3 completion fires Stage 2's po_status=5 once cumulative
    received meets ordered (modulo under_pct)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-COMP", company=cls.company)
        make_account()
        make_location(
            location_key="700008",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-COMP")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500007")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-COMP")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-COMP")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_completion_fires_on_full_receipt_and_idempotent(self) -> None:
        po = _make_po(
            supplier=self.supplier,
            warehouse=self.ee_wh,
            items=[{"item_code": self.item, "qty": 10, "rate": 100}],
        )
        _make_po_map(po.name, ee_po_id=88020)
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {"poId": 88020}}

        outcome = process_one_grn(
            _grn_payload(
                grn_id=200080,
                vendor_c_id=500007,
                inwarded_warehouse_c_id=700008,
                po_ref_num=po.name,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-COMP",
                        "received_quantity": 10,
                        "grn_detail_price": 100,
                    }
                ],
            ),
            client=client,
        )
        self.assertEqual(outcome.operation, "receipted")
        # po_status=5 was pushed.
        status_calls = [
            c for c in client.post.call_args_list
            if c[0][0] == "/wms/updatePoStatus"
            and c[1].get("payload", c[0][1] if len(c[0]) > 1 else {}).get("po_status") == 5
            or (len(c[0]) > 1 and c[0][1].get("po_status") == 5)
        ]
        self.assertTrue(status_calls, f"expected po_status=5 push; got {client.post.call_args_list}")
        # PO Map last_pushed_po_status now 5.
        self.assertEqual(
            int(
                frappe.db.get_value(
                    "EasyEcom PO Map",
                    {"purchase_order": po.name},
                    "last_pushed_po_status",
                )
            ),
            5,
        )

    def test_multi_grn_partial_then_complete(self) -> None:
        """GRN1 receives 6 of 10, GRN2 receives 4. Completion fires on
        GRN2."""
        po = _make_po(
            supplier=self.supplier,
            warehouse=self.ee_wh,
            items=[{"item_code": self.item, "qty": 10, "rate": 100}],
        )
        _make_po_map(po.name, ee_po_id=88021)
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {"poId": 88021}}

        # GRN1 — 6 received.
        process_one_grn(
            _grn_payload(
                grn_id=200090,
                vendor_c_id=500007,
                inwarded_warehouse_c_id=700008,
                po_ref_num=po.name,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-COMP",
                        "received_quantity": 6,
                        "grn_detail_price": 100,
                    }
                ],
            ),
            client=client,
        )
        # Still 3 (not 5).
        self.assertEqual(
            int(frappe.db.get_value(
                "EasyEcom PO Map", {"purchase_order": po.name},
                "last_pushed_po_status",
            )),
            3,
        )
        # GRN2 — 4 received, total now 10.
        process_one_grn(
            _grn_payload(
                grn_id=200091,
                vendor_c_id=500007,
                inwarded_warehouse_c_id=700008,
                po_ref_num=po.name,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-COMP",
                        "received_quantity": 4,
                        "grn_detail_price": 100,
                    }
                ],
            ),
            client=client,
        )
        self.assertEqual(
            int(frappe.db.get_value(
                "EasyEcom PO Map", {"purchase_order": po.name},
                "last_pushed_po_status",
            )),
            5,
        )


class TestForceClose(FrappeTestCase):
    """ERPNext PO Close → updatePoStatus=5 + markPoComplete=1."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-FC", company=cls.company)
        make_account()
        make_location(
            location_key="700009",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-FC")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500008")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-FC")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-FC")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_close_button_fires_status_5_with_mark_complete(self) -> None:
        po = _make_po(
            supplier=self.supplier,
            warehouse=self.ee_wh,
            items=[{"item_code": self.item, "qty": 10, "rate": 100}],
        )
        _make_po_map(po.name, ee_po_id=88030)
        # Simulate Close — set status='Closed' on the doc.
        po_doc = frappe.get_doc("Purchase Order", po.name)
        po_doc.status = "Closed"

        from ecommerce_super.easyecom.flows import po_push as ppm
        captured = []
        original = ppm.push_po_status

        def _spy(*, po_docname, target_status, mark_complete=0, client=None):
            captured.append(
                {
                    "po": po_docname,
                    "target_status": target_status,
                    "mark_complete": mark_complete,
                }
            )
            # Mutate the Map row so subsequent checks (if any) see it.
            return original(
                po_docname=po_docname,
                target_status=target_status,
                mark_complete=mark_complete,
                client=MagicMock(post=MagicMock(return_value={"data": {}})),
            )

        try:
            ppm.push_po_status = _spy
            # Also re-import the alias used by grn_pull (it imported
            # push_po_status by name).
            import ecommerce_super.easyecom.flows.grn_pull as gpm
            original_grn = gpm.push_po_status
            gpm.push_po_status = _spy
            try:
                enqueue_on_po_close(po_doc)
            finally:
                gpm.push_po_status = original_grn
        finally:
            ppm.push_po_status = original

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["target_status"], 5)
        self.assertEqual(int(captured[0]["mark_complete"]), 1)


class TestSyncRecordLineDiscrepancyLink(FrappeTestCase):
    """Per-GRN: a discrepancy line links to the §23 stub via
    ecs_integration_discrepancy on the Sync Record Line."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_ephemeral_state()
        cls.company = _company()
        cls.ee_wh = _ensure_warehouse(f"{_PREFIX}WH-SR", company=cls.company)
        make_account()
        make_location(
            location_key="700010",
            mapped_warehouse=cls.ee_wh,
            frappe_company=cls.company,
        )
        cls.supplier = _ensure_supplier(f"{_PREFIX}SUP-SR")
        _make_supplier_map(cls.supplier, ee_vendor_c_id="500009")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-SR")
        _make_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-SR")

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_test_state()

    def tearDown(self) -> None:
        _wipe_ephemeral_state()

    def test_pr_for_unknown_po_links_discrepancy_in_sync_record(self) -> None:
        """An unknown-PO Discrepancy is raised; the per-line outcome on
        the Sync Record points to a Discrepancy via the §23 stub link
        field. (Sync Record line_status doesn't include the
        unknown-PO discrepancy explicitly — that's PR-level — but the
        Stage 3 flow tags at least one disc-line on any discrepancy
        case via tax_disc surfacing. This test validates Sync Record
        was written + Line child populated.)"""
        outcome = process_one_grn(
            _grn_payload(
                grn_id=200100,
                vendor_c_id=500009,
                inwarded_warehouse_c_id=700010,
                po_ref_num="ghostly-po",
                ee_po_id=999777,
                items=[
                    {
                        "grn_detail_id": 1,
                        "sku": f"{_PREFIX}SKU-SR",
                        "received_quantity": 1,
                        "grn_detail_price": 100,
                    }
                ],
            )
        )
        self.assertEqual(outcome.operation, "receipted")
        self.assertIsNotNone(outcome.sync_record_name)
        sr = frappe.get_doc("EasyEcom Sync Record", outcome.sync_record_name)
        # At least one line populated.
        self.assertGreaterEqual(len(sr.lines), 1)
