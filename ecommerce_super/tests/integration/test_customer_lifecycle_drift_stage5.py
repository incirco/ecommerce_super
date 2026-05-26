"""Stage 5 tests for §8e — lifecycle / flip / drift detection.

ALL MOCKED — no real EE traffic. Mirrors §8d Stage 5 coverage:

  - Phase gate: customer_master_mode='onboarding' → accept-and-create;
    'erpnext_mastered' → drift detection only (same input row produces
    DIFFERENT side-effects).
  - Drift cases:
      - new EE customer post-flip → Drift row, NO Customer created
      - EE edit to mapped Customer → Drift, ERPNext untouched
      - quiet re-pull (no change) → stays Mapped (no flap)
  - Resolution actions: dismiss_drift, push_to_ee_for_drift; no Accept-EE.
  - Field-level exclusion via EasyEcom Item Map Exclude Field child
    (reused from §8d — generic shape).
  - Sync Record Discrepancy mapping for drift (not Failed).
"""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.flows.customer_pull import (
    CUSTOMER_DRIFT_COMPARABLE_CUSTOMER_FIELDS,
    CUSTOMER_DRIFT_COMPARABLE_ADDRESS_FIELDS,
    MODE_ERPNEXT_MASTERED,
    MODE_ONBOARDING,
    dismiss_drift,
    process_one_customer,
    push_to_ee_for_drift,
)
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)


FIXTURE_DIR = os.path.join(
    frappe.get_app_path("ecommerce_super"),
    "..", "process", "ee_mock_fixtures",
)
PREFIX = "TEST-8E-S5-"


def _load_fixture() -> dict:
    with open(os.path.join(FIXTURE_DIR, "getcustomers_b2b_response.json")) as f:
        return json.load(f)


def _wipe_state() -> None:
    test_customer_docnames = frappe.db.get_all(
        "Customer",
        filters={"customer_name": ("like", f"{PREFIX}%")},
        pluck="name",
    )
    if test_customer_docnames:
        for n in frappe.db.get_all(
            "EasyEcom Customer Map",
            filters={"erpnext_name": ("in", test_customer_docnames)},
            pluck="name",
        ):
            try: frappe.delete_doc("EasyEcom Customer Map", n, force=True, ignore_permissions=True)
            except Exception: pass
    for n in frappe.db.get_all(
        "EasyEcom Customer Map",
        filters={"ee_c_id": ("like", f"%{PREFIX}%")},
        pluck="name",
    ):
        try: frappe.delete_doc("EasyEcom Customer Map", n, force=True, ignore_permissions=True)
        except Exception: pass
    for n in test_customer_docnames:
        for addr in frappe.db.sql(
            "SELECT DISTINCT parent FROM `tabDynamic Link` "
            "WHERE parenttype='Address' AND link_doctype='Customer' AND link_name=%s",
            (n,),
        ):
            try: frappe.delete_doc("Address", addr[0], force=True, ignore_permissions=True)
            except Exception: pass
        try: frappe.delete_doc("Customer", n, force=True, ignore_permissions=True)
        except Exception: pass
    if test_customer_docnames:
        placeholders = ",".join(["%s"] * len(test_customer_docnames))
        frappe.db.sql(
            f"DELETE FROM `tabEasyEcom Sync Record` "
            f"WHERE entity_doctype='Customer' AND entity_name IN ({placeholders})",
            tuple(test_customer_docnames),
        )
    frappe.db.commit()


def _ensure_customer_group() -> str:
    leaf = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    if leaf: return leaf
    if not frappe.db.exists("Customer Group", "All Customer Groups"):
        frappe.get_doc({
            "doctype": "Customer Group",
            "customer_group_name": "All Customer Groups",
            "is_group": 1,
        }).insert(ignore_permissions=True)
    return frappe.get_doc({
        "doctype": "Customer Group",
        "customer_group_name": "TEST-8E-S5-CG",
        "parent_customer_group": "All Customer Groups",
        "is_group": 0,
    }).insert(ignore_permissions=True).name


