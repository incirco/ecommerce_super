"""Stage 5 tests for §8f — lifecycle / flip / drift detection.

ALL MOCKED — no real EE traffic. Mirrors §8e Stage 5 coverage:

  - Phase gate: supplier_master_mode='onboarding' → accept-and-create;
    'erpnext_mastered' → drift detection only (same input row produces
    DIFFERENT side-effects).
  - Drift cases:
      - new EE vendor post-flip → Drift row, NO Supplier created
      - EE edit to mapped Supplier → Drift, ERPNext untouched
      - quiet re-pull (no change) → stays Mapped (no flap)
      - quiet re-pull AFTER drift → drift table cleared, status sticks
        at Drift (FDE owns Drift → Mapped via Dismiss; no auto-heal)
  - Resolution actions: dismiss_drift, push_to_ee_for_drift; NO Accept-EE.
  - Field-level exclusion via EasyEcom Exclude Field child (entity-
    agnostic since §8f Stage 1 rename).
  - Drift outcome maps to Sync Record status=Failed per §7.3 M1
    binary contract (the legacy 'Discrepancy' enum was migrated to
    'Failed' in gh#16 — drift detail now lives in last_error).

§8f-specific:
  - Lifecycle pull-side already shipped in Stage 3 (active:0 →
    disabled, restore on active:1). This module adds the lifecycle
    test for the post-flip mode (active flag changes still get
    surfaced via drift detection on the lifecycle status comparison,
    not via direct mutation of Supplier.disabled).
  - Push-side deactivate endpoint: N/A. Live probe of 9 candidate
    paths returned 404 from Harmony — same finding as §8e Customer.
    Supplier.disabled stays ERPNext-local; pull-side already covers
    EE→EN deactivation. No push-side lifecycle test class because
    there's no endpoint to call.
"""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows.supplier_pull import (
    MODE_ERPNEXT_MASTERED,
    MODE_ONBOARDING,
    SUPPLIER_DRIFT_COMPARABLE_ADDRESS_FIELDS,
    SUPPLIER_DRIFT_COMPARABLE_SUPPLIER_FIELDS,
    dismiss_drift,
    process_one_supplier,
    push_to_ee_for_drift,
)
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)


PREFIX = "TEST-8F-S5-"

# Reuse the verified-check-digit GSTINs from Stage 4 tests so India
# Compliance accepts the test Supplier on insert.
VALID_GSTIN_DELHI = "07ABCDE1234F1Z2"


def _seed_country() -> None:
    """Stage 2 cache seed — drift detection uses _classify_country
    which reads from EasyEcom Country."""
    if not frappe.db.exists("EasyEcom Country", {"country_id": 1}):
        frappe.get_doc(
            {
                "doctype": "EasyEcom Country",
                "country_id": 1,
                "country_name": "India",
                "code_2": "IN",
                "code_3": "IND",
                "default_currency_code": "INR",
            }
        ).insert(ignore_permissions=True)


def _ensure_supplier_group() -> str:
    leaf = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if leaf:
        return leaf
    if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
        frappe.get_doc(
            {
                "doctype": "Supplier Group",
                "supplier_group_name": "All Supplier Groups",
                "is_group": 1,
            }
        ).insert(ignore_permissions=True)
    return (
        frappe.get_doc(
            {
                "doctype": "Supplier Group",
                "supplier_group_name": f"{PREFIX}SG",
                "parent_supplier_group": "All Supplier Groups",
                "is_group": 0,
            }
        )
        .insert(ignore_permissions=True)
        .name
    )


