"""§8d Stage 2 tests — EE → ERPNext Product Master pull.

Mandatory tests per the build packet:
  - Ruleset reconciliation against the real captured fixtures.
  - Cursor walk + resume-from-persisted-cursor.
  - Savepoint isolation (one bad product, page continues).
  - Count-aware paging (GetProductMastersCount → Account state).
  - Matching: map-row, exact-sku auto-map, create-new.
  - product_type branching (FNC for variant/child/kit/unknown; flag for combo).
  - Missing-HSN held (Flagged-Not-Created, India Compliance gate).
  - Dirty UOM Created-Flagged with substituted default.
  - Multi-Company tax append + idempotent (the §8d Stage-2 patch
    to the 8c resolver — single-Co tests remain green elsewhere).
  - active:0 → item.disabled=1.

All tests HTTP-mocked. No real EE traffic. The mock client serves
canned pages from `tests/ee_mock/`; production paths are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_COUNT_GET,
    PRODUCT_MASTER_GET,
)
from ecommerce_super.easyecom.doctype.easyecom_tax_rule_map.easyecom_tax_rule_map import (
    resolve_and_stamp_tax,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.item_pull import (
    ITEM_PULL_RULESET,
    STATUS_CREATED_FLAGGED,
    STATUS_FLAGGED_NOT_CREATED,
    STATUS_MAPPED,
    process_one_product,
    pull_products,
)
from ecommerce_super.tests.factories import make_account
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


PREFIX = "TEST-8D-S2-"


# ----- Fixtures + helpers -----


def _fixture(name: str) -> dict:
    base = Path(frappe.get_app_path("ecommerce_super", "tests", "ee_mock"))
    return json.loads((base / name).read_text())


def _wipe(prefix: str = PREFIX) -> None:
    """Drop all Stage-2 test artefacts. Order matters: Item Map FK→Item,
    Tax Rule Map and Item Tax Template independent. Items in the
    fixture have non-prefix codes (real captured SKUs); we wipe by
    direct SKU match too."""
    for dt, filt in [
        ("EasyEcom Item Map", {"ee_sku": ("like", f"{prefix}%")}),
    ]:
        for n in frappe.db.get_all(dt, filters=filt, pluck="name"):
            try:
                frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
            except Exception:
                pass
    # Test-prefixed Items created by the upsert path.
    for n in frappe.db.get_all(
        "Item", filters={"item_code": ("like", f"{prefix}%")}, pluck="name"
    ):
        for map_n in frappe.db.get_all(
            "EasyEcom Item Map",
            filters={"erpnext_name": n},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Item Map", map_n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        try:
            frappe.delete_doc("Item", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    # Real captured-fixture SKUs that tests upsert into the DB.
    captured_skus = ("mob000", "shirt000", "shirt111", "8906133380779")
    for sku in captured_skus:
        for n in frappe.db.get_all(
            "EasyEcom Item Map", filters={"ee_sku": sku}, pluck="name"
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Item Map", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        if frappe.db.exists("Item", sku):
            try:
                frappe.delete_doc(
                    "Item", sku, force=True, ignore_permissions=True
                )
            except Exception:
                pass
    frappe.db.commit()


def _ensure_hsn(code: str = "99999999") -> str:
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


def _ensure_account_for_pull() -> str:
    """Account with default_uom set + a known HSN in the library so
    the gating tests have a clean baseline to flex against."""
    name = make_account(name=f"{PREFIX}acct".lower())
    _ensure_uom("Nos")
    _ensure_item_group()
    frappe.db.set_value(
        "EasyEcom Account", name, {"default_uom": "Nos"}, update_modified=False
    )
    frappe.db.commit()
    return name


def _ensure_company_settings(company: str, enabled: int = 1) -> str:
    """Create or update the EasyEcom Company Settings row for `company`."""
    existing = frappe.db.get_value(
        "EasyEcom Company Settings", {"company": company}, "name"
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Company Settings",
            existing,
            {"enabled": enabled},
            update_modified=False,
        )
        return existing
    doc = frappe.new_doc("EasyEcom Company Settings")
    doc.update({"company": company, "enabled": enabled})
    doc.insert(ignore_permissions=True)
    return doc.name


class MockClient:
    """A swap-in for EasyEcomClient used by pull_products. Serves
    pre-canned page responses for PRODUCT_MASTER_GET / _COUNT_GET and
    follows nextUrl via the same _request entrypoint the production
    client uses for absolute-URL continuations."""

    def __init__(
        self,
        *,
        pages: list[dict] | None = None,
        count: int | None = None,
        raise_on_page: int | None = None,
    ) -> None:
        # `pages` are returned in order each time the iterator asks for
        # one. We pop from the front so a re-call re-uses the next page.
        self._pages = list(pages or [])
        self._count = count
        self._raise_on_page = raise_on_page
        self._pages_served = 0
        self.calls: list[tuple[str, str, dict | None]] = []  # (method, endpoint, params)
        # Capture the _is_absolute_url flag the flow passes per cursor
        # follow call. Lets tests assert relative-cursor handling
        # (the production bug fix: EE returns nextUrl as a relative
        # path, so the flow MUST pass _is_absolute_url=False or
        # requests.MissingSchema fires).
        self.absolute_flags: list[bool] = []

    def get(self, endpoint: str, params: dict | None = None, **_kwargs) -> dict:
        self.calls.append(("GET", endpoint, params))
        if endpoint == PRODUCT_MASTER_COUNT_GET:
            return {"count": self._count} if self._count is not None else {}
        if endpoint == PRODUCT_MASTER_GET:
            return self._serve_next_page()
        raise NotImplementedError(f"MockClient.get: unexpected endpoint {endpoint!r}")

    def _request(
        self, method: str, endpoint: str, *, params=None, payload=None, **kwargs
    ) -> dict:
        self.absolute_flags.append(bool(kwargs.get("_is_absolute_url")))
        """Absolute-URL continuation. Behave like get() — pop next page."""
        self.calls.append((method, endpoint, params))
        return self._serve_next_page()

    def _serve_next_page(self) -> dict:
        if self._raise_on_page is not None and self._pages_served == self._raise_on_page:
            self._raise_on_page = None  # only raise once
            raise RuntimeError("simulated EE outage on this page")
        if not self._pages:
            return {"data": [], "nextUrl": None}
        page = self._pages.pop(0)
        self._pages_served += 1
        return page


# ============================================================
# 1. Ruleset reconciliation — the real captured payloads
# ============================================================


class TestRulesetReconciliation(FrappeTestCase):
    """The stale Bidirectional EasyEcom-Item-Sync ruleset was rebuilt
    as EasyEcom-Item-Pull. These tests are the contract that prove
    the new ruleset translates the real captured payloads correctly."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.fix = _fixture("getproductmaster_response.json")
        cls.fix2 = _fixture("getproductmaster_child_product.json")

    def test_mob000_normal_product(self) -> None:
        out = self.executor.pull(self.fix["data"][0])
        self.assertEqual(out["item_code"], "mob000")
        self.assertEqual(out["item_name"], "Galaxyy")
        self.assertEqual(out["gst_hsn_code"], "8517")
        # Dirty UOM passes through; the FLOW gates substitution.
        self.assertEqual(out["stock_uom"], "111")
        self.assertEqual(out["disabled"], 0)  # active=1 → disabled=0
        self.assertEqual(out["weight_per_unit"], 100.0)
        self.assertEqual(out["ecs_height_cm"], 11.0)
        # Real captured length is 2107 cm (sic — EE field is real-dirty)
        self.assertEqual(out["ecs_length_cm"], 2107.0)
        self.assertEqual(out["ecs_ee_cost"], 123.0)
        self.assertEqual(out["standard_rate"], 123.0)
        self.assertEqual(out["ecs_ee_mrp"], 123.0)
        self.assertEqual(out["ecs_size"], "4GB")
        self.assertEqual(out["ecs_colour"], "Black")
        self.assertEqual(out["ecs_ee_product_id"], "17074183")
        self.assertEqual(out["ecs_ee_cp_id"], "59044224")

    def test_top3_variant_parent_translates_without_raising(self) -> None:
        """Stale ruleset's validate_against raised on the fake HSN '1324',
        blocking the flow from reading item_code. The new ruleset
        defers validation to the flow."""
        out = self.executor.pull(self.fix["data"][1])
        self.assertEqual(out["item_code"], "shirt000")
        self.assertEqual(out["gst_hsn_code"], "1324")  # not in HSN library; flow will FNC

    def test_nike_combo_product_translates(self) -> None:
        out = self.executor.pull(self.fix["data"][2])
        self.assertEqual(out["item_code"], "shirt111")
        self.assertEqual(out["item_name"], "Nike")

    def test_skinq_child_product_translates(self) -> None:
        out = self.executor.pull(self.fix2["data"][0])
        self.assertEqual(out["item_code"], "8906133380779")
        self.assertEqual(out["gst_hsn_code"], "33049910")
        # Cleaner UOM string but still not a UOM doc — flow substitutes.
        self.assertEqual(out["stock_uom"], "PCS")


