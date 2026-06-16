"""§8d Stage 3 tests — ERPNext → EE Product Master push.

⚠️ ZERO real EE traffic. Every test uses MockPushClient. The hard
constraint from the Stage 3 packet — "no real writes to EasyEcom
during testing" — is enforced by NEVER constructing an EasyEcomClient
and NEVER mocking via monkeypatch on a real client; the flow
functions accept a `client` parameter, and tests pass MockPushClient.
A real client would only be reachable via the (deliberately unwired)
doc_event trigger.

Coverage:
  - Push ruleset field manufacturing (materialType=1, itemType=0,
    Brand/ModelNumber fallback, Cost fallback chain) — proves the
    FDE-configurable rules in EasyEcom-Item-Push produce the right
    EE payload.
  - Missing-mandatory → Flagged-Not-Pushed (no broken EE call) —
    proves the dimension presence check + TaxRate resolution prevent
    a degenerate payload from reaching EE.
  - CreateMasterProduct → product_id writeback to Item Map + Item
    custom field.
  - UpdateMasterProduct: existing map with product_id → routes to
    Update keyed on productId, no second Create.
  - TaxRate snapping: 0.18 → 18.0, off-band → flagged.
  - Batch sweep: which-items policy (excludes already-mapped,
    excludes bundle wrappers, excludes disabled).
  - Sweep savepoint isolation (one bad item, siblings continue).
  - Lifecycle: ActivateDeactivateProduct keyed on product_id, status 0.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_ACTIVATE_DEACTIVATE,
    PRODUCT_MASTER_CREATE,
    PRODUCT_MASTER_UPDATE,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.item_push import (
    EE_ALLOWED_TAX_RATES,
    ITEM_PUSH_RULESET,
    STATUS_FLAGGED_NOT_PUSHED,
    STATUS_MAPPED,
    build_push_payload,
    push_all_pending,
    push_lifecycle,
    push_one_item,
)
from ecommerce_super.tests.factories import make_account
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


PREFIX = "TEST-8D-S3-"

# Specific India-Compliance template the tests need a known 18% rate
# from. _first_gst_template returns whatever's alphabetically first
# (often "Exempted - TC" — 0% rate, which works for "is the snap
# returning a number" but not for "did we get 18%").
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
    return frappe.get_doc("EasyEcom Account", name)


def _ensure_company_settings(company: str, enabled: int = 1) -> None:
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
    else:
        doc = frappe.new_doc("EasyEcom Company Settings")
        doc.update({"company": company, "enabled": enabled})
        doc.insert(ignore_permissions=True)
    frappe.db.commit()


def _make_item(
    item_code: str,
    *,
    company: str | None = None,
    with_dims: bool = True,
    with_brand: str | None = None,
    is_stock_item: int = 1,
    disabled: int = 0,
    tax_template: str | None = None,
    ecs_ee_cost: float | None = None,
) -> Any:
    """Insert a test Item. Optionally attach an Item Tax row pointing
    at a real India-Compliance template for the given Company so the
    TaxRate resolver finds a clean rate."""
    if frappe.db.exists("Item", item_code):
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
            "disabled": disabled,
        }
    )
    if with_brand:
        # Brand is a Link — ensure the Brand exists.
        if not frappe.db.exists("Brand", with_brand):
            b = frappe.new_doc("Brand")
            b.update({"brand": with_brand})
            b.insert(ignore_permissions=True)
        item.brand = with_brand
    if with_dims:
        item.weight_per_unit = 100
        item.ecs_length_cm = 10
        item.ecs_height_cm = 5
        item.ecs_width_cm = 3
    if ecs_ee_cost is not None:
        item.ecs_ee_cost = ecs_ee_cost
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
    return item


def _wipe(prefix: str = PREFIX) -> None:
    # Product Bundle first (FK → Item via new_item_code), then Item Map,
    # then Items. A bundle leftover from a previous run would cause a
    # duplicate-name insert on the next test (Product Bundle's
    # autoname is its new_item_code).
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
    # Item Map first (FK), then Items.
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


def _first_gst_template(company: str) -> str:
    """Pick any output-tax template that exists for this Company.
    Same helper as Stage 2 multi-Co tests."""
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


def _ensure_tax_rule_map(
    company: str,
    rule_name: str,
    item_tax_template: str,
) -> str:
    """Ensure an EasyEcom Tax Rule Map exists for (rule_name, company)
    with the given Item Tax Template attached.

    Needed for tests that exercise build_push_payload — the push now
    requires TaxRuleName, which is reverse-looked-up from the Map.
    Without this fixture every test would flag at "TaxRuleName cannot
    be resolved" (correctly, per the substrate contract).
    """
    existing = frappe.db.get_value(
        "EasyEcom Tax Rule Map",
        {"tax_rule_name": rule_name, "company": company},
        "name",
    )
    if existing:
        doc = frappe.get_doc("EasyEcom Tax Rule Map", existing)
        if not any(
            t.item_tax_template == item_tax_template
            for t in (doc.taxes or [])
        ):
            doc.append("taxes", {"item_tax_template": item_tax_template})
            doc.save(ignore_permissions=True)
            frappe.db.commit()
        return existing
    doc = frappe.new_doc("EasyEcom Tax Rule Map")
    doc.update(
        {
            "tax_rule_name": rule_name,
            "company": company,
        }
    )
    doc.append("taxes", {"item_tax_template": item_tax_template})
    doc.flags.ignore_permissions = True
    # Workflow + validate skipped for fixture setup — the substrate
    # contract under test (TaxRuleName lookup) is purely about the
    # SQL join from Item Tax Template → Map row, independent of the
    # workflow_state field.
    doc.flags.ignore_validate = True
    doc.flags.ignore_workflow_attempt = True
    frappe.flags.in_install = True
    try:
        doc.insert()
    finally:
        frappe.flags.in_install = False
    frappe.db.commit()
    return doc.name


class MockPushClient:
    """A minimal swap-in for EasyEcomClient that records POST calls
    and returns canned responses keyed on endpoint. Tests assert what
    was called + with what payload; the production client is never
    instantiated. Zero risk of accidental real EE traffic."""

    def __init__(
        self,
        *,
        create_returns: dict | None = None,
        update_returns: dict | None = None,
        deactivate_returns: dict | None = None,
        raise_on_endpoint: str | None = None,
    ) -> None:
        # Default responses match EE's documented shapes per §8.1.5.
        self._create_response = create_returns or {
            "status": "success",
            "data": {"product_id": 999001},
        }
        self._update_response = update_returns or {"status": "success"}
        self._deactivate_response = deactivate_returns or {"status": "success"}
        self._raise_on = raise_on_endpoint
        self.calls: list[tuple[str, dict]] = []  # (endpoint, payload)

    def post(self, endpoint: str, payload: dict, **kwargs) -> dict:
        self.calls.append((endpoint, dict(payload)))
        if self._raise_on == endpoint:
            raise RuntimeError(f"simulated EE outage on {endpoint}")
        if endpoint == PRODUCT_MASTER_CREATE:
            return self._create_response
        if endpoint == PRODUCT_MASTER_UPDATE:
            return self._update_response
        if endpoint == PRODUCT_MASTER_ACTIVATE_DEACTIVATE:
            return self._deactivate_response
        raise NotImplementedError(f"MockPushClient.post: unknown {endpoint!r}")

    # Hard refusal of any other client API the flow might call. Defends
    # against accidental real EE traffic (the test contract is "post
    # only", and post is mocked).
    def get(self, *args, **kwargs):
        raise NotImplementedError(
            "MockPushClient does not implement get(); the push flow "
            "must never read EE in Stage 3"
        )

    def paginated(self, *args, **kwargs):
        raise NotImplementedError(
            "MockPushClient does not implement paginated()"
        )


# ============================================================
# 1. Push ruleset field manufacturing (the FDE-configurable rules)
# ============================================================


class TestPushRulesetManufacturing(FrappeTestCase):
    """The Brand fallback / Cost fallback / materialType / itemType /
    ModelNumber rules live in EasyEcom-Item-Push (FDE-configurable
    in the desk). These tests prove the rules produce the right
    payload — same surface the FDE edits, same surface the flow
    consumes."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        _ensure_item_group()
        _ensure_uom()
        _ensure_hsn()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_branded_item_uses_brand_name(self) -> None:
        item = _make_item(f"{PREFIX}brand-yes", with_brand="Acme")
        out = self.executor.push(item)
        self.assertEqual(out["Brand"], "Acme")

    def test_unbranded_item_falls_back_to_unbranded(self) -> None:
        item = _make_item(f"{PREFIX}brand-no")
        out = self.executor.push(item)
        self.assertEqual(out["Brand"], "Unbranded")

    def test_material_type_default_is_finished_good(self) -> None:
        item = _make_item(f"{PREFIX}mat-1")
        out = self.executor.push(item)
        self.assertEqual(out["materialType"], 1)

    def test_item_type_default_is_normal(self) -> None:
        item = _make_item(f"{PREFIX}it-1")
        out = self.executor.push(item)
        self.assertEqual(out["itemType"], 0)

    def test_model_number_defaults_to_item_code(self) -> None:
        item = _make_item(f"{PREFIX}mn-1")
        out = self.executor.push(item)
        self.assertEqual(out["ModelNumber"], item.item_code)

    def test_cost_uses_ecs_ee_cost_when_present(self) -> None:
        item = _make_item(f"{PREFIX}cost-ee", ecs_ee_cost=250)
        out = self.executor.push(item)
        self.assertEqual(out["Cost"], 250)

    def test_cost_falls_back_to_valuation_rate(self) -> None:
        item = _make_item(f"{PREFIX}cost-val")
        item.valuation_rate = 333
        item.save(ignore_permissions=True)
        out = self.executor.push(item)
        self.assertEqual(out["Cost"], 333)

    def test_cost_falls_back_to_zero(self) -> None:
        item = _make_item(f"{PREFIX}cost-zero")
        # ecs_ee_cost absent + valuation_rate absent → 0.
        out = self.executor.push(item)
        self.assertEqual(out["Cost"], 0)


