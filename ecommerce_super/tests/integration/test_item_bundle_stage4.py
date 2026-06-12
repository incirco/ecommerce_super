"""§8d Stage 4 tests — Product Bundle ↔ EE combo, both directions.

⚠️ ZERO real EE traffic. All push paths exercised against
MockPushClient; pull paths run synchronously against the in-memory
combo payload (no client needed for the pull branch). The production
EasyEcomClient is never instantiated by any test in this module.

Coverage:
  PULL (combo → Product Bundle):
    - combo with all sub-products mapped → Product Bundle created;
      wrapper Item is_stock_item=0; bundle's own map row links to
      "Product Bundle", not "Item"; components are the resolved
      ERPNext Items.
    - combo with unmapped sub-product → bundle FLAGGED-NOT-CREATED
      (no broken Bundle); flag reason names the missing component.
    - combo with <2 resolvable sub-products → FLAGGED.
    - re-pull of same combo → Bundle refreshed in place, no
      duplicate Map row, no duplicate Bundle.

  PUSH (Product Bundle → EE combo):
    - bundle with all components pushed → CreateMasterProduct with
      itemType=1 + subProducts array built from component ee_skus.
    - bundle with an unpushed component (no map / no ee_product_id)
      → bundle FLAGGED-NOT-PUSHED (no broken combo), NO EE call.
    - bundle with <2 components → FLAGGED, no EE call.
    - itemType=1 for bundle wrapper (via ruleset conditional reading
      source_doc.flags.is_bundle_wrapper) vs 0 for normal item.
    - bundle gets its OWN map row (erpnext_doctype='Product Bundle')
      on Create; product_id writeback lands there, not on the
      wrapper Item's map.
    - existing bundle map with ee_product_id → routes to
      UpdateMasterProduct keyed on productId.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_CREATE,
    PRODUCT_MASTER_UPDATE,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.item_pull import (
    ITEM_PULL_RULESET,
    STATUS_FLAGGED_NOT_CREATED,
    STATUS_MAPPED,
    process_one_product,
)
from ecommerce_super.easyecom.flows.item_push import (
    ITEM_PUSH_RULESET,
    STATUS_FLAGGED_NOT_PUSHED,
    push_one_bundle,
    push_one_item,
)
from ecommerce_super.tests.factories import make_account
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


PREFIX = "TEST-8D-S4-"
GST_18_TEMPLATE = "GST 18% - TC"


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


def _ensure_account() -> Any:
    name = make_account(name=f"{PREFIX}acct".lower())
    frappe.db.set_value(
        "EasyEcom Account", name, {"default_uom": "Nos"}, update_modified=False
    )
    frappe.db.commit()
    return frappe.get_doc("EasyEcom Account", name)


def _ensure_company_settings(company: str, enabled: int = 1) -> None:
    existing = frappe.db.get_value(
        "EasyEcom Company Settings", {"company": company}, "name"
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Company Settings", existing,
            {"enabled": enabled}, update_modified=False,
        )
    else:
        doc = frappe.new_doc("EasyEcom Company Settings")
        doc.update({"company": company, "enabled": enabled})
        doc.insert(ignore_permissions=True)
    frappe.db.commit()


def _make_normal_item(
    item_code: str,
    *,
    tax_template: str | None = None,
    is_stock_item: int = 1,
    ee_product_id: str | None = None,
) -> Any:
    """Make a normal Item. Optionally insert an EasyEcom Item Map
    row pointing at it with `ee_product_id` set (i.e. the
    'already pushed/pulled' state)."""
    if frappe.db.exists("Item", item_code):
        # Wipe map rows first.
        for n in frappe.db.get_all(
            "EasyEcom Item Map",
            filters={"erpnext_name": item_code},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Item Map", n, force=True, ignore_permissions=True
            )
        frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
    item = frappe.new_doc("Item")
    item.update(
        {
            "item_code": item_code,
            "item_name": item_code,
            "item_group": _ensure_item_group(),
            "stock_uom": _ensure_uom(),
            "gst_hsn_code": _ensure_hsn(),
            "is_stock_item": is_stock_item,
            "weight_per_unit": 100,
            "ecs_length_cm": 10,
            "ecs_height_cm": 5,
            "ecs_width_cm": 3,
        }
    )
    if tax_template:
        item.append(
            "taxes",
            {
                "item_tax_template": tax_template,
                "tax_category": None,
                "valid_from": None,
                "minimum_net_rate": 0,
                "maximum_net_rate": 0,
            },
        )
    item.insert(ignore_permissions=True)
    if ee_product_id:
        # Mapped + already pushed state.
        m = frappe.new_doc("EasyEcom Item Map")
        m.update(
            {
                "ee_sku": item_code,
                "erpnext_doctype": "Item",
                "erpnext_name": item_code,
                "ee_product_id": ee_product_id,
                "status": STATUS_MAPPED,
            }
        )
        m.insert(ignore_permissions=True)
    return item


def _make_product_bundle(
    bundle_name: str, *, component_codes: list[str]
) -> tuple[Any, Any]:
    """Create the wrapper Item + Product Bundle from a list of
    already-existing component item_codes."""
    if frappe.db.exists("Product Bundle", bundle_name):
        frappe.delete_doc(
            "Product Bundle", bundle_name, force=True, ignore_permissions=True
        )
    if frappe.db.exists("Item", bundle_name):
        frappe.delete_doc(
            "Item", bundle_name, force=True, ignore_permissions=True
        )
    wrapper = _make_normal_item(bundle_name, is_stock_item=0)
    bundle = frappe.new_doc("Product Bundle")
    bundle.update({"new_item_code": wrapper.item_code})
    for c in component_codes:
        bundle.append("items", {"item_code": c, "qty": 1})
    bundle.insert(ignore_permissions=True)
    return wrapper, bundle


def _wipe(prefix: str = PREFIX) -> None:
    # Bundle first (FK), then Map, then Items.
    for n in frappe.db.get_all(
        "Product Bundle",
        filters={"new_item_code": ("like", f"{prefix}%")},
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
        filters={"ee_sku": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Item Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom Item Map",
        filters={"erpnext_name": ("like", f"{prefix}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Item Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "Item", filters={"item_code": ("like", f"{prefix}%")}, pluck="name"
    ):
        try:
            frappe.delete_doc("Item", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()


def _combo_payload(
    sku: str, *, sub_skus: list[str], product_id: int = 999, cp_id: int = 998
) -> dict:
    """Build a synthetic combo_product payload with `sub_skus`."""
    return {
        "sku": sku,
        "product_type": "combo_product",
        "product_name": sku,
        "hsn_code": "85171000",
        "accounting_unit": "Nos",
        "active": 1,
        "product_id": product_id,
        "cp_id": cp_id,
        "weight": 200,
        "height": 10,
        "length": 20,
        "width": 5,
        "cost": 100,
        "mrp": 200,
        "sub_products": [{"sku": s, "quantity": 1} for s in sub_skus],
    }


class MockPushClient:
    """Same surface as the Stage-3 mock — see test_item_push_stage3."""

    def __init__(
        self,
        *,
        create_returns: dict | None = None,
        update_returns: dict | None = None,
    ) -> None:
        self._create_response = create_returns or {
            "status": "success", "data": {"product_id": 999001},
        }
        self._update_response = update_returns or {"status": "success"}
        self.calls: list[tuple[str, dict]] = []

    def post(self, endpoint: str, payload: dict, **kwargs) -> dict:
        self.calls.append((endpoint, dict(payload)))
        if endpoint == PRODUCT_MASTER_CREATE:
            return self._create_response
        if endpoint == PRODUCT_MASTER_UPDATE:
            return self._update_response
        raise NotImplementedError(f"MockPushClient.post: unknown {endpoint!r}")

    def get(self, *args, **kwargs):
        raise NotImplementedError("Stage 4 push must not read EE")


# ============================================================
# 1. Combo PULL → Product Bundle
# ============================================================


class TestComboPull(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.tax_template = GST_18_TEMPLATE
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def _seed_component(self, item_code: str) -> str:
        """Create an Item + a map row (as if the component was
        already pulled/pushed in Stage 2/3)."""
        _make_normal_item(item_code, tax_template=self.tax_template)
        m = frappe.new_doc("EasyEcom Item Map")
        m.update(
            {
                "ee_sku": item_code,
                "erpnext_doctype": "Item",
                "erpnext_name": item_code,
                "ee_product_id": f"PID-{item_code}",
                "status": STATUS_MAPPED,
            }
        )
        m.insert(ignore_permissions=True)
        return item_code

    def _process(self, payload: dict) -> Any:
        return process_one_product(
            payload,
            account=self.account,
            executor=self.executor,
            enabled_companies=[self.company],
        )

    def test_combo_with_resolved_components_creates_bundle(self) -> None:
        # Components pulled first (as if Stage 2 mapped them).
        self._seed_component(f"{PREFIX}c-a")
        self._seed_component(f"{PREFIX}c-b")
        combo_sku = f"{PREFIX}combo-ok"
        out = self._process(_combo_payload(
            combo_sku, sub_skus=[f"{PREFIX}c-a", f"{PREFIX}c-b"]
        ))
        # Bundle WAS created (the Stage-4 contract). Status reflects the
        # multi-Co tax loop: our synthetic combo has no `tax_rule_name`,
        # so the 8c resolver flags it as a content problem on the wrapper
        # → Created-Flagged. (Status=Mapped would require pre-seeding a
        # configured Tax Rule Map, which isn't what this test is about.)
        # The bundle creation itself is the assertion that matters.
        self.assertIn(out.status, (STATUS_MAPPED, "Created-Flagged"))
        self.assertEqual(out.erpnext_doctype, "Product Bundle")
        self.assertTrue(out.created)
        # Bundle row exists; wrapper Item is non-stock.
        self.assertTrue(
            frappe.db.exists("Product Bundle", {"new_item_code": combo_sku})
        )
        wrapper = frappe.get_doc("Item", combo_sku)
        self.assertEqual(wrapper.is_stock_item, 0)
        # Map row links the Product Bundle, NOT the Item.
        map_row = frappe.get_doc("EasyEcom Item Map", {"ee_sku": combo_sku})
        self.assertEqual(map_row.erpnext_doctype, "Product Bundle")
        self.assertEqual(map_row.erpnext_name, combo_sku)
        # Bundle has both components resolved.
        bundle = frappe.get_doc("Product Bundle", combo_sku)
        component_codes = {row.item_code for row in bundle.items}
        self.assertEqual(
            component_codes,
            {f"{PREFIX}c-a", f"{PREFIX}c-b"},
        )

    def test_combo_with_unmapped_component_flags_bundle_not_created(self) -> None:
        """One sub-product has no map row → the WHOLE bundle is FNC'd
        (don't create a broken Bundle, §8.1.6 dependency contract)."""
        self._seed_component(f"{PREFIX}c-good")
        # f"{PREFIX}c-missing" is NOT seeded → unresolvable.
        combo_sku = f"{PREFIX}combo-missing"
        out = self._process(_combo_payload(
            combo_sku,
            sub_skus=[f"{PREFIX}c-good", f"{PREFIX}c-missing"],
        ))
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        joined = " ".join(out.flag_reasons)
        self.assertIn(f"{PREFIX}c-missing", joined)
        # Bundle was NOT created.
        self.assertFalse(
            frappe.db.exists("Product Bundle", {"new_item_code": combo_sku})
        )
        # Map row exists (FNC); points nowhere because creation didn't happen.
        map_row = frappe.get_doc("EasyEcom Item Map", {"ee_sku": combo_sku})
        self.assertEqual(map_row.status, STATUS_FLAGGED_NOT_CREATED)

    def test_combo_with_one_sub_product_flagged_under_min(self) -> None:
        self._seed_component(f"{PREFIX}c-only")
        combo_sku = f"{PREFIX}combo-tiny"
        out = self._process(_combo_payload(
            combo_sku, sub_skus=[f"{PREFIX}c-only"]
        ))
        self.assertEqual(out.status, STATUS_FLAGGED_NOT_CREATED)
        joined = " ".join(out.flag_reasons)
        # Post-97b8017 the flag-message wording moved from "sub-products"
        # (plural distinct count) to "sub-product" / "qty" / "components"
        # (total qty contract). Assert on substrings that name the
        # combo-qty concept so the test stays anchored on the substrate's
        # actual contract.
        self.assertIn("combo", joined)
        self.assertIn("sub-product", joined)
        self.assertFalse(
            frappe.db.exists("Product Bundle", {"new_item_code": combo_sku})
        )

    def test_repull_combo_refreshes_in_place(self) -> None:
        """Second pull of the same combo SKU updates the wrapper Item
        + Bundle in place (no duplicate map, no duplicate bundle)."""
        self._seed_component(f"{PREFIX}c-rp-a")
        self._seed_component(f"{PREFIX}c-rp-b")
        combo_sku = f"{PREFIX}combo-rp"
        self._process(_combo_payload(
            combo_sku, sub_skus=[f"{PREFIX}c-rp-a", f"{PREFIX}c-rp-b"]
        ))
        before_map_count = frappe.db.count(
            "EasyEcom Item Map", {"ee_sku": combo_sku}
        )
        before_bundle_count = frappe.db.count(
            "Product Bundle", {"new_item_code": combo_sku}
        )
        # Re-pull with the same payload.
        self._process(_combo_payload(
            combo_sku, sub_skus=[f"{PREFIX}c-rp-a", f"{PREFIX}c-rp-b"]
        ))
        self.assertEqual(
            frappe.db.count("EasyEcom Item Map", {"ee_sku": combo_sku}),
            before_map_count,
        )
        self.assertEqual(
            frappe.db.count("Product Bundle", {"new_item_code": combo_sku}),
            before_bundle_count,
        )


# ============================================================
# 2. Bundle PUSH → EE combo
# ============================================================


class TestBundlePush(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.tax_template = GST_18_TEMPLATE
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_push_bundle_with_all_components_pushed_creates_combo(self) -> None:
        # Two components already pushed (ee_product_id set).
        _make_normal_item(
            f"{PREFIX}p-c-a", tax_template=self.tax_template,
            ee_product_id="2001",
        )
        _make_normal_item(
            f"{PREFIX}p-c-b", tax_template=self.tax_template,
            ee_product_id="2002",
        )
        bundle_code = f"{PREFIX}bundle-create"
        _make_product_bundle(
            bundle_code,
            component_codes=[f"{PREFIX}p-c-a", f"{PREFIX}p-c-b"],
        )
        # The wrapper Item created by _make_product_bundle needs a tax
        # row so the push payload's TaxRate resolves.
        wrapper = frappe.get_doc("Item", bundle_code)
        wrapper.append(
            "taxes",
            {
                "item_tax_template": GST_18_TEMPLATE,
                "minimum_net_rate": 0, "maximum_net_rate": 0,
            },
        )
        wrapper.save(ignore_permissions=True)
        frappe.db.commit()

        client = MockPushClient(
            create_returns={"status": "success", "data": {"product_id": 50001}}
        )
        outcome = push_one_bundle(
            bundle_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertTrue(outcome.pushed)
        self.assertEqual(outcome.operation, "create")
        self.assertEqual(outcome.ee_product_id, "50001")
        # Exactly one EE call — Create.
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_CREATE)
        # itemType=1 from the ruleset's conditional (the flow set
        # source_doc.flags.is_bundle_wrapper=True on the wrapper).
        self.assertEqual(payload["itemType"], 1)
        # subProducts built from each component's ee_sku.
        sub_skus = {sp["sku"] for sp in payload.get("subProducts", [])}
        self.assertEqual(sub_skus, {f"{PREFIX}p-c-a", f"{PREFIX}p-c-b"})
        # Map row created for the BUNDLE (not the wrapper Item).
        map_row = frappe.get_doc(
            "EasyEcom Item Map", {"ee_sku": bundle_code}
        )
        self.assertEqual(map_row.erpnext_doctype, "Product Bundle")
        self.assertEqual(map_row.erpnext_name, bundle_code)
        self.assertEqual(map_row.ee_product_id, "50001")

    def test_push_bundle_with_unpushed_component_flags_no_ee_call(self) -> None:
        """Dependency-ordering: component lacks ee_product_id →
        bundle FLAGGED-NOT-PUSHED, no EE call."""
        _make_normal_item(
            f"{PREFIX}dep-c-good", tax_template=self.tax_template,
            ee_product_id="3001",
        )
        # `dep-c-bad` has NO ee_product_id (never pushed).
        _make_normal_item(f"{PREFIX}dep-c-bad", tax_template=self.tax_template)
        bundle_code = f"{PREFIX}bundle-dep"
        _make_product_bundle(
            bundle_code,
            component_codes=[f"{PREFIX}dep-c-good", f"{PREFIX}dep-c-bad"],
        )

        client = MockPushClient()
        outcome = push_one_bundle(
            bundle_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertFalse(outcome.pushed)
        self.assertEqual(outcome.operation, "flagged")
        self.assertEqual(client.calls, [])  # ZERO EE calls
        joined = " ".join(outcome.flag_reasons)
        self.assertIn(f"{PREFIX}dep-c-bad", joined)
        # Bundle's own map row reflects FNC.
        map_row = frappe.get_doc(
            "EasyEcom Item Map", {"ee_sku": bundle_code}
        )
        self.assertEqual(map_row.status, STATUS_FLAGGED_NOT_PUSHED)

    def test_push_bundle_with_one_component_flagged_under_min(self) -> None:
        _make_normal_item(
            f"{PREFIX}tiny-c", tax_template=self.tax_template,
            ee_product_id="4001",
        )
        bundle_code = f"{PREFIX}bundle-tiny"
        _make_product_bundle(
            bundle_code, component_codes=[f"{PREFIX}tiny-c"]
        )
        client = MockPushClient()
        outcome = push_one_bundle(
            bundle_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertEqual(outcome.operation, "flagged")
        self.assertEqual(client.calls, [])
        joined = " ".join(outcome.flag_reasons)
        self.assertIn("at least 2", joined)

    def test_existing_bundle_map_routes_to_update(self) -> None:
        """Bundle already pushed (map row has ee_product_id) → routes
        to UpdateMasterProduct keyed on productId, NOT a second Create."""
        _make_normal_item(
            f"{PREFIX}upd-c-a", tax_template=self.tax_template,
            ee_product_id="5001",
        )
        _make_normal_item(
            f"{PREFIX}upd-c-b", tax_template=self.tax_template,
            ee_product_id="5002",
        )
        bundle_code = f"{PREFIX}bundle-upd"
        _make_product_bundle(
            bundle_code,
            component_codes=[f"{PREFIX}upd-c-a", f"{PREFIX}upd-c-b"],
        )
        wrapper = frappe.get_doc("Item", bundle_code)
        wrapper.append(
            "taxes",
            {"item_tax_template": GST_18_TEMPLATE,
             "minimum_net_rate": 0, "maximum_net_rate": 0},
        )
        wrapper.save(ignore_permissions=True)
        # Pre-existing map row for the Bundle with product_id.
        m = frappe.new_doc("EasyEcom Item Map")
        m.update(
            {
                "ee_sku": bundle_code,
                "erpnext_doctype": "Product Bundle",
                "erpnext_name": bundle_code,
                "ee_product_id": "60001",
                "status": STATUS_MAPPED,
            }
        )
        m.insert(ignore_permissions=True)
        frappe.db.commit()

        client = MockPushClient()
        outcome = push_one_bundle(
            bundle_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertEqual(outcome.operation, "update")
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_UPDATE)
        self.assertEqual(payload["productId"], "60001")
        # Still itemType=1 + subProducts.
        self.assertEqual(payload["itemType"], 1)
        self.assertIn("subProducts", payload)


# ============================================================
# 3. itemType conditional — ruleset reads flags.is_bundle_wrapper
# ============================================================


class TestItemTypeConditional(FrappeTestCase):
    """The ruleset's itemType rule uses
    `source_doc.flags.is_bundle_wrapper` (set by the push flow before
    calling executor.push()). These tests assert both branches at the
    ruleset level so a regression in the conditional is caught here,
    not buried in a push-flow test."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        _ensure_hsn("85171000")
        _ensure_uom("Nos")
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_flag_unset_yields_item_type_zero(self) -> None:
        """Stage 3 contract: when the flow doesn't set the flag (normal
        item path), itemType defaults to 0 because source_doc.flags is
        a frappe._dict that returns None for missing keys."""
        item = _make_normal_item(f"{PREFIX}cond-normal")
        out = self.executor.push(item)
        self.assertEqual(out["itemType"], 0)

    def test_flag_true_yields_item_type_one(self) -> None:
        """Stage 4 contract: when the flow sets the flag (bundle path),
        itemType is 1."""
        item = _make_normal_item(f"{PREFIX}cond-bundle", is_stock_item=0)
        item.flags.is_bundle_wrapper = True
        out = self.executor.push(item)
        self.assertEqual(out["itemType"], 1)


# ============================================================
# 4. No-BOM/no-Kit on push — symmetric with pull
# ============================================================


class TestNoBomNoKitPush(FrappeTestCase):
    """ERPNext BOMs and EE kit_bom never cross. Stage 2 pull already
    FNCs kit_bom (covered there). Push side: there's no path that
    can produce a kit/BOM EE payload — push_one_item handles only
    normal items, push_one_bundle handles only Product Bundles
    (itemType=0 or 1 respectively). itemType=2 (Kit) is not produced
    by any path in this codebase. This test locks the invariant
    structurally so a future itemType change doesn't accidentally
    open the path."""

    def test_no_path_emits_item_type_two(self) -> None:
        # Inspect the push ruleset's itemType rule: must not emit 2.
        rs = frappe.get_doc("EasyEcom Field Mapping", "EasyEcom-Item-Push")
        for r in rs.rules:
            if r.easyecom_path != "itemType":
                continue
            args = frappe.parse_json(r.transform_args or "{}")
            # default is 0 (normal); conditional branches don't include 2.
            self.assertEqual(args.get("default"), 0)
            for cond in args.get("conditions") or []:
                self.assertNotEqual(cond.get("then"), 2)