# ============================================================
# 2. product_type branching
# ============================================================


class TestProductTypeBranching(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account_name = _ensure_account_for_pull()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.account = frappe.get_doc("EasyEcom Account", cls.account_name)
        cls.companies: list[str] = []

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def _process(self, payload: dict) -> Any:
        return process_one_product(
            payload,
            account=self.account,
            executor=self.executor,
            enabled_companies=self.companies,
        )

    def test_combo_product_with_no_subproducts_flagged(self) -> None:
        """Stage 4 now actively builds bundles from combos. A combo
        with no sub_products (or fewer than 2) still flags — EE
        requires ≥2 sub-products to be a valid combo. The map row
        captures the EE identifiers so a Stage-4 reconciliation can
        find the SKU later."""
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        payload = {
            "sku": f"{PREFIX}combo-1",
            "product_type": "combo_product",
            "product_name": "no-subs-combo",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
            "product_id": 1, "cp_id": 2,
            "sub_products": [],
        }
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        joined = " ".join(out.flag_reasons)
        self.assertIn("sub-products", joined)
        # Map row exists; no Bundle was created (degenerate combo).
        self.assertTrue(
            frappe.db.exists("EasyEcom Item Map", {"ee_sku": payload["sku"]})
        )
        self.assertFalse(
            frappe.db.exists("Product Bundle", {"new_item_code": payload["sku"]})
        )

    def test_variant_parent_flagged_not_created(self) -> None:
        payload = {"sku": f"{PREFIX}var-1", "product_type": "variant_parent"}
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        self.assertIn("variant_parent", out.flag_reasons[0])

    def test_child_product_flagged_not_created(self) -> None:
        payload = {"sku": f"{PREFIX}child-1", "product_type": "child_product"}
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)

    def test_kit_bom_flagged_not_created(self) -> None:
        payload = {"sku": f"{PREFIX}kit-1", "product_type": "kit_bom"}
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)

    def test_unknown_future_type_flagged_not_created(self) -> None:
        """A NEW EE type we haven't seen yet falls through to FNC."""
        payload = {"sku": f"{PREFIX}unk-1", "product_type": "digital_product"}
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        self.assertIn("digital_product", out.flag_reasons[0])


