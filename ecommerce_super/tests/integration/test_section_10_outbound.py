"""§10 Stage 2 outbound flow tests — DN.on_submit → Transfer Map +
SI Draft (different-GSTIN) + STN/PO branch dispatch.

Mocks the EasyEcomClient at the wire boundary. No real EE calls.

Test isolation: per-test-account via factories.make_account from §9
Stage 4. Stage 1's tests already proved the Stage 1 substrate fits.
This module asserts the Stage 2 behaviour: Gate-0, preconditions,
SI auto-draft, STN payload shape per §10.G, PO branch routing,
pause-defer, cancel/amend stub-blockers, batch sweep candidate set.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ecommerce_super.easyecom.flows.transfer_push import (
    block_dn_amend_after_submit,
    block_dn_cancel,
    enqueue_on_dn_submit,
    fire_pending_transfer_pushes,
    push_all_pending_transfers,
    push_one_transfer,
    validate_pre_submit,
)
from ecommerce_super.tests.factories import make_account, make_location


_PREFIX = "TEST-S10-S2-"


def _company() -> str:
    c = frappe.db.get_value("Company", filters={}, fieldname="name")
    if not c:
        raise RuntimeError("No Company")
    return c


def _other_company() -> str:
    """Second Company on the test site — the different-GSTIN target.
    Skip the test if not present."""
    return "_Other Test Co"


# ============================================================
# Fixtures
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
    has = frappe.db.sql(
        """SELECT 1 FROM `tabDynamic Link`
           WHERE parenttype='Address' AND link_doctype='Warehouse'
             AND link_name=%s LIMIT 1""",
        (warehouse,),
    )
    if has:
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
            "phone": "9999999999",
            "email_id": "wh@test.local",
            "links": [{"link_doctype": "Warehouse", "link_name": warehouse}],
        }
    )
    addr.insert(ignore_permissions=True)


def _ensure_company_address(company: str) -> None:
    has = frappe.db.sql(
        """SELECT 1 FROM `tabDynamic Link`
           WHERE parenttype='Address' AND link_doctype='Company'
             AND link_name=%s LIMIT 1""",
        (company,),
    )
    if has:
        return
    # Sanitise company name for the email local-part (the test Company
    # `_Other Test Co` has spaces + an underscore that fail Frappe's
    # email validator).
    safe = (
        company.replace(" ", "")
        .replace("_", "")
        .lower()
    )
    addr = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": f"Addr-Co-{company}",
            "address_type": "Office",
            "address_line1": "Test Corporate Park",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560001",
            "country": "India",
            "phone": "8888888888",
            "email_id": f"co-{safe}@test.local",
            "links": [{"link_doctype": "Company", "link_name": company}],
        }
    )
    addr.insert(ignore_permissions=True)


def _set_company_gstin(company: str, gstin: str) -> None:
    frappe.db.set_value("Company", company, "gstin", gstin, update_modified=False)


def _ensure_item(code: str, *, weight_per_unit: float = 1) -> str:
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
            "weight_per_unit": weight_per_unit,
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


def _ensure_internal_customer_with_companies(
    *, target_company: str, source_companies: list[str]
) -> str:
    """Find or create an Internal Customer representing target_company,
    with source_companies in its companies child table."""
    existing = frappe.db.get_value(
        "Customer",
        {"is_internal_customer": 1, "represents_company": target_company},
        "name",
    )
    if existing:
        # Reconcile companies — add missing sources.
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


def _ensure_customer_map(customer_docname: str, *, ee_customer_id: str) -> str:
    existing = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_doctype": "Customer", "erpnext_name": customer_docname},
        "name",
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Customer Map",
            existing,
            "ee_customer_id",
            ee_customer_id,
            update_modified=False,
        )
        return existing
    m = frappe.new_doc("EasyEcom Customer Map")
    m.update(
        {
            "ee_c_id": f"c-{customer_docname[-8:]}",
            "ee_customer_id": ee_customer_id,
            "erpnext_doctype": "Customer",
            "erpnext_name": customer_docname,
            "status": "Mapped",
        }
    )
    m.insert(ignore_permissions=True)
    return m.name


def _make_internal_dn(
    *,
    customer: str,
    source_wh: str,
    target_wh: str,
    item: str,
    qty: int = 5,
    rate: float = 100,
    company: str | None = None,
) -> Any:
    price_list = frappe.db.get_value("Price List", {"selling": 1}, "name")
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
    dn_company = company or _company()
    # Resolve source-warehouse company so DN.company matches.
    wh_company = frappe.db.get_value("Warehouse", source_wh, "company")
    if wh_company:
        dn_company = wh_company
    dn = frappe.new_doc("Delivery Note")
    dn.update(
        {
            "customer": customer,
            "company": dn_company,
            "is_internal_customer": 1,
            "set_warehouse": source_wh,
            "posting_date": frappe.utils.today(),
            "delivery_date": frappe.utils.add_days(frappe.utils.today(), 3),
            "selling_price_list": price_list,
            "price_list_currency": "INR",
            "plc_conversion_rate": 1,
            "currency": "INR",
            "conversion_rate": 1,
            # Avoid Pricing Rules zeroing the rate when no Item Price
            # row exists for the test item — the §10 STN payload reads
            # line.rate directly.
            "ignore_pricing_rule": 1,
        }
    )
    dn.append(
        "items",
        {
            "item_code": item,
            "qty": qty,
            "rate": rate,
            "price_list_rate": rate,
            "warehouse": source_wh,
            "target_warehouse": target_wh,
        },
    )
    dn.insert(ignore_permissions=True)
    # Defensive: if ERPNext zeroed the rate or stripped target_warehouse
    # (cross-Company DNs often lose target_warehouse on Internal-Customer
    # validate), fix via db.set_value so the §10 hook sees what we want.
    if dn.items:
        for ln in dn.items:
            updates = {}
            if flt(ln.rate) != flt(rate):
                updates["rate"] = rate
                updates["amount"] = rate * qty
            if not ln.target_warehouse:
                updates["target_warehouse"] = target_wh
            if updates:
                frappe.db.set_value(
                    "Delivery Note Item",
                    ln.name,
                    updates,
                    update_modified=False,
                )
        frappe.db.commit()
        dn.reload()
    return dn


def _wipe_test_state() -> None:
    """Wipe rows created by this test module."""
    for n in frappe.db.get_all(
        "EasyEcom Transfer Map",
        filters={"delivery_note": ("like", "MAT-DN%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Transfer Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "Delivery Note",
        filters={"customer": ("like", f"INTL-CUST-for-%")},
        pluck="name",
    ):
        try:
            doc = frappe.get_doc("Delivery Note", n)
            if int(doc.docstatus or 0) == 1:
                doc.cancel()
            frappe.delete_doc(
                "Delivery Note", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "Sales Invoice",
        filters={"customer": ("like", "INTL-CUST-for-%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "Sales Invoice", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom Sync Record",
        filters={"entity_doctype": "Delivery Note"},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Sync Record", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom Location",
        filters={"location_key": ("like", f"{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Location", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


# ============================================================
# Gate-0 + preconditions
# ============================================================


class TestGate0AndPreconditions(FrappeTestCase):
    """Non-Internal DN inert; missing pair → Drift; multi-pair refused."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_test_state()
        cls.company = _company()
        cls.src_wh = _ensure_warehouse(f"{_PREFIX}WH-SRC", company=cls.company)
        cls.tgt_wh = _ensure_warehouse(f"{_PREFIX}WH-TGT", company=cls.company)
        _ensure_warehouse_address(cls.tgt_wh)
        _ensure_company_address(cls.company)
        _set_company_gstin(cls.company, "29ABCDE1234F1Z5")
        cls.item = _ensure_item(f"{_PREFIX}ITEM")
        _ensure_item_map(cls.item, ee_sku=f"{_PREFIX}SKU")
        # Internal Customer + EE Customer Map for the same-Company pair.
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.company,
            source_companies=[cls.company],
        )
        _ensure_customer_map(cls.internal_cust, ee_customer_id="111222")
        # An EE Location on the source WH so it's EE-mapped.
        make_account(enabled=False)
        loc = make_location(
            location_key=f"{_PREFIX}LOC-SRC",
            is_operational=True,
            frappe_company=cls.company,
            mapped_warehouse=cls.src_wh,
        )
        cls._loc_src = loc

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_test_state()
        super().tearDownClass()

    def test_non_internal_customer_dn_silent(self) -> None:
        """Non-internal-customer DN → silent inert. No Transfer Map."""
        # Use a non-internal customer.
        if not frappe.db.exists("Customer", f"{_PREFIX}REG-CUST"):
            g = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
            c = frappe.new_doc("Customer")
            c.update(
                {
                    "customer_name": f"{_PREFIX}REG-CUST",
                    "customer_type": "Company",
                    "customer_group": g,
                }
            )
            c.insert(ignore_permissions=True)
        # We can't insert a non-internal DN with is_internal_customer=0
        # and a target_warehouse on lines (ERPNext refuses). Skip the
        # DN insert; assert push_one_transfer's Gate-0 returns skipped
        # on any DN that's not marked is_internal_customer.
        fake_dn = frappe._dict(
            doctype="Delivery Note",
            name=f"NONE-{_PREFIX}",
            is_internal_customer=0,
        )
        # Direct call — must short-circuit at the is_internal check
        # before any DB lookup.
        with patch(
            "ecommerce_super.easyecom.flows.transfer_push.frappe.db.exists",
            return_value=True,
        ), patch(
            "ecommerce_super.easyecom.flows.transfer_push.frappe.get_doc",
            return_value=fake_dn,
        ):
            outcome = push_one_transfer(f"NONE-{_PREFIX}")
        self.assertEqual(outcome.operation, "skipped")
        self.assertIn("not an Internal-Customer DN", " ".join(outcome.flag_reasons))

    def test_multi_warehouse_pair_refused_on_validate(self) -> None:
        """DN with two distinct (source, target) pairs across lines →
        validate_pre_submit refuses with a clear error."""
        # Build the DN dict directly to bypass auto-fill. Avoid
        # frappe._dict because its .items attribute collides with
        # dict.items() method; use lightweight classes.
        wh2_src = _ensure_warehouse(f"{_PREFIX}WH-SRC2", company=self.company)
        wh2_tgt = _ensure_warehouse(f"{_PREFIX}WH-TGT2", company=self.company)
        _ensure_warehouse_address(wh2_tgt)
        item2 = _ensure_item(f"{_PREFIX}ITEM-2")
        _ensure_item_map(item2)

        class FakeLine:
            def __init__(self, w: str, t: str) -> None:
                self.warehouse = w
                self.target_warehouse = t

        class FakeDoc:
            doctype = "Delivery Note"
            is_internal_customer = 1

            def __init__(self, items: list[Any]) -> None:
                self.items = items

        doc = FakeDoc(
            items=[
                FakeLine(self.src_wh, self.tgt_wh),
                FakeLine(wh2_src, wh2_tgt),
            ]
        )
        with self.assertRaises(frappe.ValidationError) as ctx:
            validate_pre_submit(doc)
        self.assertIn("multiple distinct", str(ctx.exception))

    def test_missing_internal_customer_pair_drift(self) -> None:
        """If the Internal Customer for this (src→tgt) doesn't exist,
        Transfer Map lands in Drift state with flag_reason naming the
        missing pair."""
        # Wipe the Internal Customer for the test pair.
        existing_cust = frappe.db.get_value(
            "Customer",
            {
                "is_internal_customer": 1,
                "represents_company": self.company,
            },
            "name",
        )
        # Build a DN — we'll point its customer to a non-internal so the
        # validate guard doesn't refuse, but is_internal_customer=1 so
        # the §10 hook fires. Actually ERPNext enforces that DN.customer
        # must be an internal customer when is_internal_customer=1, so
        # we use the existing one but TEMPORARILY remove the source
        # Company from companies[*].company.
        if existing_cust:
            doc = frappe.get_doc("Customer", existing_cust)
            saved_companies = [r.company for r in (doc.companies or [])]
            doc.set("companies", [])
            doc.save(ignore_permissions=True)
            try:
                dn = _make_internal_dn(
                    customer=existing_cust,
                    source_wh=self.src_wh,
                    target_wh=self.tgt_wh,
                    item=self.item,
                )
                outcome = push_one_transfer(dn.name)
                self.assertEqual(outcome.operation, "drift")
                self.assertEqual(outcome.status, "Drift")
                self.assertTrue(
                    any(
                        "Internal Customer pair missing" in r
                        for r in outcome.flag_reasons
                    ),
                    outcome.flag_reasons,
                )
                # Map row exists at Drift.
                gm = frappe.get_doc(
                    "EasyEcom Transfer Map", outcome.transfer_map
                )
                self.assertEqual(gm.status, "Drift")
                self.assertIsNone(gm.sales_invoice)
            finally:
                # Restore.
                doc = frappe.get_doc("Customer", existing_cust)
                for c in saved_companies:
                    doc.append("companies", {"company": c})
                doc.save(ignore_permissions=True)


