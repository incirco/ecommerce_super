"""Corrective commit 2026-05-29 (FIX 2) — pause-gate for the three
po_status pushes (3 / 5 / 7).

Tests verify Option A (consistent semantics):
  - When NOT paused: all three status pushes fire normally
    (existing tests in test_po_push_stage2 / test_grn_pull_stage3
    stay green).
  - When paused: each push defers — records ecs_pending_po_status_push
    on the PO Map, NO wire call.
  - On un-pause (go_live_enable_auto_push(pos=1)): the pending value
    fires once; idempotency via last_pushed_po_status still applies.
  - Latest-state-wins: multi-transition during pause overwrites the
    pending field (submit then cancel during pause = pending=7).
  - Adjacent fix verification: pause_all_auto_push NOW zeroes
    auto_push_pos_on_save too.

Pause detection: `_auto_push_enabled()` reads `auto_push_pos_on_save`.
Pause = field set to 0 (via pause_all_auto_push).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.auto_push_controls import (
    go_live_enable_auto_push,
    pause_all_auto_push,
)
from ecommerce_super.easyecom.flows.po_push import (
    PO_STATUS_APPROVED,
    PO_STATUS_CANCELLED,
    PO_STATUS_COMPLETED,
    _record_pending_status_push,
    enqueue_on_po_cancel,
    enqueue_on_po_submit,
    fire_pending_po_status_pushes,
)
from ecommerce_super.tests.factories import make_account


_PREFIX = "TEST-S9-FIX2-"


def _company() -> str:
    c = frappe.db.get_value("Company", filters={}, fieldname="name")
    if not c:
        raise RuntimeError("no Company")
    return c


def _wipe_po_maps_and_pos() -> None:
    # Cancel + delete any test POs (cascading deletes PO Maps too).
    for n in frappe.db.get_all(
        "Purchase Order",
        filters={"supplier": ("like", f"{_PREFIX}%")},
        pluck="name",
    ):
        try:
            doc = frappe.get_doc("Purchase Order", n)
            if int(doc.docstatus or 0) == 1:
                doc.cancel()
            frappe.delete_doc(
                "Purchase Order", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    # Wipe any orphan PO Maps.
    for n in frappe.db.get_all(
        "EasyEcom PO Map",
        filters={"reference_code": ("like", f"{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom PO Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _ensure_supplier(name: str) -> str:
    if frappe.db.exists("Supplier", name):
        return name
    g = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if not g:
        if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
            root = frappe.new_doc("Supplier Group")
            root.update(
                {
                    "supplier_group_name": "All Supplier Groups",
                    "is_group": 1,
                }
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
        g = sg.name
    s = frappe.new_doc("Supplier")
    s.update(
        {
            "supplier_name": name,
            "supplier_type": "Company",
            "supplier_group": g,
            "country": "India",
        }
    )
    s.insert(ignore_permissions=True)
    return s.name


def _ensure_item(code: str) -> str:
    if frappe.db.exists("Item", code):
        return code
    g = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    if not g:
        g = "All Item Groups"
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


def _ensure_warehouse(name: str) -> str:
    company = _company()
    existing = frappe.db.get_value(
        "Warehouse", {"warehouse_name": name, "company": company}, "name"
    )
    if existing:
        return existing
    w = frappe.new_doc("Warehouse")
    w.update({"warehouse_name": name, "company": company, "is_group": 0})
    w.insert(ignore_permissions=True)
    return w.name


def _make_submitted_po(*, sup_marker: str) -> str:
    supplier = _ensure_supplier(f"{_PREFIX}SUP-{sup_marker}")
    item = _ensure_item(f"{_PREFIX}ITEM-{sup_marker}")
    wh = _ensure_warehouse(f"{_PREFIX}WH-{sup_marker}")
    po = frappe.new_doc("Purchase Order")
    po.update(
        {
            "supplier": supplier,
            "company": _company(),
            "transaction_date": frappe.utils.today(),
            "schedule_date": frappe.utils.add_days(
                frappe.utils.today(), 7
            ),
            "set_warehouse": wh,
            "currency": "INR",
            "conversion_rate": 1,
        }
    )
    po.append(
        "items",
        {
            "item_code": item,
            "qty": 1,
            "rate": 1,
            "warehouse": wh,
            "schedule_date": po.schedule_date,
        },
    )
    po.insert(ignore_permissions=True)
    po.submit()
    return po.name


def _make_po_map(po_name: str, *, ee_po_id: int = 88800) -> str:
    m = frappe.new_doc("EasyEcom PO Map")
    m.update(
        {
            "reference_code": po_name,
            "purchase_order": po_name,
            "ee_po_id": ee_po_id,
            "status": "Mapped",
        }
    )
    m.insert(ignore_permissions=True)
    return m.name


class TestPauseAllAutoPushIncludesPOToggle(FrappeTestCase):
    """Adjacent fix verification — pause_all_auto_push NOW zeroes the
    auto_push_pos_on_save field. Previously this was a gap; FIX 2 made
    it consistent with the three masters."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_account()

    def setUp(self) -> None:
        # Pre-set all four to enabled.
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            {
                "enabled": 1,
                "auto_push_on_save": 1,
                "auto_push_customers_on_save": 1,
                "auto_push_suppliers_on_save": 1,
                "auto_push_pos_on_save": 1,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def test_pause_all_clears_pos_toggle_too(self) -> None:
        out = pause_all_auto_push(
            "test-account", reason="FIX 2 test", confirm=True
        )
        self.assertTrue(out["ok"])
        self.assertIn("POs", out["was_active"])
        self.assertEqual(out["state"]["pos"], 0)
        pos_value = frappe.db.get_value(
            "EasyEcom Account", "test-account", "auto_push_pos_on_save"
        )
        self.assertEqual(
            int(pos_value or 0),
            0,
            "auto_push_pos_on_save must be zeroed by pause_all_auto_push "
            "(corrective commit 2026-05-29 — prior gap was that pause "
            "didn't include the PO toggle, leaving PO writes uncovered).",
        )

    def test_go_live_re_enables_pos_toggle(self) -> None:
        # First pause.
        pause_all_auto_push(
            "test-account", reason="test", confirm=True
        )
        # Then go-live with pos=1.
        out = go_live_enable_auto_push(
            "test-account",
            items=0,
            customers=0,
            suppliers=0,
            pos=1,
            confirm=True,
        )
        self.assertTrue(out["ok"], out.get("message"))
        self.assertIn("POs", out["transitioned"])
        self.assertEqual(out["state"]["pos"], 1)


class TestSubmitPushDefersUnderPause(FrappeTestCase):
    """po_status=3 (Approved) on PO submit — defers under pause."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_po_maps_and_pos()
        make_account()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_po_maps_and_pos()

    def setUp(self) -> None:
        _wipe_po_maps_and_pos()
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            {
                "enabled": 1,
                "auto_push_pos_on_save": 0,  # PAUSED
            },
            update_modified=False,
        )
        frappe.db.commit()

    def test_submit_under_pause_records_pending_3(self) -> None:
        po_name = _make_submitted_po(sup_marker="SUB1")
        _make_po_map(po_name, ee_po_id=88001)
        # Fake a PO doc-like object — enqueue_on_po_submit only needs
        # .doctype and .name.
        doc = frappe._dict(doctype="Purchase Order", name=po_name)
        # _resolve_po_warehouse_to_location would normally find a real
        # PO; patch it for this unit-style test.
        with patch(
            "ecommerce_super.easyecom.flows.po_push._resolve_po_warehouse_to_location",
            return_value={"location_key": "L1", "mapped_warehouse": "WH"},
        ):
            enqueue_on_po_submit(doc)
        pending = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_name},
            "ecs_pending_po_status_push",
        )
        self.assertEqual(
            int(pending or 0),
            PO_STATUS_APPROVED,
            f"FIX 2: submit under pause must record pending=3; got {pending!r}",
        )


class TestCancelPushDefersUnderPause(FrappeTestCase):
    """po_status=7 (Cancelled) on PO cancel — defers under pause
    (Option A consistent semantics)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_po_maps_and_pos()
        make_account()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_po_maps_and_pos()

    def setUp(self) -> None:
        _wipe_po_maps_and_pos()
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            {"enabled": 1, "auto_push_pos_on_save": 0},  # PAUSED
            update_modified=False,
        )
        frappe.db.commit()

    def test_cancel_under_pause_records_pending_7(self) -> None:
        po_name = _make_submitted_po(sup_marker="CAN1")
        _make_po_map(po_name, ee_po_id=88002)
        doc = frappe._dict(doctype="Purchase Order", name=po_name)
        enqueue_on_po_cancel(doc)
        pending = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_name},
            "ecs_pending_po_status_push",
        )
        self.assertEqual(
            int(pending or 0),
            PO_STATUS_CANCELLED,
            f"FIX 2: cancel under pause must record pending=7; got {pending!r}",
        )

    def test_cancel_when_not_paused_does_not_record_pending(self) -> None:
        """Sanity check — when NOT paused, cancel goes to the queue
        (existing behaviour) rather than recording pending."""
        # Un-pause.
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            "auto_push_pos_on_save",
            1,
            update_modified=False,
        )
        po_name = _make_submitted_po(sup_marker="CAN2")
        _make_po_map(po_name, ee_po_id=88003)
        doc = frappe._dict(doctype="Purchase Order", name=po_name)
        # _enqueue_status_push touches the queue facade; patch it.
        with patch(
            "ecommerce_super.easyecom.flows.po_push._enqueue_status_push"
        ) as mock_enq:
            enqueue_on_po_cancel(doc)
        mock_enq.assert_called_once_with(
            po_docname=po_name, target_status=PO_STATUS_CANCELLED
        )
        pending = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_name},
            "ecs_pending_po_status_push",
        )
        self.assertEqual(int(pending or 0), 0)