# ============================================================
# 3. Content gating (corrected): HSN-held vs Tax/UOM-flagged
# ============================================================


class TestContentGating(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account_name = _ensure_account_for_pull()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.account = frappe.get_doc("EasyEcom Account", cls.account_name)
        cls.companies: list[str] = []  # no Companies → no tax stamping for these tests

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def _process(self, payload: dict) -> Any:
        return process_one_product(
            payload,
            account=self.account,
            executor=self.executor,
            enabled_companies=self.companies,
        )

    def test_missing_hsn_held_as_flagged_not_created(self) -> None:
        """India Compliance enforces gst_hsn_code mandatory — we cannot
        create the Item, so HOLD as FNC (do NOT placeholder)."""
        _ensure_uom("Nos")
        payload = {
            "sku": f"{PREFIX}no-hsn",
            "product_type": "normal_product",
            "product_name": "no-hsn",
            "hsn_code": None,  # missing
            "accounting_unit": "Nos",
            "active": 1,
            "product_id": 1, "cp_id": 2,
        }
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        self.assertIn("HSN", out.flag_reasons[0])
        # No Item created.
        self.assertFalse(frappe.db.exists("Item", payload["sku"]))

    def test_unknown_hsn_held_as_flagged_not_created(self) -> None:
        """HSN present but not in the GST HSN Code library → still FNC
        (creating the item would fail India Compliance validation)."""
        payload = {
            "sku": f"{PREFIX}bad-hsn",
            "product_type": "normal_product",
            "product_name": "bad-hsn",
            "hsn_code": "99999999991",  # not seeded
            "accounting_unit": "Nos",
            "active": 1,
        }
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        self.assertFalse(frappe.db.exists("Item", payload["sku"]))

    def test_dirty_uom_created_flagged_with_default_substituted(self) -> None:
        """EE's accounting_unit='111' isn't a UOM — substitute the
        Account's default_uom and flag Created-Flagged. Item IS created
        (the SKU must exist for downstream orders)."""
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        payload = {
            "sku": f"{PREFIX}dirty-uom",
            "product_type": "normal_product",
            "product_name": "dirty-uom",
            "hsn_code": "85171000",
            "accounting_unit": "111",
            "active": 1,
        }
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_CREATED_FLAGGED)
        # Item created with the substituted UOM.
        item = frappe.get_doc("Item", payload["sku"])
        self.assertEqual(item.stock_uom, "Nos")
        # Flag reason mentions the dirt.
        joined = " ".join(out.flag_reasons)
        self.assertIn("111", joined)
        self.assertIn("Nos", joined)

    def test_clean_payload_mapped_no_flags(self) -> None:
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        payload = {
            "sku": f"{PREFIX}clean",
            "product_type": "normal_product",
            "product_name": "clean",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
        }
        out = self._process(payload)
        self.assertEqual(out.status, STATUS_MAPPED)
        self.assertEqual(out.flag_reasons, [])
        self.assertTrue(frappe.db.exists("Item", payload["sku"]))


# ============================================================
# 4. Matching (§8.1.3)
# ============================================================


class TestMatching(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account_name = _ensure_account_for_pull()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.account = frappe.get_doc("EasyEcom Account", cls.account_name)
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def _payload(self, sku: str) -> dict:
        return {
            "sku": sku,
            "product_type": "normal_product",
            "product_name": f"name-{sku}",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
        }

    def _process(self, payload: dict) -> Any:
        return process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[],
        )

    def test_no_existing_creates_new(self) -> None:
        sku = f"{PREFIX}match-new"
        out = self._process(self._payload(sku))
        self.assertEqual(out.status, STATUS_MAPPED)
        self.assertTrue(out.created)
        self.assertEqual(out.erpnext_name, sku)

    def test_exact_item_code_auto_maps(self) -> None:
        """An ERPNext Item already exists at item_code = sku, no map
        row yet — auto-map + create map row, do NOT create a new Item."""
        sku = f"{PREFIX}match-exact"
        # Pre-create the Item.
        item = frappe.new_doc("Item")
        item.update(
            {
                "item_code": sku,
                "item_name": sku,
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "gst_hsn_code": "85171000",
            }
        )
        item.insert(ignore_permissions=True)
        frappe.db.commit()
        # No map row yet:
        self.assertFalse(
            frappe.db.exists("EasyEcom Item Map", {"ee_sku": sku})
        )
        out = self._process(self._payload(sku))
        self.assertFalse(out.created)  # auto-mapped, not created
        self.assertTrue(
            frappe.db.exists("EasyEcom Item Map", {"ee_sku": sku})
        )

    def test_existing_map_reuses_mapped_item(self) -> None:
        """Map exists pointing to Item X — pull reuses, does not
        create a new Item even if a same-code Item also coincidentally
        existed (which it does in our setup)."""
        sku = f"{PREFIX}match-mapped"
        # Pre-create Item + map.
        item = frappe.new_doc("Item")
        item.update(
            {
                "item_code": sku,
                "item_name": sku,
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "gst_hsn_code": "85171000",
            }
        )
        item.insert(ignore_permissions=True)
        map_doc = frappe.new_doc("EasyEcom Item Map")
        map_doc.update(
            {
                "ee_sku": sku,
                "erpnext_doctype": "Item",
                "erpnext_name": item.name,
                "status": STATUS_MAPPED,
            }
        )
        map_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        before_count = frappe.db.count("Item", {"item_code": sku})
        self._process(self._payload(sku))
        after_count = frappe.db.count("Item", {"item_code": sku})
        self.assertEqual(before_count, after_count)