def _ensure_territory() -> str:
    leaf = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if leaf: return leaf
    if not frappe.db.exists("Territory", "All Territories"):
        frappe.get_doc({
            "doctype": "Territory",
            "territory_name": "All Territories",
            "is_group": 1,
        }).insert(ignore_permissions=True)
    return frappe.get_doc({
        "doctype": "Territory",
        "territory_name": "TEST-8E-S5-T",
        "parent_territory": "All Territories",
        "is_group": 0,
    }).insert(ignore_permissions=True).name


def _make_mapped_customer(
    *,
    customer_name: str,
    ee_c_id: str,
    email: str = "test@example.local",
    mobile: str = "9999900001",
    gstin: str = "",
    gst_category: str = "Unregistered",
    billing: dict | None = None,
    shipping: dict | None = None,
) -> tuple[str, str]:
    """Pre-create a Customer + 2 Addresses + a Mapped Customer Map row.
    Returns (customer_docname, map_docname)."""
    cust = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": customer_name,
        "customer_type": "Company",
        "customer_group": _ensure_customer_group(),
        "territory": _ensure_territory(),
        "email_id": email,
        "mobile_no": mobile,
        "gstin": gstin,
        "gst_category": gst_category,
        "default_currency": "INR",
    }).insert(ignore_permissions=True)

    billing = billing or {
        "address_line1": "Test Street", "city": "Delhi",
        "pincode": "110001", "state": "Delhi", "country": "India",
    }
    shipping = shipping or {
        "address_line1": "Test Street", "city": "Delhi",
        "pincode": "110001", "state": "Delhi", "country": "India",
    }
    for atype, addr in (("Billing", billing), ("Shipping", shipping)):
        frappe.get_doc({
            "doctype": "Address",
            "address_title": cust.name,
            "address_type": atype,
            **addr,
            "links": [{"link_doctype": "Customer", "link_name": cust.name}],
        }).insert(ignore_permissions=True)

    map_doc = frappe.get_doc({
        "doctype": "EasyEcom Customer Map",
        "ee_c_id": ee_c_id,
        "ee_customer_id": ee_c_id,
        "erpnext_doctype": "Customer",
        "erpnext_name": cust.name,
        "status": "Mapped",
    }).insert(ignore_permissions=True)
    return cust.name, map_doc.name