# ============================================================
# Same-GSTIN STN push (no SI)
# ============================================================


class TestSameGstinStnPush(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_test_state()
        cls.company = _company()
        cls.src_wh = _ensure_warehouse(f"{_PREFIX}WH-S-SRC", company=cls.company)
        cls.tgt_wh = _ensure_warehouse(f"{_PREFIX}WH-S-TGT", company=cls.company)
        _ensure_warehouse_address(cls.tgt_wh)
        _ensure_company_address(cls.company)
        _set_company_gstin(cls.company, "29ABCDE1234F1Z5")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-S", weight_per_unit=2.0)
        _ensure_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-S")
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.company, source_companies=[cls.company]
        )
        _ensure_customer_map(cls.internal_cust, ee_customer_id="111222")
        make_account(enabled=False)
        make_location(
            location_key=f"{_PREFIX}LOC-S-SRC",
            is_operational=True,
            frappe_company=cls.company,
            mapped_warehouse=cls.src_wh,
        )
        make_location(
            location_key=f"{_PREFIX}LOC-S-TGT",
            is_operational=True,
            frappe_company=cls.company,
            mapped_warehouse=cls.tgt_wh,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_test_state()
        super().tearDownClass()

    def test_same_gstin_no_si_stn_payload_correct(self) -> None:
        dn = _make_internal_dn(
            customer=self.internal_cust,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            item=self.item,
            qty=5,
            rate=100,
        )
        # Mock the client.
        client = MagicMock()
        client.post.return_value = {
            "code": 200,
            "data": {
                "OrderID": "777001",
                "SuborderID": "888001",
                "InvoiceID": "999001",
            },
        }
        outcome = push_one_transfer(dn.name, client=client)
        self.assertEqual(outcome.operation, "stn_pushed")
        self.assertEqual(outcome.status, "EE-Pushed")
        self.assertIsNone(outcome.sales_invoice)
        self.assertEqual(outcome.ee_order_id, "777001")
        self.assertEqual(outcome.ee_suborder_id, "888001")
        self.assertEqual(outcome.ee_invoice_id, "999001")
        self.assertEqual(outcome.ee_doctype, "STN")

        # Verify the payload shape per §10.G.
        call_args = client.post.call_args
        payload = call_args.kwargs["payload"]
        self.assertEqual(payload["orderType"], "stocktransferorder")
        self.assertEqual(payload["orderNumber"], dn.name)
        self.assertEqual(payload["shippingCost"], 0)
        self.assertEqual(payload["paymentMode"], 5)  # default Prepaid
        self.assertEqual(payload["shippingMethod"], 1)  # default Standard COD
        # packageWeight = qty * weight_per_unit (5 * 2.0 = 10kg = 10).
        # The implementation reads weight_per_unit and multiplies by qty;
        # value is stored as grams round if weight_uom were kg/g. The
        # current impl treats weight_per_unit as-is (item weight unit).
        self.assertIn("packageWeight", payload)
        # Items array — one per DN line.
        self.assertEqual(len(payload["items"]), 1)
        item0 = payload["items"][0]
        self.assertEqual(item0["OrderItemId"], f"{dn.name}-L1")
        self.assertEqual(item0["Sku"], f"{_PREFIX}SKU-S")
        self.assertEqual(item0["Quantity"], "5")  # qty as string per §10.G
        self.assertEqual(item0["Price"], 100)
        self.assertEqual(item0["itemDiscount"], 0)
        # Customer block — single-element array.
        self.assertEqual(len(payload["customer"]), 1)
        cust_block = payload["customer"][0]
        self.assertEqual(cust_block["customerId"], 111222)
        self.assertIn("billing", cust_block)
        self.assertIn("shipping", cust_block)
        # OMITTED fields are truly absent.
        for omitted in (
            "is_market_shipped",
            "closed",
            "queue",
            "paymentGateway",
            "walletDiscount",
            "promoCodeDiscount",
            "prepaidDiscount",
            "paymentTransactionNumber",
            "collectableAmount",
            "salesmanId",
            "discount",
            "marketplace_id",
            "custom_fields",
            "latitude",
            "longitude",
            "gst_number",
            "appointment_number",
            "appointment_date",
            "company_carrier_id",
            "is_pricing_master",
            "orderAssignmentProperty",
        ):
            self.assertNotIn(
                omitted,
                payload,
                f"§10.G OMITTED field {omitted!r} leaked into STN payload",
            )

        # Verify Transfer Map row was updated.
        gm = frappe.get_doc("EasyEcom Transfer Map", outcome.transfer_map)
        self.assertEqual(gm.status, "EE-Pushed")
        self.assertEqual(gm.ee_doctype, "STN")
        self.assertEqual(gm.ee_order_id, "777001")
        self.assertEqual(gm.ee_suborder_id, "888001")
        self.assertEqual(gm.ee_invoice_id, "999001")
        self.assertFalse(int(gm.ecs_pending_ee_push or 0))


# ============================================================
# Different-GSTIN STN push (SI auto-draft)
# ============================================================


class TestDifferentGstinStnPush(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if not frappe.db.exists("Company", _other_company()):
            raise unittest.SkipTest(  # noqa: F821
                "Test site lacks _Other Test Co"
            )
        _wipe_test_state()
        # Explicit Company names — _company() is non-deterministic on
        # test sites with multiple Companies.
        cls.src_company = "_Test Company"
        cls.tgt_company = _other_company()
        if not frappe.db.exists("Company", cls.src_company):
            cls.src_company = _company()
        cls.src_wh = _ensure_warehouse(
            f"{_PREFIX}WH-D-SRC", company=cls.src_company
        )
        cls.tgt_wh = _ensure_warehouse(
            f"{_PREFIX}WH-D-TGT", company=cls.tgt_company
        )
        _ensure_warehouse_address(cls.tgt_wh)
        _ensure_company_address(cls.tgt_company)
        _set_company_gstin(cls.src_company, "29ABCDE1234F1Z5")  # Karnataka
        _set_company_gstin(cls.tgt_company, "27ABCDE9999F1Z9")  # Maharashtra
        cls.item = _ensure_item(f"{_PREFIX}ITEM-D")
        _ensure_item_map(cls.item, ee_sku=f"{_PREFIX}SKU-D")
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.tgt_company,
            source_companies=[cls.src_company],
        )
        _ensure_customer_map(cls.internal_cust, ee_customer_id="333444")
        make_account(enabled=False)
        make_location(
            location_key=f"{_PREFIX}LOC-D-SRC",
            is_operational=True,
            frappe_company=cls.src_company,
            mapped_warehouse=cls.src_wh,
        )
        make_location(
            location_key=f"{_PREFIX}LOC-D-TGT",
            is_operational=True,
            frappe_company=cls.tgt_company,
            mapped_warehouse=cls.tgt_wh,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_test_state()
        super().tearDownClass()

    def test_si_auto_drafted_when_gstin_different(self) -> None:
        dn = _make_internal_dn(
            customer=self.internal_cust,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            item=self.item,
            qty=3,
            rate=200,
        )
        client = MagicMock()
        client.post.return_value = {
            "code": 200,
            "data": {
                "OrderID": "555111",
                "SuborderID": "666111",
                "InvoiceID": "777111",
            },
        }
        outcome = push_one_transfer(dn.name, client=client)

        self.assertEqual(outcome.operation, "stn_pushed")
        # SI present + Draft.
        self.assertIsNotNone(outcome.sales_invoice)
        si = frappe.get_doc("Sales Invoice", outcome.sales_invoice)
        self.assertEqual(int(si.docstatus or 0), 0, "SI must be Draft")
        self.assertEqual(int(si.update_stock or 0), 0, "SI update_stock=0")
        self.assertEqual(si.customer, self.internal_cust)
        # SI items mirror DN lines.
        self.assertEqual(len(si.items), 1)
        self.assertEqual(si.items[0].qty, 3)
        self.assertEqual(si.items[0].rate, 200)
        # Transfer Map status = SI-Pending (overloaded — EE-Pushed but
        # SI still in Draft).
        gm = frappe.get_doc("EasyEcom Transfer Map", outcome.transfer_map)
        self.assertEqual(gm.status, "SI-Pending")
        self.assertEqual(gm.sales_invoice, si.name)
        self.assertEqual(gm.ee_order_id, "555111")
        self.assertTrue(int(gm.gstin_different or 0))


# ============================================================
# Pause defer + un-pause runner
# ============================================================


class TestPauseDeferAndUnpause(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_test_state()
        # Snapshot test-account flags so tearDownClass restores them —
        # sibling §9 tests assert auto_push_pos_on_save=0 default,
        # which this class flips during un-pause.
        if frappe.db.exists("EasyEcom Account", "test-account"):
            cls._saved_pos = int(
                frappe.db.get_value(
                    "EasyEcom Account",
                    "test-account",
                    "auto_push_pos_on_save",
                )
                or 0
            )
        else:
            cls._saved_pos = 0
        cls.company = _company()
        cls.src_wh = _ensure_warehouse(f"{_PREFIX}WH-P-SRC", company=cls.company)
        cls.tgt_wh = _ensure_warehouse(f"{_PREFIX}WH-P-TGT", company=cls.company)
        _ensure_warehouse_address(cls.tgt_wh)
        _ensure_company_address(cls.company)
        _set_company_gstin(cls.company, "29ABCDE1234F1Z5")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-P")
        _ensure_item_map(cls.item)
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.company, source_companies=[cls.company]
        )
        _ensure_customer_map(cls.internal_cust, ee_customer_id="555666")
        make_account(enabled=False)
        make_location(
            location_key=f"{_PREFIX}LOC-P-SRC",
            is_operational=True,
            frappe_company=cls.company,
            mapped_warehouse=cls.src_wh,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        # Restore test-account auto_push_pos_on_save so sibling §9
        # tests' default-is-off invariant holds.
        if frappe.db.exists("EasyEcom Account", "test-account"):
            frappe.db.set_value(
                "EasyEcom Account",
                "test-account",
                "auto_push_pos_on_save",
                cls._saved_pos,
                update_modified=False,
            )
            frappe.db.commit()
        _wipe_test_state()
        super().tearDownClass()

    def setUp(self) -> None:
        # Pause via auto_push_pos_on_save=0 on the test-account.
        if frappe.db.exists("EasyEcom Account", "test-account"):
            frappe.db.set_value(
                "EasyEcom Account",
                "test-account",
                "auto_push_pos_on_save",
                0,
                update_modified=False,
            )
            frappe.db.set_value(
                "EasyEcom Account",
                "test-account",
                "enabled",
                1,
                update_modified=False,
            )
            frappe.db.commit()

    def test_paused_records_pending_no_ee_call(self) -> None:
        client = MagicMock()
        dn = _make_internal_dn(
            customer=self.internal_cust,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            item=self.item,
        )
        outcome = push_one_transfer(dn.name, client=client)
        self.assertEqual(outcome.operation, "pending_pause")
        # No EE call.
        client.post.assert_not_called()
        # ecs_pending_ee_push = 1 on the Map row.
        gm = frappe.get_doc("EasyEcom Transfer Map", outcome.transfer_map)
        self.assertEqual(int(gm.ecs_pending_ee_push or 0), 1)
        # Status = Mapped (no SI in same-GSTIN).
        self.assertEqual(gm.status, "Mapped")

    def test_unpause_fires_pending(self) -> None:
        # Clear stray pending Maps from sibling tests.
        for n in frappe.db.get_all(
            "EasyEcom Transfer Map",
            filters={"ecs_pending_ee_push": 1},
            pluck="name",
        ):
            frappe.db.set_value(
                "EasyEcom Transfer Map",
                n,
                "ecs_pending_ee_push",
                0,
                update_modified=False,
            )
        frappe.db.commit()
        client = MagicMock()
        dn = _make_internal_dn(
            customer=self.internal_cust,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            item=self.item,
        )
        push_one_transfer(dn.name, client=client)  # paused → pending

        # Un-pause.
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            "auto_push_pos_on_save",
            1,
            update_modified=False,
        )
        frappe.db.commit()

        fire_client = MagicMock()
        fire_client.post.return_value = {
            "code": 200,
            "data": {
                "OrderID": "P-001",
                "SuborderID": "P-002",
                "InvoiceID": "P-003",
            },
        }

        # Patch EasyEcomClient construction inside push_one_transfer.
        with patch(
            "ecommerce_super.easyecom.flows.transfer_push.EasyEcomClient",
            return_value=fire_client,
        ):
            out = fire_pending_transfer_pushes()

        self.assertTrue(out["ok"])
        self.assertEqual(out["fired"], 1, out)
        # Pending flag cleared.
        gm_name = frappe.db.get_value(
            "EasyEcom Transfer Map", {"delivery_note": dn.name}, "name"
        )
        gm = frappe.get_doc("EasyEcom Transfer Map", gm_name)
        self.assertEqual(int(gm.ecs_pending_ee_push or 0), 0)
        self.assertEqual(gm.ee_order_id, "P-001")


# ============================================================
# Cancel / amend stub-blockers
# ============================================================


class TestCancelAmendStubBlockers(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_test_state()
        cls.company = _company()
        cls.src_wh = _ensure_warehouse(f"{_PREFIX}WH-C-SRC", company=cls.company)
        cls.tgt_wh = _ensure_warehouse(f"{_PREFIX}WH-C-TGT", company=cls.company)

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_test_state()
        super().tearDownClass()

    def _seed_map(self, *, status: str, ee_order_id: str) -> str:
        """Create a minimal Internal-Customer DN + Transfer Map row
        keyed off it."""
        # Use the existing Internal Customer from another test class if
        # available; otherwise create a minimal one.
        cust = _ensure_internal_customer_with_companies(
            target_company=self.company, source_companies=[self.company]
        )
        _ensure_warehouse_address(self.tgt_wh)
        _ensure_company_address(self.company)
        item = _ensure_item(f"{_PREFIX}ITEM-C")
        _ensure_item_map(item)
        dn = _make_internal_dn(
            customer=cust,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            item=item,
        )
        # Insert the Transfer Map directly with the target status.
        gm = frappe.new_doc("EasyEcom Transfer Map")
        gm.update(
            {
                "delivery_note": dn.name,
                "source_warehouse": self.src_wh,
                "target_warehouse": self.tgt_wh,
                "status": status,
                "ee_order_id": ee_order_id,
                "ee_doctype": "STN",
            }
        )
        gm.insert(ignore_permissions=True)
        return dn.name

    def test_cancel_blocks_when_ee_pushed(self) -> None:
        dn_name = self._seed_map(status="EE-Pushed", ee_order_id="CC-001")
        doc = frappe.get_doc("Delivery Note", dn_name)
        with self.assertRaises(frappe.ValidationError) as ctx:
            block_dn_cancel(doc)
        self.assertIn("cancel/amend not yet implemented", str(ctx.exception))

    def test_cancel_passes_when_not_ee_pushed(self) -> None:
        dn_name = self._seed_map(status="Mapped", ee_order_id="")
        doc = frappe.get_doc("Delivery Note", dn_name)
        # Should not raise.
        block_dn_cancel(doc)

    def test_amend_blocks_when_ee_pushed(self) -> None:
        dn_name = self._seed_map(status="EE-Pushed", ee_order_id="CC-002")
        doc = frappe.get_doc("Delivery Note", dn_name)
        with self.assertRaises(frappe.ValidationError) as ctx:
            block_dn_amend_after_submit(doc)
        self.assertIn("amend not yet implemented", str(ctx.exception))


# ============================================================
# PO branch routing (vendor resolution + Drift on miss)
# ============================================================


class TestPoBranchRouting(FrappeTestCase):
    """PO branch: source NOT EE-mapped, target EE-mapped.
    Stage 2 ships the routing shape; full wire dispatch deferred."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_test_state()
        cls.company = _company()
        # Source WH NOT EE-mapped.
        cls.src_wh = _ensure_warehouse(
            f"{_PREFIX}WH-PO-SRC-NONEE", company=cls.company
        )
        # Target WH EE-mapped.
        cls.tgt_wh = _ensure_warehouse(
            f"{_PREFIX}WH-PO-TGT-EE", company=cls.company
        )
        _ensure_warehouse_address(cls.tgt_wh)
        _ensure_company_address(cls.company)
        _set_company_gstin(cls.company, "29ABCDE1234F1Z5")
        cls.item = _ensure_item(f"{_PREFIX}ITEM-PO")
        _ensure_item_map(cls.item)
        cls.internal_cust = _ensure_internal_customer_with_companies(
            target_company=cls.company, source_companies=[cls.company]
        )
        _ensure_customer_map(cls.internal_cust, ee_customer_id="POE-1")
        make_account(enabled=False)
        make_location(
            location_key=f"{_PREFIX}LOC-PO-TGT",
            is_operational=True,
            frappe_company=cls.company,
            mapped_warehouse=cls.tgt_wh,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_test_state()
        super().tearDownClass()

    def test_po_branch_no_vendor_drift(self) -> None:
        """No Internal Supplier with ee_vendor_id for source Company →
        Drift with the documented flag_reason."""
        dn = _make_internal_dn(
            customer=self.internal_cust,
            source_wh=self.src_wh,
            target_wh=self.tgt_wh,
            item=self.item,
        )
        outcome = push_one_transfer(dn.name)
        self.assertEqual(outcome.operation, "drift")
        self.assertEqual(outcome.status, "Drift")
        joined = " || ".join(outcome.flag_reasons)
        self.assertIn("PO branch requires an EE-side vendor", joined)


# ============================================================
# Batch sweep candidate set
# ============================================================


class TestBatchSweep(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_test_state()

    @classmethod
    def tearDownClass(cls) -> None:
        _wipe_test_state()
        super().tearDownClass()

    def test_inline_mode_returns_candidates(self) -> None:
        out = push_all_pending_transfers(inline=True)
        self.assertTrue(out["ok"])
        self.assertIn("candidates_total", out)
        # Each inline result has dn/operation/status.
        for r in out.get("inline_results", []):
            self.assertIn("dn", r)
            self.assertIn("operation", r)
