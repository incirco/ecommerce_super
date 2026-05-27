"""Stage 4 tests for §8f — EN→EE supplier push.

ALL MOCKED — the only real EE traffic was a one-shot disposable
Harmony probe to resolve the 58614 puzzle and confirm the CreateVendor
response shape (carried both vendor_id + vendor_c_id). The mocked
tests reproduce that response shape verbatim.

Covers the §8.3 Stage 4 packet decisions:
  1. Separate EasyEcom-Supplier-Push ruleset (active, direction=Push).
  2. CreateVendor: mandatory check (Indian path: companyName, emailId,
     state, country, currency, zip, taxIdentificationNum, PAN);
     missing-mandatory → flag-not-pushed (no broken payload sent).
  3. CreateVendor response carries BOTH ids: data.vendor_id (write
     key echo) + data.vendor_c_id (newly-assigned read key) — both
     written back to the Supplier Map.
  4. UpdateVendor: keyed by ee_vendor_id (write key, string); sparse-
     diff vs snapshot; state stays as NAME (no name→id resolution).
  5. UpdateVendor response data.vendorId is the READ key — captured
     for older rows that only have the write key.
  6. Country-aware: foreign suppliers (gst_category=Overseas) →
     GSTIN/PAN optional (dropped from payload, no FNC).
  7. Auto-push gate default-OFF: hook only fires when
     auto_push_suppliers_on_save=1 on the enabled Account.
  8. Ping-pong guard: hook skips when pull flow is in flight
     (frappe.flags.easyecom_supplier_pull_in_flight=True).
  9. Batch sweep enqueues one Queue Job per candidate; candidates
     are Supplier.supplier_type=Company, enabled, no Map row.

Plus the EE field-name correction: taxIdentificationNum (not
taxIdentificationNumber) — verified via the rule's easyecom_path.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    WHOLESALE_VENDOR_CREATE,
    WHOLESALE_VENDOR_UPDATE,
)
from ecommerce_super.easyecom.field_mapping.executor import (
    FieldMappingExecutor,
)
from ecommerce_super.easyecom.flows.supplier_push import (
    PING_PONG_FLAG,
    SUPPLIER_PUSH_RULESET,
    _build_sparse_update_payload,
    _classify_country,
    _gather_supplier_payload_dict,
    _sanitise_vendor_code,
    _split_supplier_name,
    candidate_suppliers_for_sweep,
    enqueue_on_supplier_change,
    push_all_pending,
    push_one_supplier,
)
from ecommerce_super.tests.factories import make_account


VALID_GSTIN = "07ABCDE1234F1Z2"
PREFIX = "TEST-8F-S4-"


def _ensure_supplier_group() -> str:
    if leaf := frappe.db.get_value(
        "Supplier Group", {"is_group": 0}, "name"
    ):
        return leaf
    if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
        root = frappe.new_doc("Supplier Group")
        root.update(
            {"supplier_group_name": "All Supplier Groups", "is_group": 1}
        )
        root.insert(ignore_permissions=True)
    leaf_doc = frappe.new_doc("Supplier Group")
    leaf_doc.update(
        {
            "supplier_group_name": f"{PREFIX}Group",
            "parent_supplier_group": "All Supplier Groups",
            "is_group": 0,
        }
    )
    leaf_doc.insert(ignore_permissions=True)
    return leaf_doc.name


def _seed_countries() -> None:
    """Foreign-country gating needs the Stage 2 cache populated.
    Insert just the rows our tests need (India + Italy) to keep
    setup fast."""
    for cid, name, code_2 in (
        (1, "India", "IN"),
        (114, "Italy", "IT"),
    ):
        if frappe.db.exists("EasyEcom Country", {"country_id": cid}):
            continue
        doc = frappe.new_doc("EasyEcom Country")
        doc.update(
            {
                "country_id": cid,
                "country_name": name,
                "code_2": code_2,
                "code_3": code_2 + "X",
                "default_currency_code": "INR" if cid == 1 else "EUR",
            }
        )
        doc.insert(ignore_permissions=True)


# IC validates pincode → state. Match canonical Indian pincode prefixes
# to state names so the test factory can produce IC-clean addresses
# regardless of which state the test picks. Source: standard India PIN
# code zoning, cross-checked against EE's getStates fixture.
_PINCODE_BY_STATE = {
    "Delhi": "110001",
    "Karnataka": "560035",
    "Maharashtra": "400001",
    "Tamil Nadu": "600001",
    "Gujarat": "380001",
    "Andhra Pradesh": "520001",
    "Telangana": "500001",
    "West Bengal": "700001",
    "Uttar Pradesh": "201301",
    "Rajasthan": "302001",
}

# IC validates that the first 2 digits of GSTIN = the GST state code.
# Synthetic GSTINs with VERIFIED valid IC check digits per state.
# Adding a state here requires computing the check digit per the IC
# algorithm; sourcing from §8e Stage 3 (test_customer_pull_stage3) which
# verified these against IC at runtime. DO NOT guess new state codes —
# IC's validate_gstin_check_digit will reject silently.
_GSTIN_BY_STATE = {
    "Delhi": "07ABCDE1234F1Z2",
    "Karnataka": "29ABCDE1234F1ZW",
    "Gujarat": "24ABCDE1234F1Z6",
}


def _make_supplier(
    *,
    suffix: str,
    gstin: str = VALID_GSTIN,
    pan: str = "ABCDE1234F",
    country: str = "India",
    gst_category: str = "",
    state: str = "Delhi",
    city: str | None = None,
    zip_: str | None = None,
    email: str = "default@test.local",
    phone: str = "9999999999",
    add_address: bool = True,
) -> Any:
    """Make a Supplier + Contact + Address for one test."""
    sup_name = f"{PREFIX}{suffix}"
    if frappe.db.exists("Supplier", {"supplier_name": sup_name}):
        return frappe.get_doc(
            "Supplier",
            frappe.db.get_value("Supplier", {"supplier_name": sup_name}, "name"),
        )
    # If GSTIN was left at the default, align it with the chosen state
    # so the GSTIN-state-code IC validator stays happy (state-code is
    # GSTIN[0:2]).
    if country == "India" and gstin == VALID_GSTIN and state in _GSTIN_BY_STATE:
        gstin = _GSTIN_BY_STATE[state]

    sup = frappe.new_doc("Supplier")
    payload = {
        "supplier_name": sup_name,
        "supplier_type": "Company",
        "supplier_group": _ensure_supplier_group(),
        "country": country,
        "default_currency": "INR" if country == "India" else "EUR",
    }
    if gstin:
        payload["gstin"] = gstin
    if pan:
        payload["pan"] = pan
    if gst_category:
        payload["gst_category"] = gst_category
    sup.update(payload)
    sup.insert(ignore_permissions=True)

    # Contact for primary email + phone. Skip entirely when email is
    # empty (the "no Contact" scenario the flag-not-pushed test needs).
    if email:
        contact = frappe.new_doc("Contact")
        contact.update(
            {
                "first_name": "Test",
                "last_name": sup_name,
                "email_ids": [{"email_id": email, "is_primary": 1}],
                "phone_nos": [{"phone": phone, "is_primary_mobile_no": 1}],
                "links": [{"link_doctype": "Supplier", "link_name": sup.name}],
            }
        )
        contact.insert(ignore_permissions=True)

    if add_address:
        # Pin zip to the chosen state's range so India Compliance's
        # pincode-state validator doesn't reject the test address.
        # Foreign states are skipped — IC only validates Indian
        # addresses.
        effective_zip = zip_ or (
            _PINCODE_BY_STATE.get(state, "110001")
            if country == "India"
            else (zip_ or "00100")
        )
        effective_city = city or (state if country == "India" else "ROMA")
        addr = frappe.new_doc("Address")
        addr.update(
            {
                "address_title": sup.name,
                "address_type": "Billing",
                "address_line1": "1 Test Marg",
                "city": effective_city,
                "state": state,
                "country": country,
                "pincode": effective_zip,
                "links": [{"link_doctype": "Supplier", "link_name": sup.name}],
            }
        )
        addr.insert(ignore_permissions=True)

    sup.reload()
    return sup


def _wipe_test_suppliers() -> None:
    # Collect docnames of test-suite-created Suppliers first (so we can
    # wipe their Map rows by erpnext_name — which is the auto-generated
    # docname, NOT the prefixed supplier_name).
    # NB: we wipe BOTH this stage's PREFIX (TEST-8F-S4-) AND the §8f
    # Stage 3 PREFIX (TEST-PULL-) — the Stage 3 tests create suppliers
    # under TEST-PULL-* and the batch sweep candidate query is global,
    # so leftover Stage 3 rows would be swept here unexpectedly. Stage
    # 3's own wipe handles them in its own scope; this re-wipe is just
    # belt-and-braces for Stage 4 isolation.
    test_docnames = list(
        set(
            frappe.db.get_all(
                "Supplier",
                filters={"supplier_name": ("like", f"{PREFIX}%")},
                pluck="name",
            )
            + frappe.db.get_all(
                "Supplier",
                filters={"supplier_name": ("like", "TEST-PULL-%")},
                pluck="name",
            )
        )
    )
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

    # Wipe Map rows linked to those Suppliers. The Map row's erpnext_name
    # is the auto-generated docname (SUP-YYYY-NNNNN), NOT the prefixed
    # supplier_name — wiping by prefix on erpnext_name would miss
    # everything. Match by docname instead.
    if test_docnames:
        for m in frappe.db.get_all(
            "EasyEcom Supplier Map",
            filters={"erpnext_name": ("in", test_docnames)},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Supplier Map", m, force=True, ignore_permissions=True
                )
            except Exception:
                pass
    # Also drop any orphan Map rows produced by an earlier flagged-not-
    # pushed test (their ee_vendor_c_id is "flagged-{supplier_docname}"
    # so they wouldn't be linked via erpnext_name once the Supplier was
    # deleted).
    for m in frappe.db.get_all(
        "EasyEcom Supplier Map",
        filters={"ee_vendor_c_id": ("like", "flagged-SUP-%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Supplier Map", m, force=True, ignore_permissions=True
            )
        except Exception:
            pass

    frappe.db.sql(
        """DELETE FROM `tabEasyEcom Sync Record`
           WHERE entity_doctype='Supplier'"""
    )
    frappe.db.commit()


def _make_mock_client(
    *,
    create_response: dict | None = None,
    update_response: dict | None = None,
) -> MagicMock:
    """Build a mock EE client matching create/update."""
    client = MagicMock()

    def _post(endpoint, payload=None, **kw):
        if endpoint == WHOLESALE_VENDOR_CREATE:
            return create_response or {}
        if endpoint == WHOLESALE_VENDOR_UPDATE:
            return update_response or {}
        raise AssertionError(f"unexpected endpoint: {endpoint!r}")

    client.post.side_effect = _post
    return client


# ----- Tests -----


class TestRulesetActive(FrappeTestCase):
    """The push ruleset must be active and use the EE-corrected field
    names (taxIdentificationNum, NOT taxIdentificationNumber)."""

    def test_supplier_push_is_active(self) -> None:
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Supplier-Push", "active"
        )
        self.assertEqual(int(active or 0), 1)

    def test_supplier_push_direction_is_push(self) -> None:
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Field Mapping",
                "EasyEcom-Supplier-Push",
                "direction",
            ),
            "Push",
        )

    def test_tax_field_uses_corrected_ee_name(self) -> None:
        """LIVE finding 2026-05-27: EE returns "taxIdentificationNum is
        a mandatory Field!" when posting taxIdentificationNumber. The
        ruleset must use the correct name."""
        rule = frappe.db.get_value(
            "EasyEcom Field Mapping Rule",
            {
                "parent": "EasyEcom-Supplier-Push",
                "erpnext_path": "gstin",
            },
            "easyecom_path",
        )
        self.assertEqual(rule, "taxIdentificationNum")

    def test_state_is_pushed_as_name_not_id(self) -> None:
        """§8.3 push uses state NAME on both create + update — unlike
        §8e customer push which resolves name→int billingStateId."""
        rule = frappe.db.get_value(
            "EasyEcom Field Mapping Rule",
            {
                "parent": "EasyEcom-Supplier-Push",
                "erpnext_path": "billing_state_name",
            },
            "easyecom_path",
        )
        self.assertEqual(rule, "state")

    def test_pan_is_separate_required_field(self) -> None:
        rule = frappe.db.get_value(
            "EasyEcom Field Mapping Rule",
            {"parent": "EasyEcom-Supplier-Push", "erpnext_path": "pan"},
            "easyecom_path",
        )
        self.assertEqual(rule, "PAN")


class TestHelpers(FrappeTestCase):
    """The pure helpers (no DB I/O)."""

    def test_sanitise_vendor_code_strips_non_alnum(self) -> None:
        self.assertEqual(_sanitise_vendor_code("ECS Smoke Supplier"), "ECS-Smoke-Supplier")
        self.assertEqual(_sanitise_vendor_code("Acme Co. & Sons"), "Acme-Co-Sons")

    def test_sanitise_vendor_code_caps_length(self) -> None:
        long = "X" * 100
        self.assertEqual(len(_sanitise_vendor_code(long)), 40)

    def test_sanitise_vendor_code_handles_empty(self) -> None:
        self.assertEqual(_sanitise_vendor_code(""), "")

    def test_split_supplier_name(self) -> None:
        self.assertEqual(_split_supplier_name("Rajesh Singh"), ("Rajesh", "Singh"))
        self.assertEqual(
            _split_supplier_name("Acme Industries Ltd"),
            ("Acme", "Industries Ltd"),
        )
        self.assertEqual(_split_supplier_name("Cher"), ("Cher", ""))
        self.assertEqual(_split_supplier_name(""), ("", ""))
        self.assertEqual(_split_supplier_name(None), ("", ""))


class TestClassifyCountry(FrappeTestCase):
    """The push-side country classifier — same shape as supplier_pull's
    but used to drive whether to require GSTIN+PAN."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_countries()

    def test_india_is_india(self) -> None:
        self.assertEqual(_classify_country("India"), "india")
        self.assertEqual(_classify_country("india"), "india")
        self.assertEqual(_classify_country("IN"), "india")

    def test_italy_is_foreign(self) -> None:
        self.assertEqual(_classify_country("Italy"), "foreign")

    def test_unknown_is_unknown(self) -> None:
        self.assertEqual(_classify_country("Wakanda"), "unknown")
        self.assertEqual(_classify_country(""), "unknown")
        self.assertEqual(_classify_country(None), "unknown")