def _build_ee_row(*, c_id: int, **overrides) -> dict:
    """Synthesize a /Wholesale/v2/UserManagement row with sensible
    defaults that line up with _make_mapped_customer's defaults so
    quiet re-pulls yield no drift."""
    base = {
        "c_id": c_id,
        "companyname": f"{PREFIX}DRIFT-{c_id}",
        "customer_support_email": "test@example.local",
        "customer_support_contact": "9999900001",
        "gstNum": "",
        "currency_code": "INR",
        "customer_country_id": 1,
        "billingStreet": "Test Street",
        "billingCity": "Delhi",
        "billingZipcode": "110001",
        "billingState": "Delhi",
        "billingCountry": "India",
        "dispatchStreet": "Test Street",
        "dispatchCity": "Delhi",
        "dispatchZipcode": "110001",
        "dispatchState": "Delhi",
        "dispatchCountry": "India",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# Phase gate
# ---------------------------------------------------------------------


class TestPhaseGateChangesPullBehaviour(FrappeTestCase):
    """The SAME EE row produces different side-effects depending on
    customer_master_mode. Onboarding → accept-and-create; ERPNext-mastered
    → drift detection (no create)."""

    def setUp(self) -> None:
        _wipe_state()
        # Seed lookups so the onboarding-path Customer create can proceed.
        if not frappe.db.exists("EasyEcom Country", {"country_id": 1}):
            frappe.get_doc({
                "doctype": "EasyEcom Country", "country_id": 1,
                "country_name": "India", "code_2": "IN", "code_3": "IND",
                "default_currency_code": "INR",
            }).insert(ignore_permissions=True)

    def tearDown(self) -> None:
        _wipe_state()

    def test_onboarding_mode_creates(self) -> None:
        c_id = "8500001"
        row = _build_ee_row(
            c_id=8500001, companyname=f"{PREFIX}PHASE-ONB",
            gstNum="",  # URP → clean Customer create
        )
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ONBOARDING
        )
        self.assertEqual(out.status, "Mapped")
        self.assertEqual(out.operation, "created")
        # Customer + Addresses + Map row all exist.
        self.assertTrue(frappe.db.exists(
            "EasyEcom Customer Map", {"ee_c_id": c_id}
        ))
        self.assertIsNotNone(out.customer_docname)

    def test_erpnext_mastered_mode_drifts_new_customer(self) -> None:
        """Same input — but ERPNext-mastered → drift row, no Customer."""
        c_id = "8500002"
        row = _build_ee_row(
            c_id=8500002, companyname=f"{PREFIX}PHASE-EM",
            gstNum="",
        )
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        self.assertEqual(out.operation, "flagged")
        # Drift map row exists; NO Customer was created.
        m = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"ee_c_id": c_id},
            ["status", "erpnext_name"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Drift")
        self.assertFalse(m.erpnext_name)


# ---------------------------------------------------------------------
# Drift detection — existing mapped customer
# ---------------------------------------------------------------------


class TestDriftOnEdit(FrappeTestCase):
    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_changed_customer_name_drifts_not_overwrites(self) -> None:
        c_name, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}EDIT-CN-OLD", ee_c_id="8510001"
        )
        # EE-side now has a different name.
        row = _build_ee_row(
            c_id=8510001, companyname=f"{PREFIX}EDIT-CN-NEW-EE-SIDE"
        )
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )

        self.assertEqual(out.status, "Drift")
        # ERPNext Customer NOT overwritten — name still old.
        self.assertEqual(
            frappe.db.get_value("Customer", c_name, "customer_name"),
            f"{PREFIX}EDIT-CN-OLD",
        )
        # Drift Fields child row recorded the diff.
        rows = frappe.db.get_all(
            "EasyEcom Item Map Drift Field",
            filters={"parent": map_name, "parenttype": "EasyEcom Customer Map"},
            fields=["field", "erpnext_value", "ee_value"],
        )
        names = {r.field: r for r in rows}
        self.assertIn("customer_name", names)
        self.assertEqual(names["customer_name"].erpnext_value, f"{PREFIX}EDIT-CN-OLD")
        self.assertEqual(names["customer_name"].ee_value, f"{PREFIX}EDIT-CN-NEW-EE-SIDE")

    def test_changed_billing_city_records_prefixed_label(self) -> None:
        """Address-level drift carries 'billing.city' label so the FDE
        can tell which doc differed."""
        _, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}EDIT-CITY", ee_c_id="8510002",
            billing={
                "address_line1": "Test Street", "city": "Delhi",
                "pincode": "110001", "state": "Delhi", "country": "India",
            },
        )
        row = _build_ee_row(c_id=8510002, billingCity="Bangalore")
        # NOTE: changing only billingCity but NOT billingState may trip
        # India Compliance's pincode-state check at pull time — but
        # drift detection NEVER inserts an Address, so IC isn't called.
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        rows = frappe.db.get_all(
            "EasyEcom Item Map Drift Field",
            filters={"parent": map_name, "parenttype": "EasyEcom Customer Map"},
            fields=["field", "erpnext_value", "ee_value"],
        )
        labels = {r.field for r in rows}
        self.assertIn("billing.city", labels)