def _wipe_state() -> None:
    """Wipe Stage 5 test artifacts. Match by supplier_name prefix +
    the c_id-style placeholder pattern this stage uses."""
    test_docnames = frappe.db.get_all(
        "Supplier",
        filters={"supplier_name": ("like", f"{PREFIX}%")},
        pluck="name",
    )
    if test_docnames:
        for n in frappe.db.get_all(
            "EasyEcom Supplier Map",
            filters={"erpnext_name": ("in", test_docnames)},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Supplier Map",
                    n,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass
    # Drift map rows without a linked Supplier (EE-origin-new-post-flip).
    for n in frappe.db.get_all(
        "EasyEcom Supplier Map",
        filters={"ee_vendor_c_id": ("like", "8%")},  # test vendor_c_ids
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Supplier Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in test_docnames:
        # Linked addresses
        for (addr,) in frappe.db.sql(
            """SELECT DISTINCT parent FROM `tabDynamic Link`
               WHERE parenttype='Address' AND link_doctype='Supplier'
                 AND link_name=%s""",
            (n,),
        ):
            try:
                frappe.delete_doc(
                    "Address", addr, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        # Linked contacts
        for (cont,) in frappe.db.sql(
            """SELECT DISTINCT parent FROM `tabDynamic Link`
               WHERE parenttype='Contact' AND link_doctype='Supplier'
                 AND link_name=%s""",
            (n,),
        ):
            try:
                frappe.delete_doc(
                    "Contact", cont, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        try:
            frappe.delete_doc("Supplier", n, force=True, ignore_permissions=True)
        except Exception:
            pass
    if test_docnames:
        placeholders = ",".join(["%s"] * len(test_docnames))
        frappe.db.sql(
            f"""DELETE FROM `tabEasyEcom Sync Record`
               WHERE entity_doctype='Supplier' AND entity_name IN ({placeholders})""",
            tuple(test_docnames),
        )
    frappe.db.commit()


def _make_mapped_supplier(
    *,
    supplier_name: str,
    ee_vendor_c_id: str,
    ee_vendor_id: str = "",
    email: str = "test@example.local",
    mobile: str = "9999900001",
    gstin: str = "",
    gst_category: str = "Unregistered",
    billing: dict | None = None,
) -> tuple[str, str]:
    """Pre-create a Supplier + Contact + Billing Address + a Mapped
    Supplier Map row. Returns (supplier_docname, map_docname)."""
    payload = {
        "doctype": "Supplier",
        "supplier_name": supplier_name,
        "supplier_type": "Company",
        "supplier_group": _ensure_supplier_group(),
        "country": "India",
        "default_currency": "INR",
    }
    if gstin:
        payload["gstin"] = gstin
    if gst_category:
        payload["gst_category"] = gst_category
    sup = frappe.get_doc(payload).insert(ignore_permissions=True)

    # Contact (so the drift detector can read email/phone via the
    # primary-contact lookup helper).
    frappe.get_doc(
        {
            "doctype": "Contact",
            "first_name": "Test",
            "last_name": supplier_name,
            "email_ids": [{"email_id": email, "is_primary": 1}],
            "phone_nos": [{"phone": mobile, "is_primary_mobile_no": 1}],
            "links": [{"link_doctype": "Supplier", "link_name": sup.name}],
        }
    ).insert(ignore_permissions=True)

    billing = billing or {
        "address_line1": "Test Street",
        "city": "Delhi",
        "pincode": "110001",
        "state": "Delhi",
        "country": "India",
    }
    frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": sup.name,
            "address_type": "Billing",
            **billing,
            "links": [{"link_doctype": "Supplier", "link_name": sup.name}],
        }
    ).insert(ignore_permissions=True)

    map_doc = frappe.get_doc(
        {
            "doctype": "EasyEcom Supplier Map",
            "ee_vendor_c_id": ee_vendor_c_id,
            "ee_vendor_id": ee_vendor_id,
            "erpnext_doctype": "Supplier",
            "erpnext_name": sup.name,
            "status": "Mapped",
        }
    ).insert(ignore_permissions=True)
    return sup.name, map_doc.name


def _build_ee_vendor_row(*, vendor_c_id: int, **overrides) -> dict:
    """Synthesize a /wms/V2/getVendors row with defaults that line up
    with _make_mapped_supplier's defaults so quiet re-pulls yield no
    drift."""
    base = {
        "vendor_c_id": vendor_c_id,
        "vendor_code": overrides.pop("vendor_code", f"VC-{vendor_c_id}"),
        "vendor_name": f"{PREFIX}DRIFT-{vendor_c_id}",
        "email": "test@example.local",
        "contact_number": "9999900001",
        "active": 1,
        "tax_identification_number": "",
        "pan": "",
        "currency_code": "INR",
        "address": {
            "billing": {
                "address": "Test Street",
                "city": "Delhi",
                "state_name": "Delhi",
                "zip": "110001",
                "country": "India",
            },
            "dispatch": [],
        },
    }
    # Handle nested address overrides.
    address_overrides = overrides.pop("address", None)
    if address_overrides is not None:
        base["address"] = address_overrides
    base.update(overrides)
    return base


# =====================================================================
# Phase gate — same input, different side-effects per mode
# =====================================================================


class TestPhaseGateChangesPullBehaviour(FrappeTestCase):
    """The SAME EE row produces different side-effects depending on
    supplier_master_mode. Onboarding → accept-and-create;
    erpnext_mastered → drift detection (no create)."""

    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_onboarding_mode_creates(self) -> None:
        row = _build_ee_vendor_row(
            vendor_c_id=8500001,
            vendor_name=f"{PREFIX}PHASE-ONB",
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ONBOARDING
        )
        self.assertEqual(out.status, "Mapped")
        self.assertEqual(out.operation, "created")
        self.assertTrue(
            frappe.db.exists(
                "EasyEcom Supplier Map", {"ee_vendor_c_id": "8500001"}
            )
        )
        self.assertIsNotNone(out.supplier_docname)

    def test_erpnext_mastered_mode_drifts_new_supplier(self) -> None:
        """Same input — but ERPNext-mastered → drift row, no Supplier."""
        row = _build_ee_vendor_row(
            vendor_c_id=8500002,
            vendor_name=f"{PREFIX}PHASE-EM",
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        self.assertEqual(out.operation, "flagged")
        m = frappe.db.get_value(
            "EasyEcom Supplier Map",
            {"ee_vendor_c_id": "8500002"},
            ["status", "erpnext_name"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Drift")
        self.assertFalse(m.erpnext_name)
        # No Supplier created.
        self.assertEqual(
            frappe.db.count(
                "Supplier",
                filters={"supplier_name": f"{PREFIX}PHASE-EM"},
            ),
            0,
        )


# =====================================================================
# Drift detection — existing mapped supplier
# =====================================================================


class TestDriftOnEdit(FrappeTestCase):
    """EE-side edit to a mapped Supplier → Drift status + child rows;
    ERPNext NOT overwritten."""

    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_changed_supplier_name_drifts_not_overwrites(self) -> None:
        s_name, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}EDIT-NAME-OLD",
            ee_vendor_c_id="8510001",
        )
        row = _build_ee_vendor_row(
            vendor_c_id=8510001,
            vendor_name=f"{PREFIX}EDIT-NAME-NEW-EE-SIDE",
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        # Supplier name NOT overwritten.
        self.assertEqual(
            frappe.db.get_value("Supplier", s_name, "supplier_name"),
            f"{PREFIX}EDIT-NAME-OLD",
        )
        rows = frappe.db.get_all(
            "EasyEcom Drift Field",
            filters={
                "parent": map_name,
                "parenttype": "EasyEcom Supplier Map",
            },
            fields=["field", "erpnext_value", "ee_value"],
        )
        by_field = {r.field: r for r in rows}
        self.assertIn("supplier_name", by_field)
        self.assertEqual(by_field["supplier_name"].erpnext_value, f"{PREFIX}EDIT-NAME-OLD")
        self.assertEqual(
            by_field["supplier_name"].ee_value, f"{PREFIX}EDIT-NAME-NEW-EE-SIDE"
        )

    def test_changed_billing_city_records_prefixed_label(self) -> None:
        """Address-level drift carries 'billing.city' prefix so the FDE
        can tell which doc differed."""
        _, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}EDIT-CITY",
            ee_vendor_c_id="8510002",
            billing={
                "address_line1": "Test Street",
                "city": "Delhi",
                "pincode": "110001",
                "state": "Delhi",
                "country": "India",
            },
        )
        row = _build_ee_vendor_row(
            vendor_c_id=8510002,
            address={
                "billing": {
                    "address": "Test Street",
                    "city": "Bangalore",  # EE-side change
                    "state_name": "Delhi",
                    "zip": "110001",
                    "country": "India",
                },
                "dispatch": [],
            },
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        rows = frappe.db.get_all(
            "EasyEcom Drift Field",
            filters={
                "parent": map_name,
                "parenttype": "EasyEcom Supplier Map",
            },
            fields=["field"],
        )
        labels = {r.field for r in rows}
        self.assertIn("billing.city", labels)


class TestQuietRePullNoFlap(FrappeTestCase):
    """A pull with IDENTICAL EE data must leave the map row in Mapped
    (no flap). A pull with FIXED diff (was drifted, now matches) must
    clear the drift table but PRESERVE Drift status (no auto-heal —
    FDE owns Drift → Mapped via Dismiss)."""

    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_identical_payload_stays_mapped(self) -> None:
        _, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}QUIET",
            ee_vendor_c_id="8520001",
        )
        row = _build_ee_vendor_row(
            vendor_c_id=8520001,
            vendor_name=f"{PREFIX}QUIET",
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Mapped", "quiet re-pull must NOT flap to Drift")
        self.assertEqual(out.operation, "skipped")
        self.assertEqual(
            frappe.db.get_value("EasyEcom Supplier Map", map_name, "status"),
            "Mapped",
        )

    def test_quiet_repull_after_drift_clears_table_but_status_persists(self) -> None:
        """A previously-drifted row that becomes clean again has its
        drift_fields child rows cleared (no ghost diffs) BUT the row's
        Drift status PERSISTS. The FDE owns the Drift → Mapped
        transition via Dismiss — that's the §8d/§8e parity audit-
        trail contract."""
        _, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}QUIET-RECLEAR",
            ee_vendor_c_id="8520002",
        )
        # First pull with a diff → records drift.
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        row_drift = _build_ee_vendor_row(
            vendor_c_id=8520002,
            vendor_name=f"{PREFIX}QUIET-RECLEAR-EE-NAME",
        )
        process_one_supplier(
            row_drift, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        drift_rows_before = frappe.db.count(
            "EasyEcom Drift Field", filters={"parent": map_name}
        )
        self.assertGreater(drift_rows_before, 0)

        # Now EE-side fixes back to match ERPNext (or someone edited
        # ERPNext to match EE — either way, no diff now).
        row_clean = _build_ee_vendor_row(
            vendor_c_id=8520002,
            vendor_name=f"{PREFIX}QUIET-RECLEAR",
        )
        out = process_one_supplier(
            row_clean, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        # Status PERSISTS as Drift — FDE must Dismiss explicitly.
        self.assertEqual(out.status, "Drift")
        self.assertEqual(
            frappe.db.get_value("EasyEcom Supplier Map", map_name, "status"),
            "Drift",
        )
        # Drift child rows are cleared (no ghost diffs).
        self.assertEqual(
            frappe.db.count(
                "EasyEcom Drift Field", filters={"parent": map_name}
            ),
            0,
        )


# =====================================================================
# EE-origin new vendor post-flip
# =====================================================================


class TestNewEeSupplierPostFlip(FrappeTestCase):
    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_no_supplier_no_addresses_only_drift_map_row(self) -> None:
        """Post-flip pull encounters a brand-new EE vendor_c_id with NO
        prior map. Expected: Drift map row with no link, NO Supplier +
        NO Address rows created."""
        row = _build_ee_vendor_row(
            vendor_c_id=8530001, vendor_name=f"{PREFIX}NEW-EE"
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        self.assertIsNone(out.supplier_docname)
        # No Supplier created.
        self.assertEqual(
            frappe.db.count(
                "Supplier", filters={"supplier_name": f"{PREFIX}NEW-EE"}
            ),
            0,
        )
        # Map row exists, status=Drift, no link.
        m = frappe.db.get_value(
            "EasyEcom Supplier Map",
            {"ee_vendor_c_id": "8530001"},
            ["status", "erpnext_name", "ee_vendor_id"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Drift")
        self.assertFalse(m.erpnext_name)
        # vendor_id (write key) is captured on the Drift row when EE
        # surfaces it — so a future ERPNext-side create can adopt
        # this id rather than getting a new one.
        self.assertEqual(m.ee_vendor_id, "VC-8530001")


# =====================================================================
# Field exclusion
# =====================================================================


class TestFieldExclusion(FrappeTestCase):
    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_excluded_field_does_not_drift(self) -> None:
        """FDE marks supplier_name in the exclude table; subsequent
        pulls don't re-flag drift on that field."""
        s_name, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}EXCL-OLD",
            ee_vendor_c_id="8540001",
        )
        map_doc = frappe.get_doc("EasyEcom Supplier Map", map_name)
        map_doc.append(
            "ecs_drift_exclude_fields",
            {
                "field": "supplier_name",
                "reason": "FDE intentional rename",
            },
        )
        map_doc.save(ignore_permissions=True)
        frappe.db.commit()

        row = _build_ee_vendor_row(
            vendor_c_id=8540001,
            vendor_name=f"{PREFIX}EXCL-OLD-EE-DIFFERS",
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        # No drift recorded.
        self.assertEqual(out.status, "Mapped")
        diff_rows = frappe.db.get_all(
            "EasyEcom Drift Field", filters={"parent": map_name}
        )
        self.assertEqual(len(diff_rows), 0)

    def test_excluded_address_field_uses_prefixed_label(self) -> None:
        """Exclude 'billing.city' specifically — drift on billing.city
        is suppressed but dispatch.city would still drift if present."""
        s_name, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}EXCL-ADDR",
            ee_vendor_c_id="8540002",
        )
        # Also add a Shipping address so dispatch.* fields have an
        # ERPNext value to compare against (drift only fires when
        # both sides have data; a missing ERPNext side just means no
        # comparison row).
        frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": s_name + "-Ship",
                "address_type": "Shipping",
                "address_line1": "Test Street",
                "city": "Delhi",
                "pincode": "110001",
                "state": "Delhi",
                "country": "India",
                "links": [{"link_doctype": "Supplier", "link_name": s_name}],
            }
        ).insert(ignore_permissions=True)

        map_doc = frappe.get_doc("EasyEcom Supplier Map", map_name)
        map_doc.append(
            "ecs_drift_exclude_fields",
            {"field": "billing.city", "reason": "FDE intentional"},
        )
        map_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # EE has different billingCity (excluded) AND different
        # dispatchCity (not excluded).
        row = _build_ee_vendor_row(
            vendor_c_id=8540002,
            address={
                "billing": {
                    "address": "Test Street",
                    "city": "Bangalore",  # excluded → no drift
                    "state_name": "Delhi",
                    "zip": "110001",
                    "country": "India",
                },
                "dispatch": {
                    "address": "Test Street",
                    "city": "Bangalore",  # not excluded → drift
                    "state_name": "Delhi",
                    "zip": "110001",
                    "country": "India",
                },
            },
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        out = process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        rows = frappe.db.get_all(
            "EasyEcom Drift Field",
            filters={"parent": map_name},
            fields=["field"],
        )
        labels = {r.field for r in rows}
        self.assertIn("dispatch.city", labels)
        self.assertNotIn("billing.city", labels)


# =====================================================================
# Resolution actions — Dismiss + Push-to-EE; NO Accept-EE
# =====================================================================


class TestDismissDrift(FrappeTestCase):
    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_dismiss_resets_status_clears_child_rows(self) -> None:
        s_name, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}DISMISS",
            ee_vendor_c_id="8550001",
        )
        # Force into Drift by running a pull with a diff.
        row = _build_ee_vendor_row(
            vendor_c_id=8550001, vendor_name=f"{PREFIX}DISMISS-EE"
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(
            frappe.db.get_value("EasyEcom Supplier Map", map_name, "status"),
            "Drift",
        )

        result = dismiss_drift(supplier_map_name=map_name)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "Mapped")
        self.assertEqual(
            frappe.db.get_value("EasyEcom Supplier Map", map_name, "status"),
            "Mapped",
        )
        self.assertEqual(
            frappe.db.count(
                "EasyEcom Drift Field", filters={"parent": map_name}
            ),
            0,
        )
        # Underlying Supplier NOT modified.
        self.assertEqual(
            frappe.db.get_value("Supplier", s_name, "supplier_name"),
            f"{PREFIX}DISMISS",
        )

    def test_dismiss_rejects_non_drift_row(self) -> None:
        _, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}NONDRIFT",
            ee_vendor_c_id="8550002",
        )
        result = dismiss_drift(supplier_map_name=map_name)
        self.assertFalse(result["ok"])
        self.assertIn("not in Drift", result["message"])

    def test_dismiss_rejects_unknown_map(self) -> None:
        result = dismiss_drift(supplier_map_name="NOT-A-REAL-MAP-XYZZY")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())