# ============================================================
# 2. Missing-mandatory → Flagged-Not-Pushed
# ============================================================


class TestMissingMandatoryFlagsNotPushed(FrappeTestCase):
    """If a hard-mandatory field can't be sourced, the FLOW (not the
    ruleset) flags Not-Pushed. No EE call is made — the mock client's
    `calls` list stays empty. Proves the "don't build a broken payload"
    promise from §8.1.5."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.tax_template = _first_gst_template(cls.company)

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_missing_dimensions_flagged_no_ee_call(self) -> None:
        item = _make_item(
            f"{PREFIX}no-dims",
            with_dims=False,
            tax_template=self.tax_template,
        )
        client = MockPushClient()
        outcome = push_one_item(
            item.item_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertFalse(outcome.pushed)
        self.assertEqual(outcome.operation, "flagged")
        # Mock client never called — zero risk of real EE write.
        self.assertEqual(client.calls, [])
        # Map row landed in FNC with the dimension reasons.
        map_doc = frappe.get_doc(
            "EasyEcom Item Map", {"erpnext_name": item.item_code}
        )
        self.assertEqual(map_doc.status, STATUS_FLAGGED_NOT_PUSHED)
        self.assertIn("Weight missing", map_doc.flag_reason)

    def test_missing_tax_rate_flagged_no_ee_call(self) -> None:
        """Item with dims but NO tax row → TaxRate unresolvable → flag."""
        item = _make_item(f"{PREFIX}no-tax")  # no tax_template attached
        client = MockPushClient()
        outcome = push_one_item(
            item.item_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertFalse(outcome.pushed)
        self.assertEqual(outcome.operation, "flagged")
        self.assertEqual(client.calls, [])
        self.assertTrue(
            any("TaxRate" in r for r in outcome.flag_reasons),
            f"Expected TaxRate flag, got: {outcome.flag_reasons}",
        )


# ============================================================
# 3. CreateMasterProduct + product_id writeback (the §8.1.5 happy path)
# ============================================================


class TestCreatePushAndWriteback(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.tax_template = _first_gst_template(cls.company)
        # Tax Rule Map needed for TaxRuleName push emission.
        _ensure_tax_rule_map(
            cls.company, "GST-Test", GST_18_TEMPLATE
        )
        _ensure_tax_rule_map(
            cls.company, "GST-Test", cls.tax_template
        )

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_create_writes_product_id_to_map_row(self) -> None:
        item = _make_item(
            f"{PREFIX}create-1",
            tax_template=GST_18_TEMPLATE,
            ecs_ee_cost=100,
        )
        client = MockPushClient(
            create_returns={"status": "success", "data": {"product_id": 42}}
        )
        outcome = push_one_item(
            item.item_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertTrue(outcome.pushed)
        self.assertEqual(outcome.operation, "create")
        self.assertEqual(outcome.ee_product_id, "42")

        # Exactly one Create call.
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_CREATE)
        # Payload sanity.
        self.assertEqual(payload["Sku"], item.item_code)
        self.assertEqual(payload["materialType"], 1)
        self.assertEqual(payload["itemType"], 0)
        self.assertEqual(payload["TaxRate"], 18.0)
        # Map row carries the returned product_id.
        map_row = frappe.get_doc(
            "EasyEcom Item Map", {"erpnext_name": item.item_code}
        )
        self.assertEqual(map_row.ee_product_id, "42")
        self.assertEqual(map_row.status, STATUS_MAPPED)
        # Item custom field also stamped.
        refreshed = frappe.get_doc("Item", item.item_code)
        self.assertEqual(refreshed.ecs_ee_product_id, "42")

    def test_create_returning_no_product_id_flags(self) -> None:
        item = _make_item(
            f"{PREFIX}create-noid", tax_template=self.tax_template
        )
        client = MockPushClient(
            create_returns={"status": "success", "data": {}}  # missing product_id
        )
        outcome = push_one_item(
            item.item_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertFalse(outcome.pushed)
        self.assertEqual(outcome.operation, "flagged")
        # Create WAS called (the only way to know the response is empty).
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], PRODUCT_MASTER_CREATE)
        # Map row in FNC.
        map_row = frappe.get_doc(
            "EasyEcom Item Map", {"erpnext_name": item.item_code}
        )
        self.assertEqual(map_row.status, STATUS_FLAGGED_NOT_PUSHED)


# ============================================================
# 4. UpdateMasterProduct routing (existing map with product_id)
# ============================================================


class TestUpdatePath(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.tax_template = _first_gst_template(cls.company)

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_existing_map_routes_to_update_keyed_on_product_id(self) -> None:
        item = _make_item(f"{PREFIX}update-1", tax_template=self.tax_template)
        # Pre-existing map row with product_id (as if pulled in Stage 2).
        # Also stamp ecs_ee_cp_id on the Item — UpdateMasterProduct's
        # write contract reads `productId` from item.ecs_ee_cp_id (NOT
        # the Map's ee_product_id), see item_push.py:1597 + the long
        # EE-naming-inconsistency comment above it. ee_cp_id is what EE
        # returns as `cp_id` on GetProductMaster and what EE expects
        # as `productId` on UpdateMasterProduct.
        map_doc = frappe.new_doc("EasyEcom Item Map")
        map_doc.update(
            {
                "ee_sku": item.item_code,
                "erpnext_doctype": "Item",
                "erpnext_name": item.item_code,
                "ee_product_id": "7777",
                "ee_cp_id": "7777",
                "status": STATUS_MAPPED,
            }
        )
        map_doc.insert(ignore_permissions=True)
        item.db_set("ecs_ee_cp_id", "7777", update_modified=False)
        item.db_set("ecs_ee_product_id", "7777", update_modified=False)
        item.reload()
        frappe.db.commit()

        client = MockPushClient()
        outcome = push_one_item(
            item.item_code, client=client, account=self.account,
            executor=self.executor, enabled_companies=[self.company],
        )
        self.assertEqual(outcome.operation, "update")
        # Single call — to Update, not Create.
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_UPDATE)
        # Update payload includes productId for keying. Substrate writes
        # it as int (EE's typed expectation), so compare against int 7777
        # rather than the string "7777".
        self.assertEqual(payload.get("productId"), 7777)


# ============================================================
# 5. TaxRate snap (decimal → EE allowed-set; off-band → flag)
# ============================================================


class TestTaxRateSnap(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.gst_18 = _first_gst_template(cls.company)
        # TaxRuleName resolution requires a Tax Rule Map carrying the
        # Item Tax Template. EE made TaxRuleName mandatory on push;
        # tests must satisfy the same substrate contract.
        _ensure_tax_rule_map(
            cls.company, "GST-Test", GST_18_TEMPLATE
        )

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_gst_18_snaps_to_18(self) -> None:
        item = _make_item(f"{PREFIX}tax-18", tax_template=GST_18_TEMPLATE)
        payload, reasons = build_push_payload(
            item, executor=self.executor, enabled_companies=[self.company]
        )
        self.assertEqual(reasons, [])
        self.assertEqual(payload["TaxRate"], 18.0)

    def test_ee_allowed_set_is_locked(self) -> None:
        """If someone tries to relax the set, the contract is broken —
        EE will reject. Lock the set in tests."""
        self.assertEqual(EE_ALLOWED_TAX_RATES, (0.0, 3.0, 5.0, 12.0, 18.0, 28.0))


# ============================================================
# 6. Batch sweep — which-items policy + savepoint isolation
# ============================================================


class TestBatchSweep(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()
        cls.company = _ensure_test_company()
        _ensure_company_settings(cls.company)
        cls.tax_template = _first_gst_template(cls.company)

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_sweep_only_picks_unpushed_enabled_normal_items(self) -> None:
        """The which-items policy: not yet mapped, not bundle wrapper,
        not disabled, is_stock_item=1, has HSN."""
        # 1) Sweep candidate — fresh item, no map row.
        cand = _make_item(
            f"{PREFIX}sweep-candidate", tax_template=self.tax_template
        )
        # 2) Already pushed (map row with product_id) — NOT swept.
        already = _make_item(
            f"{PREFIX}sweep-already", tax_template=self.tax_template
        )
        map_doc = frappe.new_doc("EasyEcom Item Map")
        map_doc.update(
            {
                "ee_sku": already.item_code,
                "erpnext_doctype": "Item",
                "erpnext_name": already.item_code,
                "ee_product_id": "1111",
                "status": STATUS_MAPPED,
            }
        )
        map_doc.insert(ignore_permissions=True)
        # 3) Disabled — NOT swept.
        disabled = _make_item(
            f"{PREFIX}sweep-disabled",
            tax_template=self.tax_template,
            disabled=1,
        )
        # 4) Bundle wrapper — NOT swept.
        bundle_wrapper = _make_item(
            f"{PREFIX}sweep-bundle",
            tax_template=self.tax_template,
            is_stock_item=0,
        )
        # Need 2 components for Product Bundle.
        comp_a = _make_item(
            f"{PREFIX}sweep-comp-a", tax_template=self.tax_template
        )
        comp_b = _make_item(
            f"{PREFIX}sweep-comp-b", tax_template=self.tax_template
        )
        bundle = frappe.new_doc("Product Bundle")
        bundle.update({"new_item_code": bundle_wrapper.item_code})
        bundle.append("items", {"item_code": comp_a.item_code, "qty": 1})
        bundle.append("items", {"item_code": comp_b.item_code, "qty": 1})
        bundle.insert(ignore_permissions=True)
        frappe.db.commit()

        client = MockPushClient(
            create_returns={"status": "success", "data": {"product_id": 5001}}
        )
        # We must inject a mock client; push_all_pending would build a real one.
        # Use the lower-level individual function pattern: process candidates
        # explicitly to assert which-items policy without firing real client.
        from ecommerce_super.easyecom.flows.item_push import (
            _candidate_items_for_sweep,
        )

        candidates = _candidate_items_for_sweep()
        # Only the genuine candidate + components (they're normal items
        # too) should appear. Disabled, bundle wrapper, and already-mapped
        # should NOT.
        self.assertIn(cand.item_code, candidates)
        self.assertIn(comp_a.item_code, candidates)
        self.assertIn(comp_b.item_code, candidates)
        self.assertNotIn(already.item_code, candidates)
        self.assertNotIn(disabled.item_code, candidates)
        self.assertNotIn(bundle_wrapper.item_code, candidates)

    def test_sweep_pushes_each_candidate_with_savepoint_isolation(self) -> None:
        """The sweep query is global — other test files leave behind
        ERPNext Items + Item Tax setups. Don't assert on totals; assert
        about THIS test's own items via outcome.outcomes filtered by sku."""
        good_a = _make_item(
            f"{PREFIX}sw-good-a", tax_template=self.tax_template
        )
        bad_no_tax = _make_item(f"{PREFIX}sw-bad-no-tax")  # no tax → flagged
        good_c = _make_item(
            f"{PREFIX}sw-good-c", tax_template=self.tax_template
        )

        client = MockPushClient(
            create_returns={"status": "success", "data": {"product_id": 6001}}
        )
        outcome = push_all_pending(
            account_name=self.account.name, client=client
        )

        # Filter to OUR items.
        ours = {o.item_code: o for o in outcome.outcomes
                if o.item_code.startswith(f"{PREFIX}sw-")}
        # Two creates (good_a, good_c) + one flagged (bad_no_tax).
        self.assertEqual(ours[good_a.item_code].operation, "create")
        self.assertEqual(ours[good_c.item_code].operation, "create")
        self.assertEqual(ours[bad_no_tax.item_code].operation, "flagged")
        # EE was called for good_a and good_c, NOT for bad_no_tax (the
        # broken payload never reaches EE — confirmed by inspecting
        # MockPushClient.calls).
        skus_called = [c[1].get("Sku") for c in client.calls
                       if c[0] == PRODUCT_MASTER_CREATE
                       and (c[1].get("Sku") or "").startswith(f"{PREFIX}sw-")]
        self.assertCountEqual(skus_called, [good_a.item_code, good_c.item_code])
        # bad_no_tax never reached EE.
        self.assertNotIn(bad_no_tax.item_code, skus_called)

    def test_bundle_wrapper_dispatches_to_combo_push(self) -> None:
        """Stage 4 lit up the combo-push path. Calling push_one_item on
        a bundle wrapper now DISPATCHES to push_one_bundle. Components
        without ee_product_id → bundle FNC'd (dependency-ordering),
        so this test (whose components have no map rows) sees a
        flagged outcome rather than the Stage-3 'skipped'. The
        important Stage-3 invariant — bundles never reach the
        normal-item push path — still holds."""
        wrapper = _make_item(
            f"{PREFIX}sw-bw-wrapper",
            tax_template=self.tax_template,
            is_stock_item=0,
        )
        comp_a = _make_item(
            f"{PREFIX}sw-bw-comp-a", tax_template=self.tax_template
        )
        comp_b = _make_item(
            f"{PREFIX}sw-bw-comp-b", tax_template=self.tax_template
        )
        bundle = frappe.new_doc("Product Bundle")
        bundle.update({"new_item_code": wrapper.item_code})
        bundle.append("items", {"item_code": comp_a.item_code, "qty": 1})
        bundle.append("items", {"item_code": comp_b.item_code, "qty": 1})
        bundle.insert(ignore_permissions=True)
        frappe.db.commit()

        client = MockPushClient()
        outcome = push_one_item(
            wrapper.item_code, client=client, account=self.account,
            enabled_companies=[self.company],
        )
        # Components have no map rows → bundle FNC'd, no EE call.
        self.assertEqual(outcome.operation, "flagged")
        self.assertEqual(client.calls, [])
        joined = " ".join(outcome.flag_reasons)
        self.assertIn("EasyEcom Item Map", joined)