# ============================================================
# 5. Multi-Company tax append + idempotent
# ============================================================


class TestMultiCompanyTaxStamp(FrappeTestCase):
    """The critical correctness hinge: stamping for Company A then
    Company B must yield BOTH companies' Item Tax rows on the same
    Item. A re-pull must NOT duplicate. The Stage 2 patch to the 8c
    resolver (_stamp_preview_onto_item: append+dedupe) backs this."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account_name = _ensure_account_for_pull()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.account = frappe.get_doc("EasyEcom Account", cls.account_name)
        cls.company_a = _ensure_test_company("_Test Company")
        # Use a second company name we know India Compliance ships
        # templates for — _Test Company 1 is the standard second test Co.
        cls.company_b = _ensure_test_company("_Test Company 1")
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()
        # Wipe Tax Rule Maps for the test rule.
        for n in frappe.db.get_all(
            "EasyEcom Tax Rule Map",
            filters={"tax_rule_name": f"{PREFIX}TaxRule"},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Tax Rule Map", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        _ensure_company_settings(self.company_a, enabled=1)
        _ensure_company_settings(self.company_b, enabled=1)
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe()

    def _seed_tax_map(self, company: str, template: str) -> str:
        """Create a Configured Tax Rule Map with one stamp row."""
        doc = frappe.new_doc("EasyEcom Tax Rule Map")
        doc.update(
            {
                "tax_rule_name": f"{PREFIX}TaxRule",
                "company": company,
                "workflow_state": "To Configure",
            }
        )
        doc.append(
            "taxes",
            {
                "item_tax_template": template,
                "tax_category": None,
                "valid_from": None,
                "minimum_net_rate": 0,
                "maximum_net_rate": 0,
            },
        )
        doc.insert(ignore_permissions=True)
        # Bypass workflow guard for the test.
        frappe.db.set_value(
            "EasyEcom Tax Rule Map",
            doc.name,
            "workflow_state",
            "Configured",
            update_modified=False,
        )
        return doc.name

    def test_both_companies_rows_coexist_on_one_item(self) -> None:
        # Each Company has its own GST template (India Compliance).
        template_a = self._first_gst_template(self.company_a)
        template_b = self._first_gst_template(self.company_b)
        self._seed_tax_map(self.company_a, template_a)
        self._seed_tax_map(self.company_b, template_b)

        payload = {
            "sku": f"{PREFIX}multi-co",
            "product_type": "normal_product",
            "product_name": "multi-co",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
            "tax_rule_name": f"{PREFIX}TaxRule",
            "tax_rate": 0.18,
        }
        process_one_product(
            payload,
            account=self.account,
            executor=self.executor,
            enabled_companies=[self.company_a, self.company_b],
        )
        item = frappe.get_doc("Item", payload["sku"])
        templates_on_item = {t.item_tax_template for t in item.taxes}
        self.assertIn(template_a, templates_on_item)
        self.assertIn(template_b, templates_on_item)

    def test_repull_does_not_duplicate_rows(self) -> None:
        template_a = self._first_gst_template(self.company_a)
        template_b = self._first_gst_template(self.company_b)
        self._seed_tax_map(self.company_a, template_a)
        self._seed_tax_map(self.company_b, template_b)

        payload = {
            "sku": f"{PREFIX}idem",
            "product_type": "normal_product",
            "product_name": "idem",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
            "tax_rule_name": f"{PREFIX}TaxRule",
            "tax_rate": 0.18,
        }
        # First pull
        process_one_product(
            payload,
            account=self.account,
            executor=self.executor,
            enabled_companies=[self.company_a, self.company_b],
        )
        rows_after_first = frappe.db.count(
            "Item Tax", filters={"parent": payload["sku"]}
        )
        # Second pull — same payload.
        process_one_product(
            payload,
            account=self.account,
            executor=self.executor,
            enabled_companies=[self.company_a, self.company_b],
        )
        rows_after_second = frappe.db.count(
            "Item Tax", filters={"parent": payload["sku"]}
        )
        self.assertEqual(rows_after_first, rows_after_second)

    def test_no_enabled_companies_skips_tax_silently(self) -> None:
        """An account with no enabled Company Settings still creates
        the Item; the tax loop is skipped but per-Item flag-spam is
        avoided — the no-Co warning is logged ONCE in pull_products,
        not per-item. The map row therefore stays Mapped (clean), not
        Created-Flagged for what's a global config issue."""
        _ensure_company_settings(self.company_a, enabled=0)
        _ensure_company_settings(self.company_b, enabled=0)
        frappe.db.commit()
        payload = {
            "sku": f"{PREFIX}no-co",
            "product_type": "normal_product",
            "product_name": "no-co",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
        }
        out = process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[],
        )
        # Item exists, no tax rows, no per-item flag — Mapped is the
        # right map row state because nothing about THIS product is
        # flagged; the issue is account-level.
        self.assertEqual(out.status, STATUS_MAPPED)
        item = frappe.get_doc("Item", payload["sku"])
        self.assertEqual(len(item.taxes), 0)

    def test_company_edit_propagates_no_stale_rows(self) -> None:
        """The stale-row window that closing this Stage-2 follow-up is
        about: an FDE edits Company A's Tax Rule Map after the first
        pull (swaps template1 → template2). The next pull must end
        with ONLY template2 on the item — no template1 ghost row.

        Under the old append+dedupe (the §8d first cut), template1
        would have lingered; ERPNext would have been free to resolve
        it at invoice time. Under per-Company REPLACE, template1's
        row is identified as Company A's (via Item Tax Template's
        `company` link), deleted, and replaced by template2."""
        template1 = self._first_gst_template(self.company_a)
        template2 = self._second_gst_template(self.company_a, exclude=template1)
        map_name = self._seed_tax_map(self.company_a, template1)

        payload = {
            "sku": f"{PREFIX}edit-prop",
            "product_type": "normal_product",
            "product_name": "edit-prop",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
            "tax_rule_name": f"{PREFIX}TaxRule",
            "tax_rate": 0.18,
        }
        # First pull — Company A stamps template1.
        process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[self.company_a],
        )
        item = frappe.get_doc("Item", payload["sku"])
        templates_after_first = {t.item_tax_template for t in item.taxes}
        self.assertEqual(templates_after_first, {template1})

        # FDE edits Company A's Tax Rule Map: template1 → template2.
        map_doc = frappe.get_doc("EasyEcom Tax Rule Map", map_name)
        map_doc.set("taxes", [])
        map_doc.append(
            "taxes",
            {"item_tax_template": template2, "tax_category": None,
             "valid_from": None, "minimum_net_rate": 0, "maximum_net_rate": 0},
        )
        map_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Re-pull. Per-Company REPLACE wipes template1 (Company A's
        # stale row) and writes template2 (Company A's new fresh row).
        process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[self.company_a],
        )
        item = frappe.get_doc("Item", payload["sku"])
        templates_after_second = {t.item_tax_template for t in item.taxes}
        self.assertEqual(
            templates_after_second,
            {template2},
            "Stale template1 should be GONE after the map edit; "
            f"got {templates_after_second}",
        )
        self.assertNotIn(template1, templates_after_second)

    def test_per_company_replace_leaves_other_companies_intact(self) -> None:
        """Stamp A and B; edit A; re-stamp A; assert B's rows are
        byte-for-byte the same. Proves the per-Company REPLACE is
        scoped — only Company A's rows are touched, never B's."""
        template_a1 = self._first_gst_template(self.company_a)
        template_a2 = self._second_gst_template(self.company_a, exclude=template_a1)
        template_b = self._first_gst_template(self.company_b)
        map_a = self._seed_tax_map(self.company_a, template_a1)
        self._seed_tax_map(self.company_b, template_b)

        payload = {
            "sku": f"{PREFIX}b-untouched",
            "product_type": "normal_product",
            "product_name": "b-untouched",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
            "tax_rule_name": f"{PREFIX}TaxRule",
            "tax_rate": 0.18,
        }
        # First pull — A and B both stamp.
        process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[self.company_a, self.company_b],
        )
        item_before = frappe.get_doc("Item", payload["sku"])
        # Capture B's rows as a sorted-tuple snapshot, BEFORE A's edit.
        b_rows_before = sorted(
            (
                t.item_tax_template,
                t.tax_category or "",
                str(t.valid_from or ""),
                float(t.minimum_net_rate or 0),
                float(t.maximum_net_rate or 0),
            )
            for t in item_before.taxes
            if t.item_tax_template == template_b
        )
        self.assertEqual(len(b_rows_before), 1)

        # Edit A only.
        map_a_doc = frappe.get_doc("EasyEcom Tax Rule Map", map_a)
        map_a_doc.set("taxes", [])
        map_a_doc.append(
            "taxes",
            {"item_tax_template": template_a2, "tax_category": None,
             "valid_from": None, "minimum_net_rate": 0, "maximum_net_rate": 0},
        )
        map_a_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Re-pull — A re-stamps with template_a2; B is NOT re-stamped
        # here (the loop calls A then B, but we only loop A in this
        # call to isolate the property). We must explicitly call with
        # only A to prove the per-Company scoping: if B's rows survive
        # an A-only re-stamp, the scoping is correct.
        process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[self.company_a],
        )
        item_after = frappe.get_doc("Item", payload["sku"])

        # A's rows: only template_a2 (template_a1 gone).
        a_after = {t.item_tax_template for t in item_after.taxes
                   if t.item_tax_template in {template_a1, template_a2}}
        self.assertEqual(a_after, {template_a2})

        # B's rows: byte-for-byte unchanged from before.
        b_rows_after = sorted(
            (
                t.item_tax_template,
                t.tax_category or "",
                str(t.valid_from or ""),
                float(t.minimum_net_rate or 0),
                float(t.maximum_net_rate or 0),
            )
            for t in item_after.taxes
            if t.item_tax_template == template_b
        )
        self.assertEqual(
            b_rows_after,
            b_rows_before,
            "Company B's rows must be untouched by an A-only re-stamp",
        )

    # --- helpers ---

    def _first_gst_template(self, company: str) -> str:
        """Pick any output-tax template that exists for this Company."""
        row = frappe.db.get_value(
            "Item Tax Template",
            filters={"company": company, "disabled": 0},
            fieldname="name",
        )
        if not row:
            raise RuntimeError(
                f"No Item Tax Template seeded for {company} — install India "
                "Compliance fixtures."
            )
        return row

    def _second_gst_template(self, company: str, *, exclude: str) -> str:
        """A DIFFERENT GST template for the same Company — used to
        simulate an FDE rate-change edit. India Compliance ships
        several per-Company; pick the first one that isn't `exclude`."""
        rows = frappe.db.get_all(
            "Item Tax Template",
            filters={"company": company, "disabled": 0, "name": ("!=", exclude)},
            fields=["name"],
            order_by="name asc",
            limit=1,
        )
        if not rows:
            raise RuntimeError(
                f"Need a second Item Tax Template for {company} to test "
                "stale-row replacement; only one exists."
            )
        return rows[0].name


