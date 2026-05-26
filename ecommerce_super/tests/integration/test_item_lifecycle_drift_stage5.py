"""§8d Stage 5 tests — phase-governed lifecycle + flip + drift detection.

⚠️ ZERO real EE traffic. Pull paths use the in-memory product
payload (no HTTP); push lifecycle uses MockClient. The production
EasyEcomClient is instantiated zero times.

Coverage per the Stage 5 packet:
  - Pull side lifecycle (already shipped in Stage 2): EE active:0 →
    Item.disabled=1 IN ONBOARDING MODE.
  - Push lifecycle: ERPNext disabled → EE ActivateDeactivate(status=0);
    re-enabled → status=1. Mocked.
  - Phase-governed direction:
      onboarding: bidirectional. Pull's active flag flips Item.disabled.
      erpnext_mastered: ERPNext is SoT. Pull does NOT mutate; EE-side
        deactivation surfaces as drift.
  - Flip changes pull behavior: post-flip re-pull of a changed mapped
    item → Drift status, NOT overwritten.
  - Flip stays one-way / refuses re-flip (Stage 1 contract still in
    force; re-verified here).
  - Drift records WHAT differs in the map row's flag_reason.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.item_master_mode import (
    flip_to_erpnext_mastered,
)
from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_ACTIVATE_DEACTIVATE,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.item_pull import (
    ITEM_PULL_RULESET,
    MODE_ERPNEXT_MASTERED,
    MODE_ONBOARDING,
    STATUS_DRIFT,
    STATUS_MAPPED,
    process_one_product,
)
from ecommerce_super.easyecom.flows.item_push import push_lifecycle
from ecommerce_super.tests.factories import make_account


PREFIX = "TEST-8D-S5-"


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


def _account_in_mode(mode: str) -> Any:
    """Ensure the account exists and is in the requested
    item_master_mode. Avoids the flip endpoint (which is one-way) by
    db.set_value — tests need to flop modes."""
    name = make_account(name=f"{PREFIX}acct".lower())
    _ensure_uom()
    _ensure_item_group()
    frappe.db.set_value(
        "EasyEcom Account",
        name,
        {
            "default_uom": "Nos",
            "item_master_mode": mode,
            "item_master_flipped_at": (
                frappe.utils.now_datetime() if mode == MODE_ERPNEXT_MASTERED
                else None
            ),
        },
        update_modified=False,
    )
    frappe.db.commit()
    return frappe.get_doc("EasyEcom Account", name)


def _seed_mapped_item(item_code: str, **field_overrides: Any) -> Any:
    """Create an Item + an existing EasyEcom Item Map row pointing at
    it (the 'already mapped from a prior pull/push' state). Field
    overrides go onto the Item before insert."""
    if frappe.db.exists("Item", item_code):
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
            "is_stock_item": 1,
            "weight_per_unit": 100,
            "ecs_length_cm": 10,
            "ecs_height_cm": 5,
            "ecs_width_cm": 3,
            "standard_rate": 200,
            "ecs_ee_cost": 100,
            "ecs_ee_mrp": 200,
        }
    )
    item.update(field_overrides)
    item.insert(ignore_permissions=True)
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
    return item


def _ee_payload(item_code: str, **overrides: Any) -> dict:
    """A canonical EE GetProductMaster payload that matches the seed
    item byte-for-byte. Overrides simulate EE-side edits."""
    base = {
        "sku": item_code,
        "product_type": "normal_product",
        "product_name": item_code,
        "description": None,
        "hsn_code": "85171000",
        "accounting_unit": "Nos",
        "active": 1,
        "product_id": int(hash(item_code) % 1_000_000),
        "cp_id": int((hash(item_code) + 1) % 1_000_000),
        "weight": 100,
        "height": 5,
        "length": 10,
        "width": 3,
        "cost": 100,
        "mrp": 200,
    }
    base.update(overrides)
    return base


def _wipe(prefix: str = PREFIX) -> None:
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


class MockLifecycleClient:
    """A minimal swap-in for EasyEcomClient that records
    ActivateDeactivateProduct calls. Other endpoints raise so
    accidental real-EE attempts crash the test immediately."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, endpoint: str, payload: dict, **kwargs) -> dict:
        self.calls.append((endpoint, dict(payload)))
        if endpoint == PRODUCT_MASTER_ACTIVATE_DEACTIVATE:
            return {"status": "success"}
        raise NotImplementedError(f"unexpected endpoint {endpoint!r}")


# ============================================================
# 1. Phase-governed pull lifecycle
# ============================================================