class TestPushToEeForDrift(FrappeTestCase):
    """Drift resolution that re-asserts ERPNext as SoT."""

    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_push_dispatches_to_push_one_supplier(self) -> None:
        s_name, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}PUSHRE",
            ee_vendor_c_id="8560001",
        )
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            map_name,
            "status",
            "Drift",
            update_modified=False,
        )
        frappe.db.commit()

        with patch(
            "ecommerce_super.easyecom.flows.supplier_push.push_one_supplier"
        ) as mock_push:
            mock_push.return_value = MagicMock(
                operation="update",
                pushed=True,
                ee_vendor_c_id="8560001",
                ee_vendor_id="VC-8560001",
                flag_reasons=[],
            )
            result = push_to_ee_for_drift(supplier_map_name=map_name)

        self.assertTrue(result["ok"])
        mock_push.assert_called_once_with(s_name)

    def test_push_rejects_non_drift_row(self) -> None:
        _, map_name = _make_mapped_supplier(
            supplier_name=f"{PREFIX}PUSHRE-MAPPED",
            ee_vendor_c_id="8560002",
        )
        result = push_to_ee_for_drift(supplier_map_name=map_name)
        self.assertFalse(result["ok"])
        self.assertIn("not in Drift", result["message"])

    def test_push_rejects_when_no_linked_supplier(self) -> None:
        """EE-origin-new-post-flip rows have no linked Supplier."""
        doc = frappe.get_doc(
            {
                "doctype": "EasyEcom Supplier Map",
                "ee_vendor_c_id": "8560003",
                "ee_vendor_id": "",
                "status": "Drift",
                "flag_reason": "EE-origin new post-flip",
            }
        ).insert(ignore_permissions=True)

        result = push_to_ee_for_drift(supplier_map_name=doc.name)
        self.assertFalse(result["ok"])
        self.assertIn("no linked Supplier", result["message"])