class TestQuietRePullNoFlap(FrappeTestCase):
    """A pull with IDENTICAL EE data must leave the map row in Mapped —
    no spurious Drift → Mapped → Drift flapping."""

    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_identical_payload_stays_mapped(self) -> None:
        _, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}QUIET", ee_c_id="8520001",
            email="test@example.local",
            mobile="9999900001",
            gstin="",
        )
        row = _build_ee_row(
            c_id=8520001, companyname=f"{PREFIX}QUIET",
        )
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Mapped", "quiet re-pull must NOT flap to Drift")
        self.assertEqual(out.operation, "skipped")
        # Map row unchanged.
        self.assertEqual(
            frappe.db.get_value("EasyEcom Customer Map", map_name, "status"),
            "Mapped",
        )

    def test_quiet_repull_after_drift_clears_drift_table(self) -> None:
        """A previously-drifted row that becomes clean again must have
        its drift_fields table cleared (no ghost diffs)."""
        c_name, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}QUIET-RECLEAR", ee_c_id="8520002",
        )
        # First pull with a diff → records drift.
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        row_drift = _build_ee_row(
            c_id=8520002, companyname=f"{PREFIX}QUIET-RECLEAR-EE-NAME"
        )
        process_one_customer(
            row_drift, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        # Drift recorded.
        drift_rows_before = frappe.db.count(
            "EasyEcom Item Map Drift Field",
            filters={"parent": map_name},
        )
        self.assertGreater(drift_rows_before, 0)

        # Now EE-side fixes back to match ERPNext.
        row_clean = _build_ee_row(
            c_id=8520002, companyname=f"{PREFIX}QUIET-RECLEAR"
        )
        out = process_one_customer(
            row_clean, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Mapped")
        # Drift child rows cleared.
        self.assertEqual(
            frappe.db.count(
                "EasyEcom Item Map Drift Field",
                filters={"parent": map_name},
            ),
            0,
        )


# ---------------------------------------------------------------------
# EE-origin new customer post-flip
# ---------------------------------------------------------------------


class TestNewEeCustomerPostFlip(FrappeTestCase):
    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_no_customer_no_addresses_only_drift_map_row(self) -> None:
        """Post-flip pull encounters a brand-new EE c_id with NO prior
        map. Expected: Drift map row with no link, NO Customer + NO
        Address rows are created. ERPNext is steady-state authoritative."""
        row = _build_ee_row(c_id=8530001, companyname=f"{PREFIX}NEW-EE")
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        self.assertIsNone(out.customer_docname)
        # No Customer created.
        self.assertEqual(
            frappe.db.count(
                "Customer", filters={"customer_name": f"{PREFIX}NEW-EE"}
            ),
            0,
        )
        # Map row exists, status=Drift, no link.
        m = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"ee_c_id": "8530001"},
            ["status", "erpnext_name"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Drift")
        self.assertFalse(m.erpnext_name)


# ---------------------------------------------------------------------
# Field exclusion
# ---------------------------------------------------------------------


class TestFieldExclusion(FrappeTestCase):
    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_excluded_field_does_not_drift(self) -> None:
        """FDE marks customer_name in the exclude table; subsequent
        pulls don't re-flag drift on that field."""
        c_name, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}EXCL-OLD", ee_c_id="8540001"
        )
        # Add an exclude entry for customer_name.
        map_doc = frappe.get_doc("EasyEcom Customer Map", map_name)
        map_doc.append(
            "ecs_drift_exclude_fields",
            {"field": "customer_name", "reason": "FDE intentional rename"},
        )
        map_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # EE has a different name — but customer_name is excluded.
        row = _build_ee_row(
            c_id=8540001, companyname=f"{PREFIX}EXCL-OLD-EE-DIFFERS"
        )
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        # No drift recorded.
        self.assertEqual(out.status, "Mapped")
        diff_rows = frappe.db.get_all(
            "EasyEcom Item Map Drift Field",
            filters={"parent": map_name},
        )
        self.assertEqual(len(diff_rows), 0)

    def test_excluded_address_field_uses_prefixed_label(self) -> None:
        """Exclude 'billing.city' specifically — drift on billing.city
        is suppressed but dispatch.city would still drift."""
        _, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}EXCL-ADDR", ee_c_id="8540002"
        )
        map_doc = frappe.get_doc("EasyEcom Customer Map", map_name)
        map_doc.append(
            "ecs_drift_exclude_fields",
            {"field": "billing.city", "reason": "FDE intentional"},
        )
        map_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # EE has different billingCity (excluded) AND different
        # dispatchCity (not excluded).
        row = _build_ee_row(
            c_id=8540002,
            billingCity="Bangalore",   # excluded → no drift
            dispatchCity="Bangalore",  # not excluded → drift
        )
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        out = process_one_customer(
            row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED
        )
        self.assertEqual(out.status, "Drift")
        rows = frappe.db.get_all(
            "EasyEcom Item Map Drift Field",
            filters={"parent": map_name},
            fields=["field"],
        )
        labels = {r.field for r in rows}
        self.assertIn("dispatch.city", labels)
        self.assertNotIn("billing.city", labels)