class TestLatestStateWinsDuringPause(FrappeTestCase):
    """Multi-transition during pause: latest intended state overwrites
    the pending field (not a queue). Submit then cancel during pause
    leaves pending=7."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_po_maps_and_pos()
        make_account()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_po_maps_and_pos()

    def setUp(self) -> None:
        _wipe_po_maps_and_pos()
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            {"enabled": 1, "auto_push_pos_on_save": 0},  # PAUSED
            update_modified=False,
        )
        frappe.db.commit()

    def test_submit_then_cancel_during_pause_leaves_pending_7(self) -> None:
        po_name = _make_submitted_po(sup_marker="LAT1")
        _make_po_map(po_name, ee_po_id=88010)
        # Direct calls — exercise the helper without doc-event machinery.
        _record_pending_status_push(
            po_docname=po_name, target_status=PO_STATUS_APPROVED
        )
        # Then cancel during the same pause window.
        _record_pending_status_push(
            po_docname=po_name, target_status=PO_STATUS_CANCELLED
        )
        pending = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_name},
            "ecs_pending_po_status_push",
        )
        self.assertEqual(
            int(pending or 0),
            PO_STATUS_CANCELLED,
            "Latest-state-wins: pending field reflects the LATEST "
            "transition intent (overwrite, not queue).",
        )

    def test_completion_overwrites_pending_after_submit(self) -> None:
        po_name = _make_submitted_po(sup_marker="LAT2")
        _make_po_map(po_name, ee_po_id=88011)
        _record_pending_status_push(
            po_docname=po_name, target_status=PO_STATUS_APPROVED
        )
        _record_pending_status_push(
            po_docname=po_name, target_status=PO_STATUS_COMPLETED
        )
        pending = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_name},
            "ecs_pending_po_status_push",
        )
        self.assertEqual(int(pending or 0), PO_STATUS_COMPLETED)


class TestFirePendingOnUnpause(FrappeTestCase):
    """fire_pending_po_status_pushes() — fires each pending push,
    clears the field on success, idempotency guard last_pushed_po_status
    still applies."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _wipe_po_maps_and_pos()
        make_account()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        _wipe_po_maps_and_pos()

    def setUp(self) -> None:
        _wipe_po_maps_and_pos()
        # Belt-and-suspenders: zero any stray pending values on PO Maps
        # left by sibling tests in this module that share the dev site.
        frappe.db.sql(
            "UPDATE `tabEasyEcom PO Map` "
            "SET `ecs_pending_po_status_push` = 0 "
            "WHERE `ecs_pending_po_status_push` != 0"
        )
        # UN-PAUSED for these tests (the fire helper refuses to run
        # under pause).
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            {"enabled": 1, "auto_push_pos_on_save": 1},
            update_modified=False,
        )
        frappe.db.commit()

    def test_fire_pending_returns_noop_when_paused(self) -> None:
        # Flip back to paused for this test.
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            "auto_push_pos_on_save",
            0,
            update_modified=False,
        )
        out = fire_pending_po_status_pushes()
        self.assertFalse(out["ok"])
        self.assertEqual(out["fired"], 0)

    def test_fire_pending_invokes_push_for_each_pending_map(self) -> None:
        # Seed three PO Maps with different pending values.
        po_names = []
        for i, target in enumerate(
            [PO_STATUS_APPROVED, PO_STATUS_COMPLETED, PO_STATUS_CANCELLED]
        ):
            po_name = _make_submitted_po(sup_marker=f"FIRE{i}")
            po_names.append(po_name)
            _make_po_map(po_name, ee_po_id=88020 + i)
            frappe.db.set_value(
                "EasyEcom PO Map",
                {"purchase_order": po_name},
                "ecs_pending_po_status_push",
                int(target),
                update_modified=False,
            )
        frappe.db.commit()

        # Patch push_po_status to a no-op success that clears the
        # pending field via the runner's normal path.
        from ecommerce_super.easyecom.flows import po_push

        called: list[tuple[str, int]] = []

        def fake_push(*, po_docname: str, target_status: int, **_kw):
            called.append((po_docname, target_status))
            from ecommerce_super.easyecom.flows.po_push import (
                POPushOutcome,
            )
            return POPushOutcome(
                po_docname=po_docname,
                operation="status_only",
                pushed=True,
                ee_po_id=88020,
                po_map_status="Mapped",
                flag_reasons=[],
            )

        with patch.object(po_push, "push_po_status", side_effect=fake_push):
            out = fire_pending_po_status_pushes()

        self.assertTrue(out["ok"])
        self.assertEqual(out["fired"], 3)
        targets_fired = sorted(t for _, t in called)
        self.assertEqual(
            targets_fired,
            sorted([PO_STATUS_APPROVED, PO_STATUS_COMPLETED, PO_STATUS_CANCELLED]),
        )

        # Pending fields cleared.
        for po_name in po_names:
            pending = frappe.db.get_value(
                "EasyEcom PO Map",
                {"purchase_order": po_name},
                "ecs_pending_po_status_push",
            )
            self.assertEqual(
                int(pending or 0),
                0,
                f"Pending must be cleared after successful fire on {po_name}",
            )

    def test_go_live_pos_invokes_fire_pending(self) -> None:
        """go_live_enable_auto_push(pos=1) calls fire_pending after
        flipping the toggle. End-to-end: paused → record pending →
        go_live → pending fires + cleared."""
        # Pause first.
        frappe.db.set_value(
            "EasyEcom Account",
            "test-account",
            "auto_push_pos_on_save",
            0,
            update_modified=False,
        )
        po_name = _make_submitted_po(sup_marker="E2E")
        _make_po_map(po_name, ee_po_id=88030)
        _record_pending_status_push(
            po_docname=po_name, target_status=PO_STATUS_CANCELLED
        )
        # Verify pending set.
        self.assertEqual(
            int(
                frappe.db.get_value(
                    "EasyEcom PO Map",
                    {"purchase_order": po_name},
                    "ecs_pending_po_status_push",
                )
                or 0
            ),
            PO_STATUS_CANCELLED,
        )

        from ecommerce_super.easyecom.flows import po_push

        def fake_push(*, po_docname: str, target_status: int, **_kw):
            from ecommerce_super.easyecom.flows.po_push import (
                POPushOutcome,
            )
            return POPushOutcome(
                po_docname=po_docname,
                operation="status_only",
                pushed=True,
                ee_po_id=88030,
                po_map_status="Mapped",
                flag_reasons=[],
            )

        with patch.object(po_push, "push_po_status", side_effect=fake_push):
            out = go_live_enable_auto_push(
                "test-account",
                items=0,
                customers=0,
                suppliers=0,
                pos=1,
                confirm=True,
            )

        self.assertTrue(out["ok"])
        self.assertEqual(out["fired_pending_status_pushes"]["fired"], 1)
        # Pending cleared on the PO Map.
        cleared = frappe.db.get_value(
            "EasyEcom PO Map",
            {"purchase_order": po_name},
            "ecs_pending_po_status_push",
        )
        self.assertEqual(int(cleared or 0), 0)
