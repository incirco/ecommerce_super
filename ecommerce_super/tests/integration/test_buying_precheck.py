"""§9 Stage 4 — pre-go-live readiness check for the Buying flow.

Tests verify the four-check surface:
  1. Stock Settings.allow_negative_stock (warning)
  2. Account.default_rejected_warehouse (blocker)
  3. Account.grn_receipt_trigger_status (blocker)
  4. Each Live Location's mapped_warehouse has an Address (blocker)
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.api.buying_precheck import (
    precheck_buying_go_live,
)
from ecommerce_super.tests.factories import (
    cleanup_easyecom_state,
    make_account,
    make_location,
)


def _make_warehouse(warehouse_name: str, company: str = "_Test Company") -> str:
    """Inserts a Warehouse. Returns the resolved docname (Frappe appends
    the company abbr to warehouse_name, so the docname differs from
    warehouse_name)."""
    abbr = frappe.db.get_value("Company", company, "abbr") or "TC"
    expected = f"{warehouse_name} - {abbr}"
    if frappe.db.exists("Warehouse", expected):
        return expected
    doc = frappe.get_doc(
        {
            "doctype": "Warehouse",
            "warehouse_name": warehouse_name,
            "company": company,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _wipe_addresses_for_warehouse(warehouse: str) -> None:
    """Delete any existing Address rows linked to this Warehouse — kept
    idempotent so re-running setUpClass on a dev site doesn't pile up
    duplicate Addresses."""
    address_names = frappe.db.sql(
        """
        SELECT DISTINCT dl.parent
        FROM `tabDynamic Link` dl
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND dl.link_name = %s
        """,
        (warehouse,),
        as_dict=True,
    )
    for row in address_names:
        try:
            frappe.delete_doc(
                "Address",
                row["parent"],
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass


def _link_address_to_warehouse(
    warehouse: str, *, address_line1: str = "Test Industrial Estate"
) -> str:
    """Insert an Address with a Dynamic Link to the Warehouse."""
    addr = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": f"Addr-{warehouse}",
            "address_type": "Shipping",
            "address_line1": address_line1,
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560001",
            "country": "India",
            "links": [
                {
                    "link_doctype": "Warehouse",
                    "link_name": warehouse,
                }
            ],
        }
    )
    addr.insert(ignore_permissions=True)
    return addr.name


class TestPrecheckBuyingGoLive(FrappeTestCase):
    """All four checks under one fixture set. Each test starts from
    a clean Account + Location and toggles the property under test."""

    ACCOUNT = "test-account"
    LOC_KEY = "TEST-PRECHECK-LOC"
    LOC_DOCNAME = "ECS-LOC-TEST-PRECHECK-LOC"
    WH_BASE = "_Test Precheck WH"
    WH_NAME = ""  # filled in setUpClass (Frappe appends company abbr)

    # Snapshot of pre-existing Live Locations we'll temporarily mute so
    # the precheck's Location-scan (which walks all Live + Enabled rows
    # site-wide per §8.1) only sees our test fixture. Restored in
    # tearDownClass — non-destructive.
    _muted_locations: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cleanup_easyecom_state()
        # Mute existing Live + Enabled Locations (e.g. real Harmony
        # locations on a dev site) so the precheck scan only sees our
        # test fixture. Each row is restored to enabled=1 in
        # tearDownClass. Non-destructive.
        cls._muted_locations = frappe.db.get_all(
            "EasyEcom Location",
            filters={"workflow_state": "Live", "enabled": 1},
            pluck="name",
        )
        for loc in cls._muted_locations:
            frappe.db.set_value(
                "EasyEcom Location",
                loc,
                "enabled",
                0,
                update_modified=False,
            )
        # enabled=False because this test runs on dev sites that may
        # have a real (single, enabled) Account fixture pre-installed
        # (e.g. Harmony). The single-Account validate constraint would
        # refuse a second enabled=1 row. The precheck reads-only and
        # doesn't filter on Account.enabled — a disabled test-account
        # is identical for the purposes of these checks.
        make_account(cls.ACCOUNT, enabled=False)
        cls.WH_NAME = _make_warehouse(cls.WH_BASE)
        _wipe_addresses_for_warehouse(cls.WH_NAME)
        _link_address_to_warehouse(cls.WH_NAME)
        make_location(
            location_key=cls.LOC_KEY,
            is_operational=True,
            frappe_company="_Test Company",
            mapped_warehouse=cls.WH_NAME,
        )
        # §8.1 single-Account: no Location.account FK. The precheck's
        # Location scan walks all Live + Enabled Locations on the site,
        # not Account-filtered.
        # Set defaults for a clean baseline.
        frappe.db.set_value(
            "EasyEcom Account",
            cls.ACCOUNT,
            {
                "default_rejected_warehouse": cls.WH_NAME,
                "grn_receipt_trigger_status": "3 QC Complete",
                # Onboarding cutoff — primed at fixture time. Tests
                # that exercise the NULL-watermark blocker clear this
                # temporarily and restore.
                "grn_pull_high_watermark": "2026-05-28 00:00:00",
            },
            update_modified=False,
        )
        frappe.db.set_value(
            "Stock Settings", "Stock Settings", "allow_negative_stock", 0
        )
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        # Restore muted Live Locations before cleanup.
        for loc in cls._muted_locations:
            if frappe.db.exists("EasyEcom Location", loc):
                frappe.db.set_value(
                    "EasyEcom Location",
                    loc,
                    "enabled",
                    1,
                    update_modified=False,
                )
        frappe.db.commit()
        cleanup_easyecom_state()
        super().tearDownClass()

    def test_baseline_passes(self) -> None:
        """Happy path — everything configured, ok=True, no blockers."""
        out = precheck_buying_go_live(self.ACCOUNT)
        self.assertTrue(
            out["ok"],
            f"Baseline precheck should pass: blockers={out['blockers']}",
        )
        self.assertEqual(out["blockers"], [])
        self.assertEqual(out["warnings"], [])
        self.assertTrue(out["checked"])

    def test_missing_rejected_warehouse_is_blocker(self) -> None:
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT,
            "default_rejected_warehouse",
            None,
            update_modified=False,
        )
        try:
            out = precheck_buying_go_live(self.ACCOUNT)
            self.assertFalse(out["ok"])
            self.assertTrue(
                any(
                    "default_rejected_warehouse" in b
                    for b in out["blockers"]
                ),
                f"Expected rejected-warehouse blocker: {out['blockers']}",
            )
        finally:
            frappe.db.set_value(
                "EasyEcom Account",
                self.ACCOUNT,
                "default_rejected_warehouse",
                self.WH_NAME,
                update_modified=False,
            )

    def test_negative_stock_is_warning_not_blocker(self) -> None:
        frappe.db.set_value(
            "Stock Settings", "Stock Settings", "allow_negative_stock", 1
        )
        try:
            out = precheck_buying_go_live(self.ACCOUNT)
            # allow_negative_stock is a WARNING — ok stays True.
            self.assertTrue(
                out["ok"],
                f"Negative stock is warning-only, not blocker: "
                f"{out['blockers']}",
            )
            self.assertTrue(
                any(
                    "allow_negative_stock" in w for w in out["warnings"]
                ),
                f"Expected negative-stock warning: {out['warnings']}",
            )
        finally:
            frappe.db.set_value(
                "Stock Settings",
                "Stock Settings",
                "allow_negative_stock",
                0,
            )

    def test_warehouse_without_address_is_blocker(self) -> None:
        # Wipe all Addresses linked to the test Warehouse (there may be
        # >1 from prior test runs that didn't tearDown cleanly).
        _wipe_addresses_for_warehouse(self.WH_NAME)
        frappe.db.commit()
        try:
            out = precheck_buying_go_live(self.ACCOUNT)
            self.assertFalse(out["ok"])
            self.assertTrue(
                any(
                    "no resolvable Address" in b
                    for b in out["blockers"]
                ),
                f"Expected address blocker: {out['blockers']}",
            )
        finally:
            _link_address_to_warehouse(self.WH_NAME)
            frappe.db.commit()

    def test_nonexistent_account_returns_blocker(self) -> None:
        out = precheck_buying_go_live("nonexistent-account")
        self.assertFalse(out["ok"])
        self.assertTrue(
            any("not found" in b for b in out["blockers"])
        )

    def test_blank_grn_receipt_trigger_status_is_blocker(self) -> None:
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT,
            "grn_receipt_trigger_status",
            "",
            update_modified=False,
        )
        try:
            out = precheck_buying_go_live(self.ACCOUNT)
            self.assertFalse(out["ok"])
            self.assertTrue(
                any(
                    "grn_receipt_trigger_status" in b
                    for b in out["blockers"]
                ),
                f"Expected trigger-status blocker: {out['blockers']}",
            )
        finally:
            frappe.db.set_value(
                "EasyEcom Account",
                self.ACCOUNT,
                "grn_receipt_trigger_status",
                "3 QC Complete",
                update_modified=False,
            )

    def test_null_grn_pull_high_watermark_is_blocker(self) -> None:
        """§9 Stage 4 — the onboarding cutoff. Per user clarification
        (2026-05-28): NULL watermark would drag in EE's last-7-days
        backstop of historical GRNs. Block until primed."""
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT,
            "grn_pull_high_watermark",
            None,
            update_modified=False,
        )
        try:
            out = precheck_buying_go_live(self.ACCOUNT)
            self.assertFalse(out["ok"])
            self.assertTrue(
                any(
                    "grn_pull_high_watermark" in b
                    for b in out["blockers"]
                ),
                f"Expected watermark blocker: {out['blockers']}",
            )
            self.assertTrue(
                any(
                    "onboarding cutoff" in b.lower()
                    or "ONBOARDING CUTOFF" in b
                    for b in out["blockers"]
                ),
                "Blocker message must surface the 'onboarding cutoff' "
                "framing so the FDE knows what to do.",
            )
        finally:
            frappe.db.set_value(
                "EasyEcom Account",
                self.ACCOUNT,
                "grn_pull_high_watermark",
                "2026-05-28 00:00:00",
                update_modified=False,
            )