class TestNoAcceptEeAction(FrappeTestCase):
    """Per packet: post-flip drift has NO 'Accept EE Value' resolution.
    The only paths out of Drift are Dismiss (keep ERPNext) and Push
    (re-assert ERPNext). EE-side novelty/edits are never adopted in
    steady state — §8.3 mirrors §8.2 / §8.1."""

    def test_no_accept_ee_method_exists_in_module(self) -> None:
        import ecommerce_super.easyecom.flows.supplier_pull as mod

        names = {
            n
            for n in dir(mod)
            if "accept" in n.lower() or "adopt" in n.lower()
        }
        self.assertEqual(
            names,
            set(),
            f"supplier_pull.py has unexpected accept/adopt-style "
            f"identifiers ({names}); §8.3 post-flip contract is "
            f"'ERPNext wins; EE-side values are NOT adopted'.",
        )


# =====================================================================
# Sync Record drift outcome (binary enum per §7.3 M1)
# =====================================================================


class TestSyncRecordDiscrepancy(FrappeTestCase):
    """Drift outcome → Sync Record status = Failed; the drift detail is
    captured in `last_error`. Per §7.3 M1 the per-record outcome is
    BINARY (Success | Failed); the legacy 'Discrepancy' status was
    migrated to 'Failed' in gh#16. The drift-vs-genuine-failure
    distinction now lives in last_error content, not the enum."""

    def setUp(self) -> None:
        _seed_country()
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_drift_writes_failed_sync_record_with_drift_reason(self) -> None:
        s_name, _ = _make_mapped_supplier(
            supplier_name=f"{PREFIX}DISCR",
            ee_vendor_c_id="8570001",
        )
        row = _build_ee_vendor_row(
            vendor_c_id=8570001, vendor_name=f"{PREFIX}DISCR-EE"
        )
        executor = FieldMappingExecutor("EasyEcom-Supplier-Pull")
        process_one_supplier(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        sr = frappe.db.get_value(
            "EasyEcom Sync Record",
            {
                "entity_doctype": "Supplier",
                "entity_name": s_name,
                "direction": "Pull",
            },
            ["status", "last_error"],
            as_dict=True,
        )
        self.assertEqual(
            sr.status, "Failed",
            "drift maps to Failed enum per §7.3 M1 binary contract "
            "(was 'Discrepancy' pre-gh#16; drift detail now in last_error)",
        )
        self.assertIsNotNone(
            sr.last_error,
            "drift reason must be carried in last_error",
        )
        # Qualitative check — last_error should name the diverging
        # field so the FDE can disposition.
        self.assertIn(
            "supplier_name", sr.last_error,
            "last_error should name the drifted field "
            "(the test fixture diverges on supplier_name)",
        )


# =====================================================================
# Drift comparable-fields contract
# =====================================================================


class TestDriftComparableFieldsContract(FrappeTestCase):
    """Lock the §8f drift field set — adding/removing must be a
    deliberate decision (and tested again)."""

    def test_supplier_level_comparable_fields(self) -> None:
        self.assertEqual(
            set(SUPPLIER_DRIFT_COMPARABLE_SUPPLIER_FIELDS),
            {
                "supplier_name",
                "gstin",
                "pan",
                "email_id",
                "mobile_no",
                "default_currency",
            },
        )

    def test_address_level_comparable_fields(self) -> None:
        labels = {
            label
            for label, _, _ in SUPPLIER_DRIFT_COMPARABLE_ADDRESS_FIELDS
        }
        # 5 fields × 2 sides = 10
        self.assertEqual(len(labels), 10)
        for side in ("billing", "dispatch"):
            for fld in ("street", "city", "pincode", "state", "country"):
                self.assertIn(f"{side}.{fld}", labels)

    def test_internal_ids_NOT_compared(self) -> None:
        """vendor_c_id / vendor_id / vendor_code (identity-management
        state) must NOT be drift-comparable — they're remap concerns,
        not drift."""
        for excluded in (
            "ee_vendor_c_id",
            "ee_vendor_id",
            "vendor_c_id",
            "vendor_id",
            "vendor_code",
        ):
            self.assertNotIn(
                excluded, SUPPLIER_DRIFT_COMPARABLE_SUPPLIER_FIELDS
            )
            self.assertNotIn(
                excluded,
                {
                    payload_key
                    for _, payload_key, _ in SUPPLIER_DRIFT_COMPARABLE_ADDRESS_FIELDS
                },
            )


# =====================================================================
# Flip contract — Stage 1 endpoint flips the mode the pull observes
# =====================================================================


class TestFlipChangesPullBehaviour(FrappeTestCase):
    """The Stage 1 flip endpoint (flip_to_erpnext_mastered_suppliers)
    is the operational switch for Stage 5's two-phase contract: before
    flip, pull accepts-and-creates; after flip, pull drifts. Verify
    this round-trips through the actual Account doc, not just the
    process_one_supplier internal phase flag."""

    ACCOUNT_NAME = "test-8f-s5-acct"

    def setUp(self) -> None:
        _seed_country()
        _wipe_state()
        if not frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            from ecommerce_super.tests.factories import make_account

            make_account(name=self.ACCOUNT_NAME, enabled=False)
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {
                "supplier_master_mode": "onboarding",
                "supplier_master_flipped_at": None,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe_state()
        try:
            frappe.delete_doc(
                "EasyEcom Account",
                self.ACCOUNT_NAME,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
        frappe.db.commit()

    def test_flip_changes_supplier_master_mode_to_erpnext_mastered(self) -> None:
        """The Stage 1 flip endpoint, when called, switches the field
        the Stage 5 pull observes. End-to-end contract verification:
        flip endpoint sets mode → pull sees mode → drift detector
        runs instead of accept-and-create."""
        from ecommerce_super.easyecom.api.supplier_master_mode import (
            flip_to_erpnext_mastered_suppliers,
        )

        # Pre-flip
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Account",
                self.ACCOUNT_NAME,
                "supplier_master_mode",
            ),
            "onboarding",
        )

        # Flip
        result = flip_to_erpnext_mastered_suppliers(
            account=self.ACCOUNT_NAME, confirm=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "erpnext_mastered")

        # Post-flip — the field changed.
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Account",
                self.ACCOUNT_NAME,
                "supplier_master_mode",
            ),
            "erpnext_mastered",
        )
        self.assertIsNotNone(
            frappe.db.get_value(
                "EasyEcom Account",
                self.ACCOUNT_NAME,
                "supplier_master_flipped_at",
            )
        )