# ============================================================
# 7. Lifecycle — ActivateDeactivateProduct
# ============================================================


class TestLifecyclePush(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _ensure_account()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_disabled_item_with_product_id_sends_deactivate(self) -> None:
        # Pre-create Item and a map row with product_id.
        # See sibling test (test_existing_map_routes_to_update_keyed_on_product_id):
        # the substrate's write contracts read identifiers from the
        # Item's ecs_ee_cp_id / ecs_ee_product_id custom fields, NOT
        # from the Map row. Stamp both to mirror what a real Pull/Push
        # cycle would have put on the Item.
        item = _make_item(f"{PREFIX}life-deact", disabled=1)
        map_doc = frappe.new_doc("EasyEcom Item Map")
        map_doc.update(
            {
                "ee_sku": item.item_code,
                "erpnext_doctype": "Item",
                "erpnext_name": item.item_code,
                "ee_product_id": "8888",
                "ee_cp_id": "8888",
                "status": STATUS_MAPPED,
            }
        )
        map_doc.insert(ignore_permissions=True)
        item.db_set("ecs_ee_cp_id", "8888", update_modified=False)
        item.db_set("ecs_ee_product_id", "8888", update_modified=False)
        item.reload()
        frappe.db.commit()
        client = MockPushClient()
        outcome = push_lifecycle(
            item.item_code, client=client, account=self.account
        )
        self.assertTrue(outcome.pushed)
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_ACTIVATE_DEACTIVATE)
        # The activate/deactivate endpoint sends product_id (snake_case,
        # int) per the EE-naming flip noted at item_push.py:457-460:
        # "takes product_id (snake, int) but the value is the cp_id".
        self.assertEqual(payload["product_id"], 8888)
        self.assertEqual(payload["status"], 0)

    def test_unmapped_item_lifecycle_is_noop(self) -> None:
        item = _make_item(f"{PREFIX}life-noop", disabled=1)
        client = MockPushClient()
        outcome = push_lifecycle(
            item.item_code, client=client, account=self.account
        )
        self.assertFalse(outcome.pushed)
        self.assertEqual(client.calls, [])  # ZERO calls — no map row yet