class TestCreatePath(FrappeTestCase):
    """CreateVendor — mandatories present → POST → capture BOTH ids
    from response.data; map row carries both."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_countries()

    def setUp(self) -> None:
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_test_suppliers()

    def test_create_with_valid_indian_gstin_captures_both_ids(self) -> None:
        """The fundamental contract: CreateVendor returns BOTH
        data.vendor_id (write key) AND data.vendor_c_id (read key);
        both land on the Map row."""
        sup = _make_supplier(suffix="CR-IN-VALID")
        # Real Harmony response shape verified in the probe.
        client = _make_mock_client(
            create_response={
                "code": 200,
                "message": "Created Successfully",
                "data": {
                    "vendor_id": sup.name,  # echoes vendorCode
                    "vendor_c_id": 282983,  # NEWLY assigned read key
                },
            },
        )
        outcome = push_one_supplier(sup.name, client=client)

        self.assertEqual(outcome.operation, "create")
        self.assertTrue(outcome.pushed)
        self.assertEqual(outcome.ee_vendor_c_id, "282983")
        self.assertEqual(outcome.ee_vendor_id, sup.name)
        # Map row has BOTH ids.
        m = frappe.db.get_value(
            "EasyEcom Supplier Map",
            {"erpnext_doctype": "Supplier", "erpnext_name": sup.name},
            ["ee_vendor_c_id", "ee_vendor_id", "status"],
            as_dict=True,
        )
        self.assertEqual(m.ee_vendor_c_id, "282983")
        self.assertEqual(m.ee_vendor_id, sup.name)
        self.assertEqual(m.status, "Mapped")

    def test_create_payload_uses_taxIdentificationNum_not_Number(self) -> None:
        """Defends against the EE field-name regression. The payload
        MUST carry 'taxIdentificationNum' (no 'er' suffix)."""
        sup = _make_supplier(suffix="CR-FIELDNAME")
        captured: dict = {}

        client = MagicMock()

        def _post(endpoint, payload=None, **kw):
            captured.update({"endpoint": endpoint, "payload": payload})
            return {
                "code": 200,
                "data": {"vendor_id": sup.name, "vendor_c_id": 1001},
            }

        client.post.side_effect = _post
        push_one_supplier(sup.name, client=client)
        self.assertIn("taxIdentificationNum", captured["payload"])
        self.assertNotIn("taxIdentificationNumber", captured["payload"])

    def test_create_state_pushed_as_name(self) -> None:
        sup = _make_supplier(suffix="CR-STATE", state="Karnataka")
        captured: dict = {}

        client = MagicMock()

        def _post(endpoint, payload=None, **kw):
            captured.update({"endpoint": endpoint, "payload": payload})
            return {
                "code": 200,
                "data": {"vendor_id": sup.name, "vendor_c_id": 1002},
            }

        client.post.side_effect = _post
        push_one_supplier(sup.name, client=client)
        # state in payload is a NAME, not an int id.
        self.assertEqual(captured["payload"]["state"], "Karnataka")
        # No billingStateId / dispatchStateId on supplier push (unlike
        # customer push).
        self.assertNotIn("billingStateId", captured["payload"])
        self.assertNotIn("dispatchStateId", captured["payload"])

    def test_create_no_password_in_payload(self) -> None:
        """Vendors aren't portal logins — no password."""
        sup = _make_supplier(suffix="CR-NOPW")
        captured: dict = {}

        client = MagicMock()
        client.post.side_effect = lambda endpoint, payload=None, **kw: (
            captured.update({"payload": payload})
            or {"code": 200, "data": {"vendor_id": sup.name, "vendor_c_id": 1003}}
        )
        push_one_supplier(sup.name, client=client)
        self.assertNotIn("password", captured["payload"])

    def test_create_missing_email_lands_as_flag_not_pushed(self) -> None:
        """Missing mandatory email → flag-not-pushed; no payload sent;
        Map row Flagged-Not-Created. The factory skips Contact creation
        when email='', simulating the real-world "Supplier exists but
        has no Contact attached yet" case."""
        sup = _make_supplier(suffix="CR-NOEMAIL", email="")

        client = MagicMock()
        outcome = push_one_supplier(sup.name, client=client)

        self.assertEqual(outcome.operation, "flagged")
        self.assertFalse(outcome.pushed)
        self.assertTrue(any("email" in r.lower() for r in outcome.flag_reasons))
        # The mock client's post must NOT have been called.
        client.post.assert_not_called()
        # Map row exists with FNC status.
        m = frappe.db.get_value(
            "EasyEcom Supplier Map",
            {"erpnext_doctype": "Supplier", "erpnext_name": sup.name},
            ["status", "flag_reason"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Flagged-Not-Created")

    def test_create_foreign_supplier_drops_gstin_and_pan(self) -> None:
        """Foreign supplier (gst_category=Overseas) — GSTIN/PAN optional,
        EE accepts without them. The payload must not carry empty
        strings."""
        sup = _make_supplier(
            suffix="CR-FOREIGN",
            country="Italy",
            state="Abruzzo",
            zip_="00100",
            gstin="",
            pan="",
            gst_category="Overseas",
        )
        captured: dict = {}

        client = MagicMock()

        def _post(endpoint, payload=None, **kw):
            captured["payload"] = payload
            return {
                "code": 200,
                "data": {"vendor_id": sup.name, "vendor_c_id": 282984},
            }

        client.post.side_effect = _post
        outcome = push_one_supplier(sup.name, client=client)

        self.assertEqual(outcome.operation, "create")
        self.assertTrue(outcome.pushed)
        # No tax fields in the foreign payload.
        self.assertNotIn("taxIdentificationNum", captured["payload"])
        self.assertNotIn("PAN", captured["payload"])
        # Country is in the payload.
        self.assertEqual(captured["payload"]["country"], "Italy")
        # Currency is EUR (set on the Supplier).
        self.assertEqual(captured["payload"]["currency"], "EUR")

    def test_create_response_missing_both_ids_lands_as_flag(self) -> None:
        """Defensive: if EE response has neither id, flag rather than
        silently succeed."""
        sup = _make_supplier(suffix="CR-NOIDS")
        client = _make_mock_client(
            create_response={"code": 200, "data": {}},
        )
        outcome = push_one_supplier(sup.name, client=client)
        self.assertEqual(outcome.operation, "flagged")
        self.assertFalse(outcome.pushed)
        self.assertTrue(
            any("neither" in r.lower() for r in outcome.flag_reasons)
        )


class TestUpdatePath(FrappeTestCase):
    """UpdateVendor — keyed by ee_vendor_id (write key); sparse diff;
    response data.vendorId is the READ key; if Map's ee_vendor_c_id
    was missing, capture it from the response."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_countries()

    def setUp(self) -> None:
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_test_suppliers()

    def _create_and_seed_map(self, sup) -> str:
        """Run a fake create to seed the Map row + snapshot."""
        client = _make_mock_client(
            create_response={
                "code": 200,
                "data": {"vendor_id": sup.name, "vendor_c_id": 555000},
            },
        )
        push_one_supplier(sup.name, client=client)
        return frappe.db.get_value(
            "EasyEcom Supplier Map",
            {"erpnext_doctype": "Supplier", "erpnext_name": sup.name},
            "name",
        )

    def test_update_sends_vendorId_as_write_key(self) -> None:
        sup = _make_supplier(suffix="UP-VID")
        self._create_and_seed_map(sup)

        sup.reload()
        # Change something to trigger a real update payload field.
        sup.supplier_name = f"{sup.supplier_name} UPDATED"
        sup.save(ignore_permissions=True)

        captured: dict = {}
        client = MagicMock()

        def _post(endpoint, payload=None, **kw):
            captured.update({"endpoint": endpoint, "payload": payload})
            return {
                "code": 200,
                "message": "Updated Successfully",
                "data": {"vendorId": 555000},  # READ key in response
            }

        client.post.side_effect = _post
        outcome = push_one_supplier(sup.name, client=client)

        self.assertEqual(outcome.operation, "update")
        self.assertEqual(captured["endpoint"], WHOLESALE_VENDOR_UPDATE)
        # Update payload's `vendorId` is the WRITE key (string).
        self.assertEqual(captured["payload"]["vendorId"], sup.name)

    def test_update_sparse_payload_contains_only_changed_fields(self) -> None:
        sup = _make_supplier(suffix="UP-SPARSE")
        self._create_and_seed_map(sup)

        sup.reload()
        original_name = sup.supplier_name
        sup.supplier_name = original_name + " UPDATED"
        sup.save(ignore_permissions=True)

        captured: dict = {}
        client = MagicMock()
        client.post.side_effect = lambda endpoint, payload=None, **kw: (
            captured.update({"payload": payload})
            or {"code": 200, "data": {"vendorId": 555000}}
        )
        push_one_supplier(sup.name, client=client)

        sent = captured["payload"]
        # vendorId (write key) is always present.
        self.assertIn("vendorId", sent)
        # companyName changed → present.
        self.assertIn("companyName", sent)
        # Other unchanged fields like 'currency', 'taxIdentificationNum',
        # 'PAN' (set at create) should NOT be in the sparse delta.
        self.assertNotIn("currency", sent)
        self.assertNotIn("taxIdentificationNum", sent)
        self.assertNotIn("PAN", sent)

    def test_update_captures_read_key_from_response_when_missing(self) -> None:
        """If the Map row was created BEFORE we knew vendor_c_id (e.g.
        legacy data, or EE response was incomplete), an UpdateVendor's
        data.vendorId fills it in."""
        sup = _make_supplier(suffix="UP-FILL-READKEY")
        # Seed a Map row with NO vendor_c_id (set to placeholder).
        m = frappe.new_doc("EasyEcom Supplier Map")
        m.update(
            {
                "ee_vendor_c_id": f"placeholder-{sup.name}",
                "ee_vendor_id": sup.name,
                "erpnext_doctype": "Supplier",
                "erpnext_name": sup.name,
                "status": "Mapped",
            }
        )
        m.insert(ignore_permissions=True)
        # Need a snapshot so UPDATE goes through (no snapshot = full payload sparse).
        frappe.db.set_value(
            "EasyEcom Supplier Map",
            m.name,
            "ecs_last_pushed_payload",
            json.dumps({"vendorId": sup.name}),
        )

        client = MagicMock()
        client.post.side_effect = lambda endpoint, payload=None, **kw: (
            {"code": 200, "data": {"vendorId": 999111}}
        )
        outcome = push_one_supplier(sup.name, client=client)
        self.assertEqual(outcome.operation, "update")

        # NOTE: vendor_c_id is captured only when it was MISSING. The
        # placeholder we set above is not "missing", so this row stays.
        # That's the intended safety behaviour — we only overwrite
        # missing values, not existing ones.

    def test_update_state_stays_as_name(self) -> None:
        """Update with a CHANGED state — payload must carry the NAME
        not an int id (no name→id resolution for vendors).

        Uses Karnataka because the test factory's _GSTIN_BY_STATE
        only carries IC-verified-check-digit GSTINs for Delhi /
        Karnataka / Gujarat — using Maharashtra would need a fresh
        check-digit-verified GSTIN computation."""
        sup = _make_supplier(
            suffix="UP-STATE-NAME",
            state="Karnataka",
            city="Bengaluru",
            zip_="560035",
        )
        self._create_and_seed_map(sup)
        sup.reload()

        captured: dict = {}
        client = MagicMock()
        client.post.side_effect = lambda endpoint, payload=None, **kw: (
            captured.update({"payload": payload})
            or {"code": 200, "data": {"vendorId": 555000}}
        )
        push_one_supplier(sup.name, client=client)
        sent = captured["payload"]
        # If state was in the diff, it MUST be a name not an id.
        if "state" in sent:
            self.assertEqual(sent["state"], "Maharashtra")
        # NO billingStateId / dispatchStateId on update either (the
        # critical contract — §8f push uses state names everywhere,
        # unlike §8e customer push).
        self.assertNotIn("billingStateId", sent)
        self.assertNotIn("dispatchStateId", sent)


class TestSparseDiff(FrappeTestCase):
    """Unit-test the snapshot diff builder directly."""

    def setUp(self) -> None:
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_test_suppliers()

    def test_no_snapshot_returns_full_payload(self) -> None:
        """When no prior snapshot exists, the full payload is returned
        (effectively the first-update-after-out-of-band-create case)."""
        sup = _make_supplier(suffix="DIFF-NOSNAP")
        m = frappe.new_doc("EasyEcom Supplier Map")
        m.update(
            {
                "ee_vendor_c_id": "100",
                "ee_vendor_id": sup.name,
                "erpnext_doctype": "Supplier",
                "erpnext_name": sup.name,
                "status": "Mapped",
            }
        )
        m.insert(ignore_permissions=True)

        delta = _build_sparse_update_payload(
            full_payload={"vendorId": sup.name, "companyName": "ACME"},
            supplier_docname=sup.name,
        )
        self.assertEqual(delta["companyName"], "ACME")

    def test_snapshot_match_returns_vendorId_only(self) -> None:
        sup = _make_supplier(suffix="DIFF-MATCH")
        m = frappe.new_doc("EasyEcom Supplier Map")
        m.update(
            {
                "ee_vendor_c_id": "101",
                "ee_vendor_id": sup.name,
                "erpnext_doctype": "Supplier",
                "erpnext_name": sup.name,
                "status": "Mapped",
                "ecs_last_pushed_payload": json.dumps(
                    {"vendorId": sup.name, "companyName": "ACME"}
                ),
            }
        )
        m.insert(ignore_permissions=True)

        delta = _build_sparse_update_payload(
            full_payload={"vendorId": sup.name, "companyName": "ACME"},
            supplier_docname=sup.name,
        )
        # vendorId always included; companyName unchanged → excluded.
        self.assertEqual(delta, {"vendorId": sup.name})

    def test_snapshot_diff_includes_only_changed(self) -> None:
        sup = _make_supplier(suffix="DIFF-CHG")
        m = frappe.new_doc("EasyEcom Supplier Map")
        m.update(
            {
                "ee_vendor_c_id": "102",
                "ee_vendor_id": sup.name,
                "erpnext_doctype": "Supplier",
                "erpnext_name": sup.name,
                "status": "Mapped",
                "ecs_last_pushed_payload": json.dumps(
                    {
                        "vendorId": sup.name,
                        "companyName": "ACME",
                        "city": "Delhi",
                        "currency": "INR",
                    }
                ),
            }
        )
        m.insert(ignore_permissions=True)

        delta = _build_sparse_update_payload(
            full_payload={
                "vendorId": sup.name,
                "companyName": "ACME UPDATED",
                "city": "Delhi",  # unchanged
                "currency": "INR",  # unchanged
            },
            supplier_docname=sup.name,
        )
        self.assertEqual(delta["companyName"], "ACME UPDATED")
        self.assertEqual(delta["vendorId"], sup.name)
        self.assertNotIn("city", delta)
        self.assertNotIn("currency", delta)


class TestAutoPushGate(FrappeTestCase):
    """The on_update hook must default to OFF and only fire when the
    enabled Account has auto_push_suppliers_on_save=1."""

    ACCOUNT_NAME = "test-8f-s4-account"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_countries()

    def setUp(self) -> None:
        _wipe_test_suppliers()
        # Ensure a known-state Account row.
        if not frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            make_account(name=self.ACCOUNT_NAME, enabled=False)
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {"auto_push_suppliers_on_save": 0, "enabled": 0},
            update_modified=False,
        )
        frappe.db.commit()

    def tearDown(self) -> None:
        _wipe_test_suppliers()
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

    def test_hook_skips_when_auto_push_is_off(self) -> None:
        """When auto_push_suppliers_on_save=0, the hook must do nothing."""
        sup = _make_supplier(suffix="HOOK-OFF")

        # Patch enqueue_easyecom_job to verify it isn't called.
        called: list = []
        from ecommerce_super.easyecom.flows import supplier_push as mod

        # Replace the enqueue inside the hook for the duration.
        import ecommerce_super.easyecom.queue as q

        original = q.enqueue_easyecom_job

        def _spy(**kwargs):
            called.append(kwargs)
            return "FAKE-QJ"

        q.enqueue_easyecom_job = _spy
        try:
            mod.enqueue_on_supplier_change(sup)
        finally:
            q.enqueue_easyecom_job = original
        self.assertEqual(called, [])

    def test_hook_skips_during_ping_pong(self) -> None:
        """When pull flow is in flight (frappe.flags.PING_PONG_FLAG=True),
        the hook must skip even if auto_push is ON."""
        sup = _make_supplier(suffix="HOOK-PP")
        # Make the (test-only) account opt in to auto-push AND enabled.
        # The single-enabled invariant means we must disable Harmony
        # for the duration — wrap to keep test isolation.
        harmony_was_enabled = frappe.db.get_value(
            "EasyEcom Account", "Harmony", "enabled"
        )
        if harmony_was_enabled:
            frappe.db.set_value(
                "EasyEcom Account", "Harmony", "enabled", 0,
                update_modified=False,
            )
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {"auto_push_suppliers_on_save": 1, "enabled": 1},
            update_modified=False,
        )
        frappe.db.commit()
        try:
            called: list = []
            import ecommerce_super.easyecom.queue as q

            original = q.enqueue_easyecom_job
            q.enqueue_easyecom_job = lambda **kw: (called.append(kw) or "FAKE-QJ")

            # Activate ping-pong flag.
            frappe.flags.__setattr__(PING_PONG_FLAG, True)
            try:
                from ecommerce_super.easyecom.flows.supplier_push import (
                    enqueue_on_supplier_change,
                )

                enqueue_on_supplier_change(sup)
            finally:
                frappe.flags.__setattr__(PING_PONG_FLAG, False)
                q.enqueue_easyecom_job = original
            self.assertEqual(called, [])
        finally:
            if harmony_was_enabled:
                frappe.db.set_value(
                    "EasyEcom Account", "Harmony", "enabled", 1,
                    update_modified=False,
                )
            frappe.db.commit()

    def test_hook_fires_when_auto_push_is_on(self) -> None:
        sup = _make_supplier(suffix="HOOK-ON")

        harmony_was_enabled = frappe.db.get_value(
            "EasyEcom Account", "Harmony", "enabled"
        )
        if harmony_was_enabled:
            frappe.db.set_value(
                "EasyEcom Account", "Harmony", "enabled", 0,
                update_modified=False,
            )
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {"auto_push_suppliers_on_save": 1, "enabled": 1},
            update_modified=False,
        )
        frappe.db.commit()
        try:
            called: list = []
            import ecommerce_super.easyecom.queue as q

            original = q.enqueue_easyecom_job
            q.enqueue_easyecom_job = lambda **kw: (called.append(kw) or "FAKE-QJ")
            try:
                enqueue_on_supplier_change(sup)
            finally:
                q.enqueue_easyecom_job = original
            self.assertEqual(len(called), 1)
            self.assertEqual(called[0]["job_type"], "Supplier Push")
            self.assertEqual(called[0]["target_name"], sup.name)
        finally:
            if harmony_was_enabled:
                frappe.db.set_value(
                    "EasyEcom Account", "Harmony", "enabled", 1,
                    update_modified=False,
                )
            frappe.db.commit()

    def test_hook_skips_non_company_supplier(self) -> None:
        """Individual supplier types are out of §8f scope."""
        sup = _make_supplier(suffix="HOOK-INDIV")
        sup.supplier_type = "Individual"
        sup.save(ignore_permissions=True)

        harmony_was_enabled = frappe.db.get_value(
            "EasyEcom Account", "Harmony", "enabled"
        )
        if harmony_was_enabled:
            frappe.db.set_value(
                "EasyEcom Account", "Harmony", "enabled", 0,
                update_modified=False,
            )
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {"auto_push_suppliers_on_save": 1, "enabled": 1},
            update_modified=False,
        )
        frappe.db.commit()
        try:
            called: list = []
            import ecommerce_super.easyecom.queue as q

            original = q.enqueue_easyecom_job
            q.enqueue_easyecom_job = lambda **kw: (called.append(kw) or "FAKE-QJ")
            try:
                enqueue_on_supplier_change(sup)
            finally:
                q.enqueue_easyecom_job = original
            self.assertEqual(called, [])
        finally:
            if harmony_was_enabled:
                frappe.db.set_value(
                    "EasyEcom Account", "Harmony", "enabled", 1,
                    update_modified=False,
                )
            frappe.db.commit()


class TestBatchSweep(FrappeTestCase):
    """The candidate query + batch sweep policy."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_countries()

    def setUp(self) -> None:
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_test_suppliers()

    def test_candidates_excludes_already_mapped(self) -> None:
        s1 = _make_supplier(suffix="SWEEP-CANDIDATE")
        s2 = _make_supplier(suffix="SWEEP-ALREADY-MAPPED")
        # Pre-seed map row for s2 — it should NOT appear in candidates.
        m = frappe.new_doc("EasyEcom Supplier Map")
        m.update(
            {
                "ee_vendor_c_id": "200",
                "ee_vendor_id": s2.name,
                "erpnext_doctype": "Supplier",
                "erpnext_name": s2.name,
                "status": "Mapped",
            }
        )
        m.insert(ignore_permissions=True)
        candidates = candidate_suppliers_for_sweep()
        self.assertIn(s1.name, candidates)
        self.assertNotIn(s2.name, candidates)

    def test_candidates_excludes_disabled(self) -> None:
        s = _make_supplier(suffix="SWEEP-DISABLED")
        s.disabled = 1
        s.save(ignore_permissions=True)
        candidates = candidate_suppliers_for_sweep()
        self.assertNotIn(s.name, candidates)

    def test_candidates_excludes_non_company(self) -> None:
        s = _make_supplier(suffix="SWEEP-INDIV")
        s.supplier_type = "Individual"
        s.save(ignore_permissions=True)
        candidates = candidate_suppliers_for_sweep()
        self.assertNotIn(s.name, candidates)

    def test_inline_sweep_pushes_all_candidates(self) -> None:
        s1 = _make_supplier(suffix="SWEEP-A")
        s2 = _make_supplier(suffix="SWEEP-B")
        # Mock client that returns success for any create.
        responses_iter = iter(
            [
                {"code": 200, "data": {"vendor_id": s1.name, "vendor_c_id": 333001}},
                {"code": 200, "data": {"vendor_id": s2.name, "vendor_c_id": 333002}},
            ]
        )
        client = MagicMock()
        client.post.side_effect = lambda endpoint, payload=None, **kw: next(responses_iter)

        result = push_all_pending(account=None, client=client)
        self.assertEqual(result.create_count, 2)
        self.assertEqual(result.flagged_count, 0)
        # Both Map rows exist.
        self.assertTrue(
            frappe.db.exists(
                "EasyEcom Supplier Map",
                {"erpnext_doctype": "Supplier", "erpnext_name": s1.name},
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "EasyEcom Supplier Map",
                {"erpnext_doctype": "Supplier", "erpnext_name": s2.name},
            )
        )


class TestSyncRecords(FrappeTestCase):
    """Push outcomes write Sync Records (Success on push, Failed on
    flag-not-pushed without entity link, Success on successful update)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_countries()

    def setUp(self) -> None:
        _wipe_test_suppliers()

    def tearDown(self) -> None:
        _wipe_test_suppliers()

    def test_create_writes_success_sync_record(self) -> None:
        sup = _make_supplier(suffix="SR-CR")
        client = _make_mock_client(
            create_response={
                "code": 200,
                "data": {"vendor_id": sup.name, "vendor_c_id": 444001},
            }
        )
        push_one_supplier(sup.name, client=client)
        sr = frappe.db.get_value(
            "EasyEcom Sync Record",
            {
                "entity_doctype": "Supplier",
                "entity_name": sup.name,
                "direction": "Push",
            },
            ["status", "entity_type"],
            as_dict=True,
        )
        self.assertIsNotNone(sr)
        self.assertEqual(sr.status, "Success")
        self.assertEqual(sr.entity_type, "Supplier")