class TestPhaseGovernedPullLifecycle(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_onboarding_pull_active_zero_disables_item(self) -> None:
        """Stage 2 / §8.1.7 onboarding: EE active:0 flips Item.disabled.
        This is the original Stage-2 contract; re-verified for Stage 5
        to make explicit that mode=onboarding still does this."""
        account = _account_in_mode(MODE_ONBOARDING)
        _seed_mapped_item(f"{PREFIX}lp-on-1")
        payload = _ee_payload(f"{PREFIX}lp-on-1", active=0)
        out = process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        # Item is now disabled in ERPNext.
        item = frappe.get_doc("Item", f"{PREFIX}lp-on-1")
        self.assertEqual(item.disabled, 1)
        # The map row is NOT in Drift — onboarding pull accepts.
        self.assertNotEqual(out.status, STATUS_DRIFT)

    def test_erpnext_mastered_pull_active_zero_is_drift_not_mutation(self) -> None:
        """Steady state: EE active:0 must NOT flip ERPNext disabled.
        It surfaces as a 'disabled' diff in the drift reason; the
        ERPNext Item stays as it was."""
        account = _account_in_mode(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}lp-mst-1")  # ERPNext disabled=0
        payload = _ee_payload(f"{PREFIX}lp-mst-1", active=0)
        out = process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        # ERPNext side IS NOT mutated.
        item = frappe.get_doc("Item", f"{PREFIX}lp-mst-1")
        self.assertEqual(item.disabled, 0)
        # Map row carries the lifecycle drift.
        self.assertEqual(out.status, STATUS_DRIFT)
        joined = " ".join(out.flag_reasons)
        self.assertIn("disabled", joined)
        # And the map row in the DB matches.
        map_row = frappe.get_doc(
            "EasyEcom Item Map", {"ee_sku": f"{PREFIX}lp-mst-1"}
        )
        self.assertEqual(map_row.status, STATUS_DRIFT)


# ============================================================
# 2. Push lifecycle (status 0/1) — Mocked
# ============================================================


class TestPushLifecycle(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.account = _account_in_mode(MODE_ONBOARDING)

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_erpnext_disable_sends_deactivate_status_zero(self) -> None:
        item = _seed_mapped_item(f"{PREFIX}ld-1", disabled=1)
        client = MockLifecycleClient()
        outcome = push_lifecycle(
            item.item_code, client=client, account=self.account
        )
        self.assertTrue(outcome.pushed)
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_ACTIVATE_DEACTIVATE)
        self.assertEqual(payload["status"], 0)
        self.assertEqual(payload["product_id"], f"PID-{item.item_code}")

    def test_erpnext_enable_sends_activate_status_one(self) -> None:
        item = _seed_mapped_item(f"{PREFIX}le-1", disabled=0)
        client = MockLifecycleClient()
        outcome = push_lifecycle(
            item.item_code, client=client, account=self.account
        )
        self.assertTrue(outcome.pushed)
        self.assertEqual(len(client.calls), 1)
        endpoint, payload = client.calls[0]
        self.assertEqual(endpoint, PRODUCT_MASTER_ACTIVATE_DEACTIVATE)
        self.assertEqual(payload["status"], 1)


# ============================================================
# 3. Drift detection — EE-side edits in steady state
# ============================================================


class TestDriftDetectionPostFlip(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

    def setUp(self) -> None:
        _wipe()

    def tearDown(self) -> None:
        _wipe()

    def test_post_flip_changed_field_is_drift_not_overwrite(self) -> None:
        """Mapped item exists with item_name='Widget'. EE sends a
        re-pull with product_name='Widget Renamed'. In erpnext_mastered
        mode this must: (a) NOT overwrite Item.item_name; (b) set the
        map row to Drift; (c) record the diff in flag_reason."""
        account = _account_in_mode(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}drift-name", item_name="Widget")
        payload = _ee_payload(
            f"{PREFIX}drift-name", product_name="Widget Renamed"
        )
        out = process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        # Item not mutated.
        item = frappe.get_doc("Item", f"{PREFIX}drift-name")
        self.assertEqual(item.item_name, "Widget")
        # Map row flipped to Drift with the field diff.
        self.assertEqual(out.status, STATUS_DRIFT)
        joined = " ".join(out.flag_reasons)
        self.assertIn("item_name", joined)
        self.assertIn("Widget Renamed", joined)
        self.assertIn("Widget", joined)

    def test_post_flip_new_ee_product_is_drift_not_created(self) -> None:
        """EE invents a new SKU post-flip. ERPNext is SoT in steady
        state — don't create the Item; flag it as Drift so the FDE
        sees the EE-origin novelty."""
        account = _account_in_mode(MODE_ERPNEXT_MASTERED)
        new_sku = f"{PREFIX}drift-new"
        payload = _ee_payload(new_sku)
        # No seed — the SKU is fresh on the EE side.
        out = process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        # No Item created.
        self.assertFalse(frappe.db.exists("Item", new_sku))
        # Map row landed in Drift.
        self.assertEqual(out.status, STATUS_DRIFT)
        map_row = frappe.get_doc("EasyEcom Item Map", {"ee_sku": new_sku})
        self.assertEqual(map_row.status, STATUS_DRIFT)
        self.assertIn("EE-origin new product", map_row.flag_reason)

    def test_post_flip_unchanged_payload_no_drift(self) -> None:
        """A 'quiet' re-pull (payload byte-equals the existing Item)
        must NOT flap the map row into Drift."""
        account = _account_in_mode(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}drift-quiet")
        payload = _ee_payload(f"{PREFIX}drift-quiet")
        out = process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        # No drift — map row keeps its existing status.
        self.assertNotEqual(out.status, STATUS_DRIFT)
        map_row = frappe.get_doc(
            "EasyEcom Item Map", {"ee_sku": f"{PREFIX}drift-quiet"}
        )
        self.assertEqual(map_row.status, STATUS_MAPPED)

    def test_post_flip_multiple_field_changes_listed_in_reason(self) -> None:
        """All differing fields show up in flag_reason — not just the
        first one. The FDE needs the full set."""
        account = _account_in_mode(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(
            f"{PREFIX}drift-multi", item_name="Original", standard_rate=200
        )
        payload = _ee_payload(
            f"{PREFIX}drift-multi",
            product_name="Renamed",
            mrp=999,  # standard_rate would diff
            weight=500,  # weight_per_unit would diff
        )
        out = process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        self.assertEqual(out.status, STATUS_DRIFT)
        joined = " ".join(out.flag_reasons)
        self.assertIn("item_name", joined)
        self.assertIn("standard_rate", joined)
        self.assertIn("weight_per_unit", joined)


# ============================================================
# 4. Flip contract still in force (Stage 1 invariants)
# ============================================================


class TestFlipContractStillOneWay(FrappeTestCase):

    ACCOUNT_NAME = f"{PREFIX}flip-onceacct".lower()

    def setUp(self) -> None:
        _wipe()
        if not frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            make_account(name=self.ACCOUNT_NAME)
        frappe.db.set_value(
            "EasyEcom Account", self.ACCOUNT_NAME,
            {
                "item_master_mode": MODE_ONBOARDING,
                "item_master_flipped_at": None,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        if frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            try:
                frappe.delete_doc(
                    "EasyEcom Account", self.ACCOUNT_NAME,
                    force=True, ignore_permissions=True,
                )
            except Exception:
                pass
            frappe.db.commit()
        _wipe()

    def test_flip_remains_one_way_refuses_re_flip(self) -> None:
        """Stage 1 invariant re-verified: a second flip after a
        successful flip returns ok=False, message contains 'already'."""
        first = flip_to_erpnext_mastered(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertTrue(first["ok"])
        second = flip_to_erpnext_mastered(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertFalse(second["ok"])
        self.assertIn("already", second["message"].lower())

    def test_flip_explicitly_changes_pull_behavior(self) -> None:
        """The very point of Stage 5: BEFORE flip the pull accepts
        EE changes; AFTER flip the same pull flags them as drift."""
        executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

        # Make a dedicated account so we don't poison other tests'
        # mode state.
        acct_name = make_account(name=f"{PREFIX}flip-pull")
        frappe.db.set_value(
            "EasyEcom Account", acct_name,
            {
                "default_uom": "Nos",
                "item_master_mode": MODE_ONBOARDING,
                "item_master_flipped_at": None,
            },
            update_modified=False,
        )
        frappe.db.commit()
        account = frappe.get_doc("EasyEcom Account", acct_name)

        # Onboarding pull — accepts and mutates.
        _seed_mapped_item(f"{PREFIX}flip-i", item_name="Original")
        payload = _ee_payload(f"{PREFIX}flip-i", product_name="From EE")
        out_pre = process_one_product(
            payload, account=account, executor=executor,
            enabled_companies=[],
        )
        # Item WAS updated.
        item_pre = frappe.get_doc("Item", f"{PREFIX}flip-i")
        self.assertEqual(item_pre.item_name, "From EE")
        self.assertNotEqual(out_pre.status, STATUS_DRIFT)

        # Now FLIP via the real endpoint.
        flip_result = flip_to_erpnext_mastered(
            account=acct_name, confirm=True
        )
        self.assertTrue(flip_result["ok"])
        # Re-fetch the account (the flip wrote new fields).
        account = frappe.get_doc("EasyEcom Account", acct_name)
        self.assertEqual(account.item_master_mode, MODE_ERPNEXT_MASTERED)

        # SAME pull — now drift, no mutation.
        payload2 = _ee_payload(
            f"{PREFIX}flip-i", product_name="Edited Post Flip"
        )
        out_post = process_one_product(
            payload2, account=account, executor=executor,
            enabled_companies=[],
        )
        item_post = frappe.get_doc("Item", f"{PREFIX}flip-i")
        # Item NOT mutated by the post-flip pull.
        self.assertEqual(item_post.item_name, "From EE")
        # Map row flipped to Drift.
        self.assertEqual(out_post.status, STATUS_DRIFT)
        joined = " ".join(out_post.flag_reasons)
        self.assertIn("Edited Post Flip", joined)

        # Cleanup the dedicated account.
        try:
            frappe.delete_doc(
                "EasyEcom Account", acct_name, force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
        frappe.db.commit()
