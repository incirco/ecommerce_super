"""§8d audit follow-up tests — Sync Record writes, facade switch,
sweep enqueue, drift child table, drift resolution, field exclusion,
single-account guard.

⚠️ ZERO real EE traffic. Push paths use MockPushClient; the facade-
enqueue paths assert via spy on enqueue_easyecom_job.

Each test asserts ONE thing the audit follow-up promised. Read the
test name → know what's being verified.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

import ecommerce_super.easyecom.flows.item_push as item_push
from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_ACTIVATE_DEACTIVATE,
    PRODUCT_MASTER_CREATE,
    PRODUCT_MASTER_UPDATE,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows.item_pull import (
    ITEM_PULL_RULESET,
    MODE_ERPNEXT_MASTERED,
    MODE_ONBOARDING,
    STATUS_DRIFT,
    STATUS_MAPPED,
    STATUS_CREATED_FLAGGED,
    dismiss_drift,
    mark_mapped_override,
    process_one_product,
    scheduled_discover_products,
)
from ecommerce_super.easyecom.flows.item_push import (
    enqueue_push_all_pending,
    push_all_pending_products,
    push_lifecycle,
    push_one_item,
)
from ecommerce_super.tests.factories import make_account
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)

PREFIX = "TEST-8D-AUD-"
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


def _account(mode: str = MODE_ONBOARDING) -> Any:
    name = make_account(name=f"{PREFIX}acct".lower())
    frappe.db.set_value(
        "EasyEcom Account", name,
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


def _seed_mapped_item(item_code: str, **overrides) -> Any:
    if frappe.db.exists("Item", item_code):
        for n in frappe.db.get_all(
            "EasyEcom Item Map", filters={"erpnext_name": item_code}, pluck="name"
        ):
            frappe.delete_doc("EasyEcom Item Map", n, force=True, ignore_permissions=True)
        frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
    item = frappe.new_doc("Item")
    item.update({
        "item_code": item_code, "item_name": item_code,
        "item_group": _ensure_item_group(), "stock_uom": _ensure_uom(),
        "gst_hsn_code": _ensure_hsn(), "is_stock_item": 1,
        "weight_per_unit": 100, "ecs_length_cm": 10,
        "ecs_height_cm": 5, "ecs_width_cm": 3,
        "standard_rate": 200, "ecs_ee_cost": 100, "ecs_ee_mrp": 200,
    })
    item.update(overrides)
    item.insert(ignore_permissions=True)
    m = frappe.new_doc("EasyEcom Item Map")
    m.update({
        "ee_sku": item_code, "erpnext_doctype": "Item",
        "erpnext_name": item_code, "ee_product_id": f"PID-{item_code}",
        "status": STATUS_MAPPED,
    })
    m.insert(ignore_permissions=True)
    return item


def _payload(item_code: str, **overrides) -> dict:
    base = {
        "sku": item_code, "product_type": "normal_product",
        "product_name": item_code, "hsn_code": "85171000",
        "accounting_unit": "Nos", "active": 1,
        "product_id": int(hash(item_code) % 1_000_000),
        "cp_id": int((hash(item_code) + 1) % 1_000_000),
        "weight": 100, "height": 5, "length": 10, "width": 3,
        "cost": 100, "mrp": 200,
    }
    base.update(overrides)
    return base


def _wipe() -> None:
    # Drop test-prefixed Accounts so single-account guard tests don't
    # collide across runs.
    for n in frappe.db.get_all("EasyEcom Account",
        filters={"name": ("like", f"{PREFIX}%")}, pluck="name"):
        try: frappe.delete_doc("EasyEcom Account", n, force=True, ignore_permissions=True)
        except Exception: pass
    for n in frappe.db.get_all("EasyEcom Sync Record",
        filters={"entity_name": ("like", f"{PREFIX}%")}, pluck="name"):
        try: frappe.delete_doc("EasyEcom Sync Record", n, force=True, ignore_permissions=True)
        except Exception: pass
    for n in frappe.db.get_all("EasyEcom Queue Job",
        filters={"target_name": ("like", f"{PREFIX}%")}, pluck="name"):
        try: frappe.delete_doc("EasyEcom Queue Job", n, force=True, ignore_permissions=True)
        except Exception: pass
    for n in frappe.db.get_all("EasyEcom Item Map",
        filters={"ee_sku": ("like", f"{PREFIX}%")}, pluck="name"):
        try: frappe.delete_doc("EasyEcom Item Map", n, force=True, ignore_permissions=True)
        except Exception: pass
    for n in frappe.db.get_all("EasyEcom Item Map",
        filters={"erpnext_name": ("like", f"{PREFIX}%")}, pluck="name"):
        try: frappe.delete_doc("EasyEcom Item Map", n, force=True, ignore_permissions=True)
        except Exception: pass
    for n in frappe.db.get_all("Item",
        filters={"item_code": ("like", f"{PREFIX}%")}, pluck="name"):
        try: frappe.delete_doc("Item", n, force=True, ignore_permissions=True)
        except Exception: pass
    frappe.db.commit()


class MockClient:
    def __init__(self, *, create_product_id: int = 999001) -> None:
        self._create_product_id = create_product_id
        self.calls: list[tuple[str, dict]] = []

    def post(self, endpoint: str, payload: dict, **kwargs) -> dict:
        self.calls.append((endpoint, dict(payload)))
        if endpoint == PRODUCT_MASTER_CREATE:
            return {"status": "success", "data": {"product_id": self._create_product_id}}
        return {"status": "success"}

    def get(self, *a, **k):
        raise NotImplementedError


@contextmanager
def _spy_on_enqueue():
    calls: list[tuple[str, str]] = []
    original = item_push.enqueue_item_push

    def _spy(item_code: str, *, account_name: str) -> str:
        calls.append((item_code, account_name))
        return f"QJ-MOCK-{item_code}"

    item_push.enqueue_item_push = _spy
    try:
        yield calls
    finally:
        item_push.enqueue_item_push = original


def _last_sync_record(item_code: str, direction: str) -> Any | None:
    name = frappe.db.get_value(
        "EasyEcom Sync Record",
        {"entity_name": item_code, "direction": direction},
        "name",
        order_by="modified desc",
    )
    if not name:
        return None
    return frappe.get_doc("EasyEcom Sync Record", name)


# ============================================================
# Audit #1 — Sync Record writes at all 5 op points
# ============================================================


class TestSyncRecordWrites(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        cls.company = _ensure_test_company()
        _ensure_hsn(); _ensure_uom(); _ensure_item_group()

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_pull_success_writes_success_sync_record(self) -> None:
        account = _account(MODE_ONBOARDING)
        process_one_product(
            _payload(f"{PREFIX}sr-pull-1"), account=account,
            executor=self.executor, enabled_companies=[],
        )
        sr = _last_sync_record(f"{PREFIX}sr-pull-1", "Pull")
        self.assertIsNotNone(sr, "Pull op must write a Sync Record")
        self.assertEqual(sr.status, "Success")
        self.assertEqual(sr.entity_type, "Item")
        self.assertEqual(sr.direction, "Pull")

    def test_pull_fnc_does_not_write_sync_record(self) -> None:
        """FNC outcomes (missing HSN / unsupported type) don't create
        an ERPNext doc. Sync Record's entity_name is a Dynamic Link →
        writing one for a non-existent entity would trip Frappe's
        link validation and bubble up "Could not find {Doctype}:
        {sku}" errors via msgprint. The Item Map row carries the FNC
        state for the FDE worklist; Sync Record is per-entity and
        skipped when no entity exists."""
        account = _account(MODE_ONBOARDING)
        # variant_parent → unsupported type → FNC (no Item created)
        payload = dict(_payload(f"{PREFIX}sr-fnc-var"))
        payload["product_type"] = "variant_parent"
        process_one_product(
            payload, account=account, executor=self.executor,
            enabled_companies=[],
        )
        # No Item was created.
        self.assertFalse(frappe.db.exists("Item", f"{PREFIX}sr-fnc-var"))
        # No Sync Record either — would have failed link validation.
        sr = _last_sync_record(f"{PREFIX}sr-fnc-var", "Pull")
        self.assertIsNone(
            sr,
            "FNC outcomes must not write a Sync Record (the entity "
            "doesn't exist → Frappe Dynamic Link validation would "
            "trip). Item Map row carries the FNC state instead.",
        )

    def test_push_success_writes_success_sync_record(self) -> None:
        account = _account()
        item = _seed_mapped_item(f"{PREFIX}sr-push-1")
        # Add a tax row so TaxRate resolves (audit #5 doesn't change this).
        item.append("taxes", {
            "item_tax_template": GST_18_TEMPLATE,
            "minimum_net_rate": 0, "maximum_net_rate": 0,
        })
        item.save(ignore_permissions=True)
        client = MockClient()
        push_one_item(item.item_code, client=client, account=account,
                      enabled_companies=[self.company])
        sr = _last_sync_record(item.item_code, "Push")
        self.assertIsNotNone(sr)
        self.assertEqual(sr.status, "Success")

    def test_push_flagged_writes_failed_sync_record(self) -> None:
        """Missing dims → push_one_item returns operation=flagged →
        SR=Failed (the push didn't land on EE; that IS a sync failure)."""
        account = _account()
        item = _seed_mapped_item(f"{PREFIX}sr-push-flag", weight_per_unit=0,
                                  ecs_length_cm=0, ecs_height_cm=0, ecs_width_cm=0)
        client = MockClient()
        push_one_item(item.item_code, client=client, account=account,
                      enabled_companies=[self.company])
        # ZERO EE calls (flagged before push).
        self.assertEqual(client.calls, [])
        sr = _last_sync_record(item.item_code, "Push")
        self.assertEqual(sr.status, "Failed")
        self.assertIsNotNone(sr.last_error)

    def test_drift_writes_discrepancy_sync_record_not_failed(self) -> None:
        """The §7.3 hinge: drift is NOT Failed — it's Discrepancy.
        Stays cleanly distinct so §22 can subscribe to drift events
        without conflating them with sync failures."""
        account = _account(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}sr-drift", item_name="Original")
        process_one_product(
            _payload(f"{PREFIX}sr-drift", product_name="Changed"),
            account=account, executor=self.executor, enabled_companies=[],
        )
        sr = _last_sync_record(f"{PREFIX}sr-drift", "Pull")
        self.assertEqual(sr.status, "Discrepancy")
        self.assertNotEqual(sr.status, "Failed")  # explicit — distinct

    def test_lifecycle_success_writes_success_sync_record(self) -> None:
        account = _account()
        _seed_mapped_item(f"{PREFIX}sr-life", disabled=1)
        client = MockClient()
        push_lifecycle(f"{PREFIX}sr-life", client=client, account=account)
        sr = _last_sync_record(f"{PREFIX}sr-life", "Push")
        self.assertEqual(sr.status, "Success")


# ============================================================
# Audit #2 — Facade switch (no raw frappe.enqueue; Queue Job created)
# ============================================================


class TestEnqueueFacade(FrappeTestCase):

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_enqueue_item_push_creates_queue_job_row(self) -> None:
        """The facade — enqueue_easyecom_job — creates an
        EasyEcom Queue Job tracking row. Raw frappe.enqueue wouldn't."""
        _ensure_test_company()
        account = _account()
        _ensure_hsn(); _ensure_uom(); _ensure_item_group()
        _seed_mapped_item(f"{PREFIX}fac-1")
        # The actual call (no monkeypatch) — make the worker quietly
        # discoverable but unrun.
        qj_name = item_push.enqueue_item_push(
            f"{PREFIX}fac-1", account_name=account.name
        )
        self.assertTrue(qj_name)
        self.assertTrue(frappe.db.exists("EasyEcom Queue Job", qj_name))
        qj = frappe.get_doc("EasyEcom Queue Job", qj_name)
        self.assertEqual(qj.job_type, "Item Push")
        self.assertEqual(qj.target_doctype, "Item")
        self.assertEqual(qj.target_name, f"{PREFIX}fac-1")
        self.assertEqual(qj.state, "Queued")
        self.assertTrue(qj.idempotency_key)  # facade requires it


# ============================================================
# Audit #8 — Batch sweep enqueues + returns immediately
# ============================================================


class TestBatchSweepEnqueues(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _ensure_test_company()
        _ensure_hsn(); _ensure_uom(); _ensure_item_group()

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_sweep_enqueues_one_job_per_candidate(self) -> None:
        account = _account()
        # Three candidate items (no map row → eligible).
        for i in (1, 2, 3):
            code = f"{PREFIX}sweep-{i}"
            item = frappe.new_doc("Item")
            item.update({
                "item_code": code, "item_name": code,
                "item_group": _ensure_item_group(), "stock_uom": _ensure_uom(),
                "gst_hsn_code": _ensure_hsn(), "is_stock_item": 1,
                "weight_per_unit": 100, "ecs_length_cm": 10,
                "ecs_height_cm": 5, "ecs_width_cm": 3,
            })
            item.insert(ignore_permissions=True)

        with _spy_on_enqueue() as calls:
            result = enqueue_push_all_pending(account_name=account.name)
        # Only OUR three items show up among the calls.
        ours = [c for c in calls if c[0].startswith(f"{PREFIX}sweep-")]
        self.assertEqual(len(ours), 3)
        # Result reports the enqueued count.
        self.assertGreaterEqual(result["enqueued_count"], 3)


# ============================================================
# Audit #6 — Drift child table populated, ||-string gone
# ============================================================


class TestDriftChildTable(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        _ensure_hsn(); _ensure_uom(); _ensure_item_group()

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_drift_populates_structured_child_rows(self) -> None:
        account = _account(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}dft-tbl", item_name="OriginalName",
                          standard_rate=200)
        process_one_product(
            _payload(f"{PREFIX}dft-tbl", product_name="NewName", mrp=999),
            account=account, executor=self.executor, enabled_companies=[],
        )
        map_doc = frappe.get_doc("EasyEcom Item Map",
                                  {"ee_sku": f"{PREFIX}dft-tbl"})
        self.assertEqual(map_doc.status, STATUS_DRIFT)
        # Child rows present — at LEAST item_name + standard_rate diffs.
        fields_drifted = {row.field for row in map_doc.drift_fields}
        self.assertIn("item_name", fields_drifted)
        self.assertIn("standard_rate", fields_drifted)
        # Each row carries both values for the FDE to compare.
        for row in map_doc.drift_fields:
            self.assertTrue(row.erpnext_value)
            self.assertTrue(row.ee_value)
        # flag_reason is now a summary, not a ||-delimited string.
        self.assertIn("drifted", map_doc.flag_reason)


# ============================================================
# Audit #7 — Drift resolution: Dismiss
# ============================================================


class TestDriftResolutionDismiss(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        _ensure_hsn(); _ensure_uom(); _ensure_item_group()

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_dismiss_returns_drift_to_mapped(self) -> None:
        account = _account(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}dft-dis", item_name="A")
        process_one_product(
            _payload(f"{PREFIX}dft-dis", product_name="B"),
            account=account, executor=self.executor, enabled_companies=[],
        )
        map_doc = frappe.get_doc("EasyEcom Item Map", {"ee_sku": f"{PREFIX}dft-dis"})
        self.assertEqual(map_doc.status, STATUS_DRIFT)

        result = dismiss_drift(item_map_name=map_doc.name)
        self.assertTrue(result["ok"])
        refreshed = frappe.get_doc("EasyEcom Item Map", map_doc.name)
        self.assertEqual(refreshed.status, STATUS_MAPPED)
        self.assertIsNone(refreshed.flag_reason)
        self.assertEqual(len(refreshed.drift_fields), 0)
        # Underlying Item NOT mutated.
        item = frappe.get_doc("Item", f"{PREFIX}dft-dis")
        self.assertEqual(item.item_name, "A")  # ERPNext stays SoT

    def test_dismiss_rejects_non_drift_row(self) -> None:
        _seed_mapped_item(f"{PREFIX}dft-nondft")
        map_name = frappe.db.get_value(
            "EasyEcom Item Map", {"ee_sku": f"{PREFIX}dft-nondft"}, "name"
        )
        result = dismiss_drift(item_map_name=map_name)
        self.assertFalse(result["ok"])


# ============================================================
# Audit #10 — Field-level drift exclusion
# ============================================================


class TestDriftFieldExclusion(FrappeTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.executor = FieldMappingExecutor(ITEM_PULL_RULESET)
        _ensure_hsn(); _ensure_uom(); _ensure_item_group()

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_excluded_field_does_not_trigger_drift(self) -> None:
        account = _account(MODE_ERPNEXT_MASTERED)
        _seed_mapped_item(f"{PREFIX}exc-1", item_name="Renamed in ERPNext")
        map_name = frappe.db.get_value(
            "EasyEcom Item Map", {"ee_sku": f"{PREFIX}exc-1"}, "name"
        )
        # FDE adds item_name to the exclusion list.
        map_doc = frappe.get_doc("EasyEcom Item Map", map_name)
        map_doc.append("ecs_drift_exclude_fields",
                        {"field": "item_name", "reason": "intentional ERPNext rename"})
        map_doc.save(ignore_permissions=True)

        # Now pull with EE-side item_name different — should NOT flag.
        out = process_one_product(
            _payload(f"{PREFIX}exc-1", product_name="EE-side different"),
            account=account, executor=self.executor, enabled_companies=[],
        )
        self.assertNotEqual(out.status, STATUS_DRIFT)
        refreshed = frappe.get_doc("EasyEcom Item Map", map_name)
        self.assertNotEqual(refreshed.status, STATUS_DRIFT)


# ============================================================
# Audit #11 — Single-Account constraint
# ============================================================


class TestSingleAccountConstraint(FrappeTestCase):

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_second_enabled_account_is_refused(self) -> None:
        first = f"{PREFIX}sa-first"
        if not frappe.db.exists("EasyEcom Account", first):
            make_account(name=first)
        # Try a SECOND enabled account directly.
        with self.assertRaises(frappe.ValidationError):
            second = frappe.new_doc("EasyEcom Account")
            second.update({
                "account_name": f"{PREFIX}sa-second",
                "enabled": 1,
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.easyecom.io",
                "x_api_key": "k", "email": "e@e.com", "password": "p",
                "rate_limit_tier": "Silver", "webhook_enabled": 0,
            })
            second.insert(ignore_permissions=True)

    def test_second_disabled_account_is_allowed(self) -> None:
        first = f"{PREFIX}sa-first2"
        if not frappe.db.exists("EasyEcom Account", first):
            make_account(name=first)
        second = frappe.new_doc("EasyEcom Account")
        second.update({
            "account_name": f"{PREFIX}sa-second-off",
            "enabled": 0,  # disabled — fine
            "environment_badge": "Sandbox",
            "api_endpoint": "https://api.easyecom.io",
            "x_api_key": "k", "email": "e@e.com", "password": "p",
            "rate_limit_tier": "Silver", "webhook_enabled": 0,
        })
        second.insert(ignore_permissions=True)  # no exception
        self.assertTrue(frappe.db.exists(
            "EasyEcom Account", f"{PREFIX}sa-second-off"
        ))


# ============================================================
# Audit #3 — Scheduler entry exists + handles no-account quietly
# ============================================================


class TestSchedulerEntry(FrappeTestCase):

    def setUp(self) -> None: _wipe()
    def tearDown(self) -> None: _wipe()

    def test_scheduled_discover_products_handles_no_account(self) -> None:
        """When no Account exists, scheduled run is a quiet no-op
        (pre-onboarding state). Must NOT raise — that would crash
        the scheduler tick."""
        for n in frappe.db.get_all("EasyEcom Account",
                                    filters={"enabled": 1}, pluck="name"):
            frappe.db.set_value("EasyEcom Account", n, "enabled", 0,
                                update_modified=False)
        frappe.db.commit()
        # Should not raise.
        scheduled_discover_products()


# ============================================================
# Mark Mapped override (FDE/SM escape hatch for Created-Flagged)
# ============================================================


class TestMarkMappedOverride(FrappeTestCase):
    """The FDE/SM-only override that flips a Created-Flagged row to
    Mapped without fixing the source. Audit-logged via Frappe Comment
    so the override survives subsequent pulls' status churn.

    Contracts:
      - status=Created-Flagged required (refuses Mapped / Drift / FNC).
      - Operator role refused with PermissionError.
      - Underlying Item is NOT mutated.
      - A Comment is added to the Map row quoting the suppressed
        flag_reason + the user who clicked.
    """

    def setUp(self) -> None:
        _wipe()
        _ensure_hsn()
        _ensure_uom()
        _ensure_item_group()

    def tearDown(self) -> None:
        _wipe()

    def _seed_cf_row(self, item_code: str = f"{PREFIX}cf-ovr") -> tuple[str, str]:
        """Insert a Created-Flagged Item + Map row directly. We bypass
        the full pull (which would flip the row back on the next run)
        since this test exercises the override endpoint, not the
        pull's flag-evaluation."""
        if not frappe.db.exists("Item", item_code):
            it = frappe.new_doc("Item")
            it.item_code = item_code
            it.item_name = item_code
            it.item_group = "All Item Groups"
            it.stock_uom = "Nos"
            it.gst_hsn_code = "85171000"
            it.insert(ignore_permissions=True)
        m = frappe.new_doc("EasyEcom Item Map")
        m.update({
            "ee_sku": item_code,
            "status": STATUS_CREATED_FLAGGED,
            "erpnext_doctype": "Item",
            "erpnext_name": item_code,
            "flag_reason": "missing HSN || dirty UOM 'X' substituted with 'Nos'",
        })
        m.insert(ignore_permissions=True)
        frappe.db.commit()
        return item_code, m.name

    def test_override_flips_cf_to_mapped_and_clears_reason(self) -> None:
        item_code, map_name = self._seed_cf_row()
        result = mark_mapped_override(item_map_name=map_name)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "Mapped")
        refreshed = frappe.get_doc("EasyEcom Item Map", map_name)
        self.assertEqual(refreshed.status, STATUS_MAPPED)
        self.assertIsNone(refreshed.flag_reason)
        # Underlying Item is untouched.
        self.assertTrue(frappe.db.exists("Item", item_code))

    def test_override_writes_audit_comment(self) -> None:
        item_code, map_name = self._seed_cf_row(f"{PREFIX}cf-audit")
        mark_mapped_override(item_map_name=map_name)
        # Frappe stores Comments as `tabComment` rows with reference_doctype/name.
        comments = frappe.get_all(
            "Comment",
            filters={
                "reference_doctype": "EasyEcom Item Map",
                "reference_name": map_name,
            },
            fields=["content"],
        )
        self.assertTrue(comments, "audit Comment must be added")
        self.assertTrue(
            any("Mark Mapped override" in c.content for c in comments),
            f"comment must mention the override; got {[c.content[:100] for c in comments]}",
        )
        self.assertTrue(
            any("missing HSN" in c.content for c in comments),
            "audit comment must quote the suppressed flag_reason",
        )

    def test_override_rejects_mapped_row(self) -> None:
        item_code = f"{PREFIX}cf-mapped-skip"
        _ensure_test_company()
        _seed_mapped_item(item_code)
        map_name = frappe.db.get_value(
            "EasyEcom Item Map", {"ee_sku": item_code}, "name"
        )
        result = mark_mapped_override(item_map_name=map_name)
        self.assertFalse(result["ok"])
        self.assertIn("not Created-Flagged", result["message"])

    def test_override_rejects_unknown_map(self) -> None:
        result = mark_mapped_override(item_map_name="NO-SUCH-MAP-XYZZY")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())

    def test_override_refused_for_operator_role(self) -> None:
        item_code, map_name = self._seed_cf_row(f"{PREFIX}cf-op")
        email = "op-mark-mapped@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
        u = frappe.new_doc("User")
        u.update({
            "email": email, "first_name": "Op",
            "send_welcome_email": 0, "enabled": 1,
        })
        u.insert(ignore_permissions=True)
        u.append("roles", {"role": "EasyEcom Operator"})
        u.save(ignore_permissions=True)
        frappe.db.commit()
        orig = frappe.session.user
        frappe.set_user(email)
        try:
            with self.assertRaises(frappe.PermissionError):
                mark_mapped_override(item_map_name=map_name)
        finally:
            frappe.set_user(orig)
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
            frappe.db.commit()