# ---------------------------------------------------------------------
# Resolution actions
# ---------------------------------------------------------------------


class TestDismissDrift(FrappeTestCase):
    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_dismiss_resets_status_clears_child_rows(self) -> None:
        c_name, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}DISMISS", ee_c_id="8550001"
        )
        # Force into Drift by running a pull with diff.
        row = _build_ee_row(c_id=8550001, companyname=f"{PREFIX}DISMISS-EE")
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        process_one_customer(row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED)
        self.assertEqual(
            frappe.db.get_value("EasyEcom Customer Map", map_name, "status"),
            "Drift",
        )

        result = dismiss_drift(customer_map_name=map_name)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "Mapped")
        # Status reset; drift child rows cleared.
        self.assertEqual(
            frappe.db.get_value("EasyEcom Customer Map", map_name, "status"),
            "Mapped",
        )
        self.assertEqual(
            frappe.db.count(
                "EasyEcom Item Map Drift Field", filters={"parent": map_name}
            ),
            0,
        )
        # Underlying Customer NOT modified.
        self.assertEqual(
            frappe.db.get_value("Customer", c_name, "customer_name"),
            f"{PREFIX}DISMISS",  # not the EE-side value
        )

    def test_dismiss_rejects_non_drift_row(self) -> None:
        _, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}NONDRIFT", ee_c_id="8550002"
        )
        # Row is Mapped, not Drift.
        result = dismiss_drift(customer_map_name=map_name)
        self.assertFalse(result["ok"])
        self.assertIn("not in Drift", result["message"])

    def test_dismiss_rejects_unknown_map(self) -> None:
        result = dismiss_drift(customer_map_name="NOT-A-REAL-MAP-XYZZY")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"].lower())


class TestPushToEeForDrift(FrappeTestCase):
    """The drift resolution that re-asserts ERPNext as SoT."""

    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_push_dispatches_to_push_one_customer(self) -> None:
        """Drift row → push_to_ee_for_drift → push_one_customer is
        called with the linked Customer docname."""
        c_name, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}PUSHRE", ee_c_id="8560001"
        )
        # Set status Drift directly.
        frappe.db.set_value(
            "EasyEcom Customer Map", map_name, "status", "Drift",
            update_modified=False,
        )
        frappe.db.commit()

        with patch(
            "ecommerce_super.easyecom.flows.customer_push.push_one_customer"
        ) as mock_push:
            mock_push.return_value = MagicMock(
                operation="update", pushed=True,
                ee_customer_id="8560001", flag_reasons=[],
            )
            result = push_to_ee_for_drift(customer_map_name=map_name)

        self.assertTrue(result["ok"])
        mock_push.assert_called_once_with(c_name)

    def test_push_rejects_non_drift_row(self) -> None:
        _, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}PUSHRE-MAPPED", ee_c_id="8560002"
        )
        result = push_to_ee_for_drift(customer_map_name=map_name)
        self.assertFalse(result["ok"])
        self.assertIn("not in Drift", result["message"])

    def test_push_rejects_when_no_linked_customer(self) -> None:
        """EE-origin-new-post-flip rows have no linked Customer; the
        push action correctly refuses."""
        doc = frappe.get_doc({
            "doctype": "EasyEcom Customer Map",
            "ee_c_id": f"{PREFIX}8560003",
            "ee_customer_id": f"{PREFIX}8560003",
            "status": "Drift",
            "flag_reason": "EE-origin new post-flip",
        }).insert(ignore_permissions=True)

        result = push_to_ee_for_drift(customer_map_name=doc.name)
        self.assertFalse(result["ok"])
        self.assertIn("no linked Customer", result["message"])