# ============================================================
# 6. Resolver append regression — single-Company unchanged
# ============================================================


class TestResolverSingleCompanyRegression(FrappeTestCase):
    """The Stage 2 patch to _stamp_preview_onto_item changed behaviour
    from clear-then-write to append+dedupe. Confirm a single-Company
    call still ends up with exactly the map's rows — same final state
    as before (the existing 8c test suite is the primary regression
    surface; this is a Stage-2-local sanity)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_item_group()
        _ensure_uom("Nos")

    def setUp(self) -> None:
        for n in frappe.db.get_all(
            "EasyEcom Tax Rule Map",
            filters={"tax_rule_name": f"{PREFIX}Single"},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Tax Rule Map", n, force=True, ignore_permissions=True
            )
        frappe.db.commit()

    def test_empty_item_one_stamp_yields_map_rows(self) -> None:
        template = frappe.db.get_value(
            "Item Tax Template",
            filters={"company": self.company, "disabled": 0},
            fieldname="name",
        )
        # Configured Tax Rule Map.
        doc = frappe.new_doc("EasyEcom Tax Rule Map")
        doc.update(
            {
                "tax_rule_name": f"{PREFIX}Single",
                "company": self.company,
                "workflow_state": "To Configure",
            }
        )
        doc.append(
            "taxes",
            {"item_tax_template": template, "tax_category": None, "valid_from": None,
             "minimum_net_rate": 0, "maximum_net_rate": 0},
        )
        doc.insert(ignore_permissions=True)
        frappe.db.set_value(
            "EasyEcom Tax Rule Map", doc.name, "workflow_state", "Configured",
            update_modified=False,
        )
        frappe.db.commit()

        # In-memory item; resolver mutates in place.
        item = frappe.new_doc("Item")
        item.update(
            {"item_code": f"{PREFIX}resolver-single", "item_name": "x",
             "item_group": "All Item Groups", "stock_uom": "Nos"}
        )
        result = resolve_and_stamp_tax(
            item, {"tax_rule_name": f"{PREFIX}Single", "tax_rate": 0.18},
            self.company,
        )
        self.assertTrue(result.mapped)
        self.assertEqual(result.stamped_count, 1)
        self.assertEqual(len(item.taxes), 1)


# ============================================================
# 7. Cursor walk + resume + savepoint isolation
# ============================================================


class TestCursorAndIsolation(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account_name = _ensure_account_for_pull()
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()
        # Reset cursor on the Account.
        frappe.db.set_value(
            "EasyEcom Account",
            self.account_name,
            {
                "item_pull_cursor": None,
                "item_pull_cursor_at": None,
                "item_pull_total_seen": 0,
                "item_pull_last_updated_at": None,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe()

    def _clean_payload(self, sku: str) -> dict:
        return {
            "sku": sku,
            "product_type": "normal_product",
            "product_name": f"name-{sku}",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
            "product_id": hash(sku) % 10000,
            "cp_id": (hash(sku) + 1) % 10000,
        }

    def test_two_page_walk_consumes_all_products(self) -> None:
        client = MockClient(
            count=4,
            pages=[
                {
                    "data": [
                        self._clean_payload(f"{PREFIX}cw-1"),
                        self._clean_payload(f"{PREFIX}cw-2"),
                    ],
                    "nextUrl": "/Products/GetProductMaster?cursor=PAGE2",
                },
                {
                    "data": [
                        self._clean_payload(f"{PREFIX}cw-3"),
                        self._clean_payload(f"{PREFIX}cw-4"),
                    ],
                    "nextUrl": None,
                },
            ],
        )
        result = pull_products(
            account_name=self.account_name, client=client, start_fresh=True
        )
        self.assertEqual(result.total_count_reported, 4)
        self.assertEqual(result.pages_walked, 2)
        self.assertEqual(result.products_processed, 4)
        # All four items created.
        for i in range(1, 5):
            self.assertTrue(frappe.db.exists("Item", f"{PREFIX}cw-{i}"))
        # Cursor cleared after clean walk; high-water set.
        account = frappe.get_doc("EasyEcom Account", self.account_name)
        self.assertFalse(account.item_pull_cursor)
        self.assertIsNotNone(account.item_pull_last_updated_at)
        self.assertEqual(account.item_pull_total_seen, 4)

    def test_resume_from_persisted_cursor(self) -> None:
        # Pre-set a cursor on the Account; the mock client will see
        # _request (absolute-url continuation) instead of get.
        frappe.db.set_value(
            "EasyEcom Account",
            self.account_name,
            "item_pull_cursor",
            "/Products/GetProductMaster?cursor=RESUME_HERE",
            update_modified=False,
        )
        frappe.db.commit()
        client = MockClient(
            count=2,
            pages=[
                {
                    "data": [self._clean_payload(f"{PREFIX}res-1")],
                    "nextUrl": None,
                }
            ],
        )
        result = pull_products(
            account_name=self.account_name, client=client, start_fresh=False
        )
        # First call should have gone to _request with the resume URL,
        # NOT to PRODUCT_MASTER_GET via get().
        first_get_calls = [c for c in client.calls if c[1] == PRODUCT_MASTER_GET]
        # First "GET PRODUCT_MASTER_GET" call should be absent (we resumed).
        self.assertEqual(len(first_get_calls), 0)
        # An absolute-url call should appear via _request.
        abs_calls = [c for c in client.calls if "RESUME_HERE" in c[1]]
        self.assertGreaterEqual(len(abs_calls), 1)
        self.assertEqual(result.products_processed, 1)

    def test_relative_cursor_is_followed_with_absolute_false(self) -> None:
        """Regression: EE Product Master returns nextUrl as a RELATIVE
        path ("/Products/GetProductMaster?cursor=..."). Production bug
        had the flow always passing _is_absolute_url=True, which made
        requests fire MissingSchema for relative URLs. Flow now
        detects the scheme — relative cursors must be followed with
        _is_absolute_url=False so the client prepends api_endpoint."""
        relative_cursor = "/Products/GetProductMaster?cursor=ABC123"
        frappe.db.set_value(
            "EasyEcom Account",
            self.account_name,
            "item_pull_cursor",
            relative_cursor,
            update_modified=False,
        )
        frappe.db.commit()
        client = MockClient(
            count=1,
            pages=[{"data": [self._clean_payload(f"{PREFIX}rel-1")],
                    "nextUrl": None}],
        )
        pull_products(
            account_name=self.account_name, client=client, start_fresh=False
        )
        # The flow must have called _request with _is_absolute_url=False
        # for the relative cursor (otherwise requests fires MissingSchema
        # in production — the bug fix pin).
        self.assertEqual(
            client.absolute_flags, [False],
            "Relative cursor must be followed with _is_absolute_url=False; "
            f"got {client.absolute_flags}",
        )

    def test_absolute_cursor_is_followed_with_absolute_true(self) -> None:
        """Defensive: if EE ever returns an absolute URL in nextUrl
        (other bulk endpoints do; Product Master doesn't today), the
        flow should still handle it cleanly via _is_absolute_url=True
        so the client doesn't re-prepend api_endpoint."""
        absolute_cursor = "https://api.easyecom.io/Products/GetProductMaster?cursor=XYZ"
        frappe.db.set_value(
            "EasyEcom Account",
            self.account_name,
            "item_pull_cursor",
            absolute_cursor,
            update_modified=False,
        )
        frappe.db.commit()
        client = MockClient(
            count=1,
            pages=[{"data": [self._clean_payload(f"{PREFIX}abs-1")],
                    "nextUrl": None}],
        )
        pull_products(
            account_name=self.account_name, client=client, start_fresh=False
        )
        self.assertEqual(
            client.absolute_flags, [True],
            f"Absolute cursor must be followed with _is_absolute_url=True; "
            f"got {client.absolute_flags}",
        )

    def test_data_string_no_data_found_treated_as_empty(self) -> None:
        """Regression: EE returns `{"data": "No Data Found"}` (string,
        not list) when a cursor walks past the last product page.
        Without normalisation, the caller iterates the string char by
        char - each char becomes a "record" - and downstream
        `.get(...)` calls on a str raise AttributeError. Observed
        live in the Harmony sandbox 2026-05-26: 13 chars of
        "No Data Found" -> 13 spurious AttributeError failures."""
        client = MockClient(
            count=0,
            # MockClient.get will return whatever is in pages[0]; use
            # the EE-shape string-data response to mimic the wire.
            pages=[{"data": "No Data Found", "nextUrl": None}],
        )
        result = pull_products(
            account_name=self.account_name, client=client, start_fresh=True
        )
        self.assertEqual(result.products_processed, 0)
        self.assertEqual(result.pages_walked, 1)
        self.assertEqual(result.page_failures, [])

    def test_savepoint_isolation_one_bad_product(self) -> None:
        """A product whose payload makes the executor raise should not
        abort siblings on the same page. Use one with required-field
        missing (no sku) — the per-product handler raises ValueError;
        the savepoint rolls it back; the other 2 still get processed."""
        client = MockClient(
            count=3,
            pages=[
                {
                    "data": [
                        self._clean_payload(f"{PREFIX}iso-1"),
                        # Bad product: no sku at all → ValueError in handler.
                        {"product_type": "normal_product"},
                        self._clean_payload(f"{PREFIX}iso-3"),
                    ],
                    "nextUrl": None,
                }
            ],
        )
        result = pull_products(
            account_name=self.account_name, client=client, start_fresh=True
        )
        # Siblings created.
        self.assertTrue(frappe.db.exists("Item", f"{PREFIX}iso-1"))
        self.assertTrue(frappe.db.exists("Item", f"{PREFIX}iso-3"))
        # Bad one logged as a failure.
        self.assertEqual(len(result.page_failures), 1)


# ============================================================
# 8. Lifecycle — active 0/1 → disabled
# ============================================================


class TestLifecycleDisable(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account_name = _ensure_account_for_pull()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.account = frappe.get_doc("EasyEcom Account", cls.account_name)
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_active_zero_disables_item(self) -> None:
        payload = {
            "sku": f"{PREFIX}disabled",
            "product_type": "normal_product",
            "product_name": "x",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 0,
        }
        process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[],
        )
        item = frappe.get_doc("Item", payload["sku"])
        self.assertEqual(item.disabled, 1)

    def test_active_one_keeps_item_enabled(self) -> None:
        payload = {
            "sku": f"{PREFIX}enabled",
            "product_type": "normal_product",
            "product_name": "y",
            "hsn_code": "85171000",
            "accounting_unit": "Nos",
            "active": 1,
        }
        process_one_product(
            payload, account=self.account, executor=self.executor,
            enabled_companies=[],
        )
        item = frappe.get_doc("Item", payload["sku"])
        self.assertEqual(item.disabled, 0)


class TestNonFoundationalClientConstruction(FrappeTestCase):
    """Regression: §8d Item Pull/Push endpoints are NON-foundational
    (§31.2.4). The EasyEcom API Call controller's validate() throws
    'Non-foundational API Calls require either a Company or a Location
    Key' when a row lands with both blank. Item flows are account-wide
    (no location_key), so client.company MUST be populated.

    Two prior fixes in this lineage:
      1. b5b5fd6 — removed bad `EasyEcomClient(account=...)` kwarg that
         hit TypeError; replaced with `EasyEcomClient()`.
      2. THIS fix — `EasyEcomClient()` constructs the client with
         company=None, which the validate() throw caught on the first
         real Discover Products run. Must pass company=.

    The MockClient-based tests above DO bypass this branch (they pass
    client= explicitly), so they don't cover construction. This class
    spies on the real EasyEcomClient symbol with mock.patch to assert
    the kwargs at the construction site itself."""

    def setUp(self) -> None:
        _wipe(PREFIX)
        _ensure_test_company()
        self.account_name = _ensure_account_for_pull()

    def test_pull_constructs_client_with_company(self) -> None:
        from unittest.mock import MagicMock, patch

        captured: dict[str, Any] = {}

        def _spy(*args: Any, **kwargs: Any) -> Any:
            captured["args"] = args
            captured["kwargs"] = kwargs
            # Return a stand-in that satisfies the rest of pull_products
            # enough to exit cleanly. _read_total_count and _iter_pages
            # both go through client; an empty iterable + None count
            # short-circuits the walk.
            m = MagicMock()
            m.get.return_value = {"data": [], "totalCount": 0}
            return m

        with patch(
            "ecommerce_super.easyecom.flows.item_pull.EasyEcomClient",
            side_effect=_spy,
        ):
            pull_products(account_name=self.account_name, client=None)

        # company= must be present and resolve to a real Company name
        # (validate() throws when both company AND location_key blank
        # on a non-foundational call).
        self.assertIn(
            "company", captured["kwargs"],
            "EasyEcomClient must be constructed with company= for "
            "non-foundational Item Pull calls; got "
            f"args={captured.get('args')} kwargs={captured.get('kwargs')}",
        )
        co = captured["kwargs"]["company"]
        self.assertTrue(
            co and frappe.db.exists("Company", co),
            f"company= must be a real Company; got {co!r}",
        )

    def test_push_one_product_constructs_client_with_company(self) -> None:
        """Same regression on the push side. The 4 push call sites
        (push_all_pending_items, push_one_product whitelist,
        push_lifecycle_product whitelist, item_push_queue_handler) all
        construct EasyEcomClient — the whitelist wrappers are the
        path the desk buttons hit, so cover one of them here."""
        from unittest.mock import MagicMock, patch

        from ecommerce_super.easyecom.flows.item_push import (
            push_one_product,
        )

        # Need a real Item so the whitelist doesn't bail on "Item not
        # found" before reaching client construction. Minimal payload.
        item_code = f"{PREFIX}push-client-co"
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()
        if not frappe.db.exists("Item", item_code):
            it = frappe.new_doc("Item")
            it.item_code = item_code
            it.item_name = item_code
            it.item_group = "All Item Groups"
            it.stock_uom = "Nos"
            it.gst_hsn_code = "85171000"
            it.insert(ignore_permissions=True)

        captured: dict[str, Any] = {}

        def _spy(*args: Any, **kwargs: Any) -> Any:
            captured["args"] = args
            captured["kwargs"] = kwargs
            m = MagicMock()
            # push_one_product → push_one_item → executor.run → EE.post;
            # we never reach EE because we'll patch push_one_item too
            # to a no-op. Just return something callable.
            return m

        with patch(
            "ecommerce_super.easyecom.flows.item_push.EasyEcomClient",
            side_effect=_spy,
        ), patch(
            "ecommerce_super.easyecom.flows.item_push.push_one_item",
            return_value=MagicMock(
                pushed=False, operation="skipped",
                ee_product_id=None, flag_reasons=[],
            ),
        ):
            result = push_one_product(item_code=item_code)

        self.assertTrue(result.get("ok"), f"push wrapper failed: {result}")
        self.assertIn(
            "company", captured["kwargs"],
            "EasyEcomClient must be constructed with company= for "
            "non-foundational Item Push calls; got "
            f"args={captured.get('args')} kwargs={captured.get('kwargs')}",
        )
        co = captured["kwargs"]["company"]
        self.assertTrue(
            co and frappe.db.exists("Company", co),
            f"company= must be a real Company; got {co!r}",
        )
