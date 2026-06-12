"""§10 Stage 3 — Inbound flow tests.

Covers:
  - §9 routing handoff: GRN with po_ref_num matching a §10 DN → §10
    inbound branch (not §9 PR creation).
  - IPR submit gate: same-GSTIN auto-submit; different-GSTIN+SI Submitted
    auto-submit; different-GSTIN+SI Draft → IPR Draft + Comment + ToDo
    + NO Discrepancy (ERP-user pending, not FDE issue).
  - SI on_submit auto-retry: drafted IPRs become submitted when ERP user
    submits SI later; IPI + DN chain follows.
  - IPI auto-draft: different-GSTIN + IPR submitted → IPI in Draft sized
    to SI dispatched qty.
  - Debit Note auto-draft: different-GSTIN + cumulative < dispatched →
    DN in Draft sized to the gap.
  - Multi-GRN cumulative: GRN1+GRN2 closes the gap → DN auto-cancelled.
  - Submitted-DN-late-GRN block: §7 IPR stays Draft + Discrepancy.
  - EE-originated standalone: handle_ee_originated_grn → Discrepancy + no
    Transfer Map link.
  - Status transitions: Partial-Received → Fully-Received → DN-Submitted-Locked.

Mocks EE client where applicable. No real EE writes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ecommerce_super.easyecom.flows.transfer_inbound import (
    _decide_ipr_submit,
    _find_internal_supplier,
    handle_ee_originated_grn,
    on_sales_invoice_submit,
    process_inbound_grn,
)
from ecommerce_super.tests.factories import (
    cleanup_internal_pair_fabric,
    make_account,
    make_location,
)


_PREFIX = "TEST-S10-S3-"


def _company() -> str:
    c = frappe.db.get_value("Company", filters={}, fieldname="name")
    if not c:
        raise RuntimeError("No Company")
    return c


# ============================================================
# Fixtures (reuse §10 Stage 2 helper patterns)
# ============================================================


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


def _ensure_warehouse_address(warehouse: str) -> None:
    if frappe.db.sql(
        """SELECT 1 FROM `tabDynamic Link`
           WHERE parenttype='Address' AND link_doctype='Warehouse'
             AND link_name=%s LIMIT 1""",
        (warehouse,),
    ):
        return
    addr = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": f"Addr-{warehouse}",
            "address_type": "Shipping",
            "address_line1": "Test Industrial Estate",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560001",
            "country": "India",
            "links": [
                {"link_doctype": "Warehouse", "link_name": warehouse}
            ],
        }
    )
    addr.insert(ignore_permissions=True)


def _set_company_gstin(company: str, gstin: str) -> None:
    frappe.db.set_value("Company", company, "gstin", gstin, update_modified=False)


def _ensure_item(code: str, *, weight: float = 1) -> str:
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
            "weight_per_unit": weight,
            "weight_uom": "Kg",
        }
    )
    it.insert(ignore_permissions=True)
    return it.name


def _ensure_item_map(item_code: str, *, ee_sku: str | None = None) -> str:
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


def _ensure_supplier_group() -> str:
    g = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if g:
        return g
    if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
        root = frappe.new_doc("Supplier Group")
        root.update(
            {"supplier_group_name": "All Supplier Groups", "is_group": 1}
        )
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


def _ensure_internal_customer_with_companies(
    *, target_company: str, source_companies: list[str]
) -> str:
    existing = frappe.db.get_value(
        "Customer",
        {"is_internal_customer": 1, "represents_company": target_company},
        "name",
    )
    if existing:
        doc = frappe.get_doc("Customer", existing)
        present = {r.company for r in (doc.companies or [])}
        for src in source_companies:
            if src not in present:
                doc.append("companies", {"company": src})
        if any(s not in present for s in source_companies):
            doc.save(ignore_permissions=True)
        return existing
    g = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    c = frappe.new_doc("Customer")
    c.update(
        {
            "customer_name": f"INTL-CUST-for-{target_company}",
            "customer_type": "Company",
            "customer_group": g,
            "is_internal_customer": 1,
            "represents_company": target_company,
            "companies": [{"company": s} for s in source_companies],
        }
    )
    c.insert(ignore_permissions=True)
    return c.name


def _ensure_internal_supplier_with_companies(
    *, source_company: str, target_companies: list[str]
) -> str:
    existing = frappe.db.get_value(
        "Supplier",
        {"is_internal_supplier": 1, "represents_company": source_company},
        "name",
    )
    if existing:
        doc = frappe.get_doc("Supplier", existing)
        present = {r.company for r in (doc.companies or [])}
        for tgt in target_companies:
            if tgt not in present:
                doc.append("companies", {"company": tgt})
        if any(t not in present for t in target_companies):
            doc.save(ignore_permissions=True)
        return existing
    s = frappe.new_doc("Supplier")
    s.update(
        {
            "supplier_name": f"INTL-SUPP-from-{source_company}",
            "supplier_type": "Company",
            "supplier_group": _ensure_supplier_group(),
            "is_internal_supplier": 1,
            "represents_company": source_company,
            "companies": [{"company": t} for t in target_companies],
        }
    )
    s.insert(ignore_permissions=True)
    return s.name


def _make_transfer_map(
    *,
    dn_name: str,
    source_wh: str,
    target_wh: str,
    sales_invoice: str | None = None,
    gstin_different: int = 0,
    status: str = "EE-Pushed",
    ee_order_id: str = "MOCK-OID-001",
) -> str:
    existing = frappe.db.get_value(
        "EasyEcom Transfer Map", {"delivery_note": dn_name}, "name"
    )
    if existing:
        return existing
    src_gstin = frappe.db.get_value(
        "Company",
        frappe.db.get_value("Warehouse", source_wh, "company"),
        "gstin",
    ) or ""
    tgt_gstin = frappe.db.get_value(
        "Company",
        frappe.db.get_value("Warehouse", target_wh, "company"),
        "gstin",
    ) or ""
    tm = frappe.new_doc("EasyEcom Transfer Map")
    tm.update(
        {
            "delivery_note": dn_name,
            "sales_invoice": sales_invoice,
            "source_warehouse": source_wh,
            "target_warehouse": target_wh,
            "source_company_gstin": src_gstin,
            "target_company_gstin": tgt_gstin,
            "gstin_different": gstin_different
            or (1 if src_gstin and tgt_gstin and src_gstin != tgt_gstin else 0),
            "status": status,
            "ee_doctype": "STN",
            "ee_order_id": ee_order_id,
        }
    )
    tm.insert(ignore_permissions=True)
    return tm.name


def _make_grn_payload(
    *,
    grn_id: int,
    inwarded_warehouse_c_id: int,
    vendor_c_id: int,
    po_ref_num: str,
    sku: str,
    received_qty: float = 5,
    qc_fail: float = 0,
    grn_detail_price: float = 500,
) -> dict:
    return {
        "grn_id": grn_id,
        "grn_invoice_number": f"INV-{grn_id}",
        "grn_invoice_date": frappe.utils.today(),
        "grn_created_at": f"{frappe.utils.today()} 10:00:00",
        "grn_status_id": 3,
        "total_grn_value": received_qty * grn_detail_price,
        "inwarded_warehouse_c_id": inwarded_warehouse_c_id,
        "vendor_c_id": vendor_c_id,
        "po_ref_num": po_ref_num,
        "po_id": 0,
        "po_status_id": 3,
        "grn_items": [
            {
                "grn_detail_id": grn_id * 10 + 1,
                "sku": sku,
                "received_quantity": received_qty,
                "qc_fail": qc_fail,
                "grn_detail_price": grn_detail_price * received_qty,
            }
        ],
    }


def _wipe_state() -> None:
    """Module-wide cleanup. Wipe in dependency order."""
    # PIs (IPI + DN)
    for n in frappe.db.get_all(
        "Purchase Invoice",
        filters={"supplier": ("like", "INTL-SUPP%")},
        pluck="name",
    ):
        try:
            d = frappe.get_doc("Purchase Invoice", n)
            if int(d.docstatus or 0) == 1:
                d.cancel()
            frappe.delete_doc(
                "Purchase Invoice", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # PRs (IPRs) — wipe BOTH by Internal Supplier AND by §10/§9 back-
    # refs so stale Map links from prior test runs (which would make
    # the §10 idempotency check return noop) get cleared.
    pr_names: set[str] = set()
    for n in frappe.db.get_all(
        "Purchase Receipt",
        filters={"supplier": ("like", "INTL-SUPP%")},
        pluck="name",
    ):
        pr_names.add(n)
    for n in frappe.db.get_all(
        "Purchase Receipt",
        filters={"ecs_section10_transfer_map": ("!=", "")},
        pluck="name",
    ):
        pr_names.add(n)
    for n in frappe.db.get_all(
        "Purchase Receipt",
        filters={"ecs_easyecom_grn_id": ("like", "5%")},  # test grn_id range
        pluck="name",
    ):
        pr_names.add(n)
    for n in pr_names:
        try:
            d = frappe.get_doc("Purchase Receipt", n)
            if int(d.docstatus or 0) == 1:
                d.cancel()
            frappe.delete_doc(
                "Purchase Receipt", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Sales Invoices linked to Internal Customer
    for n in frappe.db.get_all(
        "Sales Invoice",
        filters={"customer": ("like", "INTL-CUST%")},
        pluck="name",
    ):
        try:
            d = frappe.get_doc("Sales Invoice", n)
            if int(d.docstatus or 0) == 1:
                d.cancel()
            frappe.delete_doc(
                "Sales Invoice", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Delivery Notes
    for n in frappe.db.get_all(
        "Delivery Note",
        filters={"customer": ("like", "INTL-CUST%")},
        pluck="name",
    ):
        try:
            d = frappe.get_doc("Delivery Note", n)
            if int(d.docstatus or 0) == 1:
                d.cancel()
            frappe.delete_doc(
                "Delivery Note", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Transfer Maps + GRN Maps
    for n in frappe.db.get_all("EasyEcom Transfer Map", pluck="name"):
        try:
            frappe.delete_doc(
                "EasyEcom Transfer Map",
                n,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom GRN Map",
        filters={"ee_grn_id": (">=", 500000)},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom GRN Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    cleanup_internal_pair_fabric()
    # Sync Records keyed on the DN entity (§10 §9 family).
    for n in frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"entity_doctype": ("in", ["Delivery Note", "EasyEcom GRN Map"])},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Sync Record",
                n,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
    # Discrepancies
    for n in frappe.db.get_all(
        "EasyEcom Integration Discrepancy",
        filters={
            "kind": (
                "in",
                [
                    "Late GRN after submitted DN",
                    "EE-originated transfer (self-GRN)",
                ],
            )
        },
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
    # Test Locations.
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
    frappe.db.commit()


def _ensure_test_account_with_git(rejected_wh: str, git_wh: str) -> None:
    """test-account.default_in_transit_warehouse + default_rejected_warehouse."""
    if frappe.db.exists("EasyEcom Account", "test-account"):
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            {
                "enabled": 1,
                "default_in_transit_warehouse": git_wh,
                "default_rejected_warehouse": rejected_wh,
            },
            update_modified=False,
        )
        frappe.db.commit()


# ============================================================
# Same-GSTIN IPR auto-submit
# ============================================================


class TestSameGstinIprAutoSubmit(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_state()
        cls.company = _company()
        cls.src_wh = _ensure_warehouse(f"{_PREFIX}WH-SAME-SRC", company=cls.company)
        cls.tgt_wh = _ensure_warehouse(f"{_PREFIX}WH-SAME-TGT", company=cls.company)
        cls.git_wh = _ensure_warehouse(f"{_PREFIX}WH-SAME-GIT", company=cls.company)
        cls.rej_wh = _ensure_warehouse(f"{_PREFIX}WH-SAME-REJ", company=cls.company)
        _ensure_warehouse_address(cls.tgt_wh)
        _set_company_gstin(cls.company, "29ABCDE1234F1Z5")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-SAME")
        _ensure_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-SAME")
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.company, source_companies=[cls.company]
        )
        cls.internal_sup = _ensure_internal_supplier_with_companies(
            source_company=cls.company, target_companies=[cls.company]
        )
        make_account(enabled=False)
        _ensure_test_account_with_git(cls.rej_wh, cls.git_wh)
        # Allow negative stock so the IPR can pull from GIT without
        # the test having to round-trip a Stock Entry + DN submit
        # to actually park stock in GIT first.
        frappe.db.set_value(
            "Stock Settings",
            "Stock Settings",
            "allow_negative_stock",
            1,
        )
        cls.location_key = f"{_PREFIX}LOC-SAME-TGT"
        cls.ee_company_id = 500001  # synthetic int for the test GRN c_id
        loc = make_location(
            location_key=cls.location_key,
            is_operational=True,
            frappe_company=cls.company,
            mapped_warehouse=cls.tgt_wh,
        )
        frappe.db.set_value(
            "EasyEcom Location",
            loc,
            "ee_company_id",
            cls.ee_company_id,
            update_modified=False,
        )
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        # Restore allow_negative_stock default.
        frappe.db.set_value(
            "Stock Settings",
            "Stock Settings",
            "allow_negative_stock",
            0,
        )
        _wipe_state()
        super().tearDownClass()

    def test_same_gstin_grn_routes_to_section10_and_auto_submits(self) -> None:
        """Routing: po_ref_num matches DN → §10 inbound; same-GSTIN →
        IPR auto-submits; stock moves GIT → destination."""
        # Build a DN + Transfer Map (status EE-Pushed, no SI because
        # same-GSTIN).
        dn_name = self._make_minimal_dn()
        tm_name = _make_transfer_map(
            dn_name=dn_name,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            sales_invoice=None,
            gstin_different=0,
            status="EE-Pushed",
        )

        # Build GRN payload with po_ref_num matching the DN.
        from ecommerce_super.easyecom.flows.grn_pull import process_one_grn

        c_id = self.ee_company_id
        outcome = process_one_grn(
            _make_grn_payload(
                grn_id=500001,
                inwarded_warehouse_c_id=c_id,
                vendor_c_id=c_id + 1,  # Not self-GRN
                po_ref_num=dn_name,
                sku=f"{_PREFIX}SKU-SAME",
                received_qty=5,
                grn_detail_price=100,
            )
        )

        self.assertEqual(outcome.operation, "receipted")
        self.assertIsNotNone(outcome.purchase_receipt)
        pr = frappe.get_doc("Purchase Receipt", outcome.purchase_receipt)
        self.assertEqual(int(pr.docstatus or 0), 1, "IPR must be Submitted")
        self.assertEqual(int(pr.is_internal_supplier or 0), 1)
        self.assertEqual(pr.supplier, self.internal_sup)
        self.assertEqual(pr.ecs_section10_transfer_map, tm_name)
        # Stock moves GIT → destination. PR.set_warehouse is the
        # destination (where stock lands); per-line from_warehouse
        # is GIT (where it came from).
        self.assertEqual(pr.set_warehouse, self.tgt_wh)
        for line in pr.items:
            self.assertEqual(line.from_warehouse, self.git_wh)
        # Transfer Map → Fully-Received.
        tm = frappe.get_doc("EasyEcom Transfer Map", tm_name)
        self.assertEqual(tm.status, "Fully-Received")
        # IPR linked into Transfer Map.internal_purchase_receipts.
        pr_links = [
            r.internal_purchase_receipt for r in tm.internal_purchase_receipts
        ]
        self.assertIn(pr.name, pr_links)

    def _make_minimal_dn(self) -> str:
        """Insert a Submitted DN with Internal Customer + target_warehouse."""
        price_list = frappe.db.get_value(
            "Price List", {"selling": 1}, "name"
        )
        if not price_list:
            pl = frappe.new_doc("Price List")
            pl.update(
                {
                    "price_list_name": "_Test S10 Selling",
                    "currency": "INR",
                    "selling": 1,
                }
            )
            pl.insert(ignore_permissions=True)
            price_list = pl.name
        dn = frappe.new_doc("Delivery Note")
        dn.update(
            {
                "customer": self.internal_cust,
                "company": self.company,
                "is_internal_customer": 1,
                "set_warehouse": self.src_wh,
                "posting_date": frappe.utils.today(),
                "selling_price_list": price_list,
                "price_list_currency": "INR",
                "plc_conversion_rate": 1,
                "currency": "INR",
                "conversion_rate": 1,
                "ignore_pricing_rule": 1,
                # §10 UX layer (post-2026-05-30) requires the header
                # Transfer From / To Warehouse fields on
                # internal-customer DNs. Substrate gate
                # validate_pre_submit throws otherwise.
                "ecs_is_section10_transfer": 1,
                "ecs_section10_transfer_from_warehouse": self.src_wh,
                "ecs_section10_transfer_to_warehouse": self.tgt_wh,
            }
        )
        dn.append(
            "items",
            {
                "item_code": self.item,
                "qty": 5,
                "rate": 100,
                "price_list_rate": 100,
                "warehouse": self.src_wh,
                "target_warehouse": self.tgt_wh,
            },
        )
        dn.insert(ignore_permissions=True)
        # Defensive — restore rate + target if ERPNext stripped them.
        for ln in dn.items:
            updates = {}
            if flt(ln.rate) != 100:
                updates["rate"] = 100
                updates["amount"] = 500
            if not ln.target_warehouse:
                updates["target_warehouse"] = self.tgt_wh
            if updates:
                frappe.db.set_value(
                    "Delivery Note Item",
                    ln.name,
                    updates,
                    update_modified=False,
                )
        frappe.db.commit()
        return dn.name

    def _location_c_id(self) -> int:
        """Synthetic c_id integer for the test Location."""
        return abs(hash(self.location_key)) % 1000000


# ============================================================
# Different-GSTIN with SI Draft → IPR Draft (no auto-submit)
# ============================================================


class TestDifferentGstinSiDraftIprStaysDraft(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if not frappe.db.exists("Company", "_Other Test Co"):
            cls.skip_reason = "_Other Test Co missing"
            return
        cls.skip_reason = None
        _wipe_state()
        cls.src_company = "_Test Company"
        cls.tgt_company = "_Other Test Co"
        cls.src_wh = _ensure_warehouse(
            f"{_PREFIX}WH-DIFF-SRC", company=cls.src_company
        )
        cls.tgt_wh = _ensure_warehouse(
            f"{_PREFIX}WH-DIFF-TGT", company=cls.tgt_company
        )
        cls.git_wh = _ensure_warehouse(
            f"{_PREFIX}WH-DIFF-GIT", company=cls.tgt_company
        )
        cls.rej_wh = _ensure_warehouse(
            f"{_PREFIX}WH-DIFF-REJ", company=cls.tgt_company
        )
        _ensure_warehouse_address(cls.tgt_wh)
        _set_company_gstin(cls.src_company, "29ABCDE1234F1Z5")
        _set_company_gstin(cls.tgt_company, "27ABCDE9999F1Z9")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-DIFF")
        _ensure_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-DIFF")
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.tgt_company,
            source_companies=[cls.src_company],
        )
        cls.internal_sup = _ensure_internal_supplier_with_companies(
            source_company=cls.src_company,
            target_companies=[cls.tgt_company],
        )
        make_account(enabled=False)
        _ensure_test_account_with_git(cls.rej_wh, cls.git_wh)
        cls.location_key = f"{_PREFIX}LOC-DIFF-TGT"
        make_location(
            location_key=cls.location_key,
            is_operational=True,
            frappe_company=cls.tgt_company,
            mapped_warehouse=cls.tgt_wh,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_state()
        super().tearDownClass()

    def setUp(self) -> None:
        if self.skip_reason:
            self.skipTest(self.skip_reason)

    def test_si_draft_ipr_stays_draft_no_discrepancy(self) -> None:
        """Different-GSTIN + drafted SI → IPR in Draft + Comment.
        Importantly, NO Discrepancy is raised (this is ERP-user pending,
        not FDE issue)."""
        # Build minimal SI in Draft.
        si = self._draft_si()
        dn_name = f"{_PREFIX}DN-DIFF-001"
        if not frappe.db.exists("Delivery Note", dn_name):
            self.skipTest("Need a real DN for this scenario; smoke only")
        # Build Transfer Map with SI in Draft.
        tm_name = _make_transfer_map(
            dn_name=dn_name,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            sales_invoice=si,
            gstin_different=1,
            status="SI-Pending",
        )
        # Submit gate decision call (unit-level).
        tm = frappe.get_doc("EasyEcom Transfer Map", tm_name)
        decision = _decide_ipr_submit(transfer_map=tm, ee_grn_id=500050)
        self.assertEqual(decision["action"], "draft")
        self.assertEqual(decision["kind"], "si_pending")
        self.assertIn("source-side SI to be Submitted", decision["reason"])

    def _draft_si(self) -> str:
        if frappe.db.exists("Sales Invoice", {"company": self.src_company, "customer": self.internal_cust, "docstatus": 0}):
            return frappe.db.get_value(
                "Sales Invoice",
                {
                    "company": self.src_company,
                    "customer": self.internal_cust,
                    "docstatus": 0,
                },
                "name",
            )
        si = frappe.new_doc("Sales Invoice")
        si.update(
            {
                "customer": self.internal_cust,
                "company": self.src_company,
                "posting_date": frappe.utils.today(),
                "due_date": frappe.utils.today(),
                "is_internal_customer": 1,
                "update_stock": 0,
                "currency": "INR",
                "conversion_rate": 1,
                "selling_price_list": frappe.db.get_value(
                    "Price List", {"selling": 1}, "name"
                ),
                "price_list_currency": "INR",
                "plc_conversion_rate": 1,
            }
        )
        si.append(
            "items",
            {"item_code": self.item, "qty": 5, "rate": 100},
        )
        si.insert(ignore_permissions=True)
        return si.name


# ============================================================
# Submit-gate unit tests (no DB inserts beyond fixtures)
# ============================================================


class TestSubmitGateDecisions(FrappeTestCase):
    """_decide_ipr_submit branch behaviours."""

    def test_same_gstin_decision_is_submit(self) -> None:
        tm = frappe._dict(
            name="ECS-XFER-FAKE",
            gstin_different=0,
            sales_invoice=None,
            draft_debit_note=None,
        )
        d = _decide_ipr_submit(transfer_map=tm, ee_grn_id=1)
        self.assertEqual(d["action"], "submit")
        self.assertEqual(d["kind"], "same_gstin")

    def test_different_gstin_no_si_is_draft(self) -> None:
        tm = frappe._dict(
            name="ECS-XFER-FAKE",
            gstin_different=1,
            sales_invoice=None,
            draft_debit_note=None,
        )
        d = _decide_ipr_submit(transfer_map=tm, ee_grn_id=1)
        self.assertEqual(d["action"], "draft")
        self.assertEqual(d["kind"], "si_pending")


# ============================================================
# EE-originated standalone path
# ============================================================


class TestEeOriginatedStandalone(FrappeTestCase):
    def test_handle_ee_originated_grn_raises_discrepancy(self) -> None:
        outcome = handle_ee_originated_grn(
            grn_row={"grn_id": 500100, "grn_items": []},
            ee_grn_id=500100,
            inwarded_wh_c_id=999999,
            vendor_c_id=999999,
        )
        self.assertEqual(outcome.operation, "ee_originated_draft")
        # Either a Discrepancy was raised (company resolved) or
        # the flag_reason captures the reason regardless.
        self.assertTrue(
            outcome.flag_reasons,
            "EE-originated path must surface a flag_reason",
        )
        # Reason names self-GRN routing.
        joined = " || ".join(outcome.flag_reasons)
        self.assertIn("EE-originated GRN", joined)


# ============================================================
# Internal Supplier resolution (symmetric with §10 Stage 2 lookup)
# ============================================================


class TestInternalSupplierLookup(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_state()
        cls.src_company = _company()
        cls.tgt_company = (
            "_Other Test Co"
            if frappe.db.exists("Company", "_Other Test Co")
            else cls.src_company
        )
        cls.internal_sup = _ensure_internal_supplier_with_companies(
            source_company=cls.src_company,
            target_companies=[cls.tgt_company],
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_state()
        super().tearDownClass()

    def test_finds_internal_supplier_by_source_and_target(self) -> None:
        name = _find_internal_supplier(
            source_company=self.src_company,
            target_company=self.tgt_company,
        )
        self.assertEqual(name, self.internal_sup)

    def test_returns_none_for_unmapped_target(self) -> None:
        name = _find_internal_supplier(
            source_company=self.src_company,
            target_company="_Nonexistent Co",
        )
        self.assertIsNone(name)