# ---------------------------------------------------------------------
# No Accept-EE
# ---------------------------------------------------------------------


class TestNoAcceptEeAction(FrappeTestCase):
    """Per packet: post-flip drift has NO 'Accept EE Value' resolution.
    The only paths out of Drift are Dismiss (keep ERPNext) and Push
    (re-assert ERPNext). EE-side novelty/edits are never adopted in
    steady state."""

    def test_no_accept_ee_method_exists_in_module(self) -> None:
        import ecommerce_super.easyecom.flows.customer_pull as mod
        # Defensive: a future contributor adding an 'accept' method
        # should trip this test and trigger a design discussion.
        names = {n for n in dir(mod) if "accept" in n.lower() or "adopt" in n.lower()}
        self.assertEqual(
            names, set(),
            f"customer_pull.py has unexpected accept/adopt-style "
            f"identifiers ({names}); §8.2 post-flip contract is "
            f"'ERPNext wins; EE-side values are NOT adopted'."
        )


# ---------------------------------------------------------------------
# Sync Record Discrepancy
# ---------------------------------------------------------------------


class TestSyncRecordDiscrepancy(FrappeTestCase):
    """Drift outcome → Sync Record status = Discrepancy (not Failed).
    §7.3: divergence is not failure."""

    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_drift_writes_discrepancy_sync_record(self) -> None:
        c_name, map_name = _make_mapped_customer(
            customer_name=f"{PREFIX}DISCR", ee_c_id="8570001"
        )
        row = _build_ee_row(c_id=8570001, companyname=f"{PREFIX}DISCR-EE")
        executor = FieldMappingExecutor("EasyEcom-Customer-Pull")
        process_one_customer(row, executor=executor, account_mode=MODE_ERPNEXT_MASTERED)
        sr = frappe.db.get_value(
            "EasyEcom Sync Record",
            {"entity_doctype": "Customer", "entity_name": c_name, "direction": "Pull"},
            ["status", "last_error"],
            as_dict=True,
        )
        self.assertEqual(sr.status, "Discrepancy", "drift must NOT map to Failed")
        self.assertIsNotNone(sr.last_error)


# ---------------------------------------------------------------------
# Drift comparable set
# ---------------------------------------------------------------------


class TestDriftComparableFieldsContract(FrappeTestCase):
    """Lock the drift field set — adding/removing must be a deliberate
    decision (and tested again)."""

    def test_customer_level_comparable_fields(self) -> None:
        self.assertEqual(
            set(CUSTOMER_DRIFT_COMPARABLE_CUSTOMER_FIELDS),
            {"customer_name", "email_id", "mobile_no", "gstin", "default_currency"},
        )

    def test_address_level_comparable_fields(self) -> None:
        labels = {label for label, _, _ in CUSTOMER_DRIFT_COMPARABLE_ADDRESS_FIELDS}
        # 5 fields × 2 sides = 10
        self.assertEqual(len(labels), 10)
        for side in ("billing", "dispatch"):
            for fld in ("street", "city", "pincode", "state", "country"):
                self.assertIn(f"{side}.{fld}", labels)

    def test_internal_ids_NOT_compared(self) -> None:
        """ee_c_id / ee_customer_id (identity-management state) must
        NOT be drift-comparable — they're remap concerns, not drift."""
        for excluded in ("ee_c_id", "ee_customer_id", "c_id", "customerId"):
            self.assertNotIn(
                excluded, CUSTOMER_DRIFT_COMPARABLE_CUSTOMER_FIELDS
            )
            self.assertNotIn(
                excluded,
                {payload_key for _, payload_key, _ in CUSTOMER_DRIFT_COMPARABLE_ADDRESS_FIELDS},
            )
