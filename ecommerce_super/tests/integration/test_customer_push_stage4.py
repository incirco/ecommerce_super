"""Stage 4 tests for §8e — EN→EE customer push (CreateCustomer + UpdateCustomer).

ALL MOCKED — no real EE traffic in this test module. (The live
c_id==customerId verification was done during the Stage 4 build and
is documented in the closeout report; the answer is stored in the
production code, not re-run on every test invocation.)

Covers:
  - Endpoint registration (foundational + query-strip)
  - Ruleset registration (active=1, version 1)
  - candidate_customers_for_sweep policy (Company + email + no map row)
  - push_one_customer create path: missing-mandatory → flag-not-pushed
  - push_one_customer create path: state name → int id (Stage 2 resolver)
  - push_one_customer create path: random password manufactured
  - push_one_customer create path: customerId writeback from c_id key
  - push_one_customer update path: sparse payload + snapshot
  - push_one_customer update path: state as NAME (no resolution)
  - auto-push hook OFF by default (no enqueue)
  - auto-push hook ping-pong guard (suppressed during pull)
  - URP substitution when gst_category=Unregistered
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.client.endpoints import (
    FOUNDATIONAL_ENDPOINTS,
    WHOLESALE_CUSTOMER_CREATE,
    WHOLESALE_CUSTOMER_UPDATE,
    is_foundational,
)
from ecommerce_super.easyecom.flows.customer_push import (
    PING_PONG_FLAG,
    candidate_customers_for_sweep,
    enqueue_on_customer_change,
    push_one_customer,
)


PREFIX = "TEST-8E-S4-"
TEST_ACCOUNT = "test-8e-stage4"


# ---------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------


def _ensure_customer_group_and_territory() -> tuple[str, str]:
    """Find / make a leaf Customer Group and Territory for test customers."""
    cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    if not cg:
        if not frappe.db.exists("Customer Group", "All Customer Groups"):
            frappe.get_doc({
                "doctype": "Customer Group",
                "customer_group_name": "All Customer Groups",
                "is_group": 1,
            }).insert(ignore_permissions=True)
        cg_doc = frappe.get_doc({
            "doctype": "Customer Group",
            "customer_group_name": "TEST-8E-S4-CG",
            "parent_customer_group": "All Customer Groups",
            "is_group": 0,
        }).insert(ignore_permissions=True)
        cg = cg_doc.name

    t = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if not t:
        if not frappe.db.exists("Territory", "All Territories"):
            frappe.get_doc({
                "doctype": "Territory",
                "territory_name": "All Territories",
                "is_group": 1,
            }).insert(ignore_permissions=True)
        t_doc = frappe.get_doc({
            "doctype": "Territory",
            "territory_name": "TEST-8E-S4-T",
            "parent_territory": "All Territories",
            "is_group": 0,
        }).insert(ignore_permissions=True)
        t = t_doc.name
    return cg, t


def _seed_lookups() -> None:
    """Seed EasyEcom Country + State with India + Delhi entries so the
    state_resolver name→id lookup works in tests. Use direct inserts
    rather than running the discover flow to keep tests fast."""
    if not frappe.db.exists("EasyEcom Country", {"country_id": 1}):
        frappe.get_doc({
            "doctype": "EasyEcom Country",
            "country_id": 1,
            "country_name": "India",
            "code_2": "IN",
            "code_3": "IND",
            "default_currency_code": "INR",
        }).insert(ignore_permissions=True)
    country_docname = frappe.db.get_value(
        "EasyEcom Country", {"country_id": 1}, "name"
    )
    # Delhi (id 30, range 11-11) and Karnataka (id 12, range 56-59).
    for sid, sname, start, end in (
        (30, "Delhi", 11, 0),
        (12, "Karnataka", 56, 59),
    ):
        if not frappe.db.exists("EasyEcom State", {"state_id": sid}):
            frappe.get_doc({
                "doctype": "EasyEcom State",
                "state_id": sid,
                "state_name": sname,
                "country": country_docname,
                "country_id": 1,
                "is_union_territory": 0,
                "zip_start_range": start,
                "zip_end_range": end,
                "postal_code": sname[:2].upper(),
                "zone": "North" if sid == 30 else "South",
            }).insert(ignore_permissions=True)


def _make_customer(
    name: str,
    *,
    customer_type: str = "Company",
    email: str = "test@example.local",
    mobile: str = "9999900001",
    gstin: str = "",
    gst_category: str = "Unregistered",
    with_addresses: bool = True,
) -> str:
    """Insert a test Customer + optional Billing/Shipping Addresses.
    Returns the auto-generated docname."""
    cg, t = _ensure_customer_group_and_territory()
    cust = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": name,
        "customer_type": customer_type,
        "customer_group": cg,
        "territory": t,
        "email_id": email,
        "mobile_no": mobile,
        "gstin": gstin,
        "gst_category": gst_category,
        "default_currency": "INR",
    }).insert(ignore_permissions=True)
    if with_addresses:
        # Delhi pincode 110001, gstin-state-code 07 if a gstin is set
        # (state code 07 = Delhi). Empty gstin → no state-code check.
        for atype in ("Billing", "Shipping"):
            frappe.get_doc({
                "doctype": "Address",
                "address_title": cust.name,
                "address_type": atype,
                "address_line1": "Test Street",
                "city": "Delhi",
                "pincode": "110001",
                "state": "Delhi",
                "country": "India",
                "links": [{
                    "link_doctype": "Customer", "link_name": cust.name,
                }],
            }).insert(ignore_permissions=True)
    return cust.name


def _wipe_state() -> None:
    """Clean up test rows. Order matters (FKs).

    Customer.name is an auto-generated docname (CUST-YYYY-NNNNN) — NOT
    the customer_name. So we resolve test Customer docnames by their
    customer_name LIKE prefix, then sweep Maps + Addresses + SRs by
    those docnames.
    """
    test_customer_docnames = frappe.db.get_all(
        "Customer",
        filters={"customer_name": ("like", f"{PREFIX}%")},
        pluck="name",
    )

    # Map rows that link to any test Customer (by erpnext_name docname).
    if test_customer_docnames:
        for n in frappe.db.get_all(
            "EasyEcom Customer Map",
            filters={"erpnext_name": ("in", test_customer_docnames)},
            pluck="name",
        ):
            try:
                frappe.delete_doc("EasyEcom Customer Map", n, force=True, ignore_permissions=True)
            except Exception: pass
    # Map rows whose ee_c_id matches the test prefix (FNC paths use a
    # 'flagged-<docname>' ee_c_id; pre-seeded UPD tests use plain ints).
    for n in frappe.db.get_all(
        "EasyEcom Customer Map",
        filters={"ee_c_id": ("like", f"%{PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc("EasyEcom Customer Map", n, force=True, ignore_permissions=True)
        except Exception: pass

    # Test Customer + linked Addresses
    for n in test_customer_docnames:
        for addr in frappe.db.sql(
            "SELECT DISTINCT parent FROM `tabDynamic Link` "
            "WHERE parenttype='Address' AND link_doctype='Customer' AND link_name=%s",
            (n,),
        ):
            try:
                frappe.delete_doc("Address", addr[0], force=True, ignore_permissions=True)
            except Exception: pass
        try:
            frappe.delete_doc("Customer", n, force=True, ignore_permissions=True)
        except Exception: pass

    # Sync Records linked to test customer docnames
    if test_customer_docnames:
        placeholders = ",".join(["%s"] * len(test_customer_docnames))
        frappe.db.sql(
            f"DELETE FROM `tabEasyEcom Sync Record` "
            f"WHERE entity_doctype='Customer' AND entity_name IN ({placeholders})",
            tuple(test_customer_docnames),
        )
    frappe.db.commit()


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


class TestEndpointAndRulesetRegistration(FrappeTestCase):
    def test_create_endpoint_is_foundational(self) -> None:
        self.assertIn(WHOLESALE_CUSTOMER_CREATE, FOUNDATIONAL_ENDPOINTS)
        self.assertTrue(is_foundational(WHOLESALE_CUSTOMER_CREATE))

    def test_update_endpoint_is_foundational(self) -> None:
        self.assertIn(WHOLESALE_CUSTOMER_UPDATE, FOUNDATIONAL_ENDPOINTS)
        self.assertTrue(is_foundational(WHOLESALE_CUSTOMER_UPDATE))

    def test_customer_push_ruleset_exists_and_active(self) -> None:
        active = frappe.db.get_value(
            "EasyEcom Field Mapping", "EasyEcom-Customer-Push", "active"
        )
        self.assertEqual(int(active or 0), 1)


class TestCandidateSweepPolicy(FrappeTestCase):
    """The which-items policy: Company + enabled + email + no map row."""

    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()

    def test_company_customer_with_email_no_map_is_candidate(self) -> None:
        c = _make_customer(f"{PREFIX}CANDIDATE-1")
        codes = candidate_customers_for_sweep()
        self.assertIn(c, codes)

    def test_individual_customer_excluded(self) -> None:
        c = _make_customer(f"{PREFIX}INDIV-1", customer_type="Individual")
        self.assertNotIn(c, candidate_customers_for_sweep())

    def test_no_email_excluded(self) -> None:
        c = _make_customer(f"{PREFIX}NOEMAIL-1", email="")
        self.assertNotIn(c, candidate_customers_for_sweep())

    def test_with_map_row_excluded(self) -> None:
        c = _make_customer(f"{PREFIX}MAPPED-1")
        frappe.get_doc({
            "doctype": "EasyEcom Customer Map",
            "ee_c_id": f"{PREFIX}MAPPED-1-CID",
            "ee_customer_id": f"{PREFIX}MAPPED-1-CID",
            "erpnext_doctype": "Customer",
            "erpnext_name": c,
            "status": "Mapped",
        }).insert(ignore_permissions=True)
        self.assertNotIn(c, candidate_customers_for_sweep())


class TestPushCreateMissingMandatory(FrappeTestCase):
    """Missing mandatories → flag-not-pushed (Customer Map row only;
    no broken EE payload sent)."""

    def setUp(self) -> None:
        _wipe_state()
        _seed_lookups()

    def tearDown(self) -> None:
        _wipe_state()

    def test_missing_email_flags(self) -> None:
        c = _make_customer(f"{PREFIX}NOEMAIL", email="")
        client = MagicMock()
        out = push_one_customer(c, client=client)

        self.assertEqual(out.operation, "flagged")
        self.assertFalse(out.pushed)
        self.assertTrue(any("email_id" in r for r in out.flag_reasons))
        # No EE call made.
        client.post.assert_not_called()
        # Map row carries the FNC.
        m = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"erpnext_doctype": "Customer", "erpnext_name": c},
            ["status", "flag_reason"],
            as_dict=True,
        )
        self.assertEqual(m.status, "Flagged-Not-Created")

    def test_missing_mobile_flags(self) -> None:
        """EE requires contactNumber in practice (Harmony rejected with
        'Missing contact number at row 1' when omitted)."""
        c = _make_customer(f"{PREFIX}NOMOB", mobile="")
        client = MagicMock()
        out = push_one_customer(c, client=client)
        self.assertEqual(out.operation, "flagged")
        self.assertTrue(any("mobile_no" in r for r in out.flag_reasons))
        client.post.assert_not_called()

    def test_unresolvable_state_flags(self) -> None:
        """State name not in EasyEcom State cache → flagged. State
        resolver returns None when 'Wakanda' isn't seeded."""
        c = _make_customer(f"{PREFIX}BADSTATE")
        # Override the address state to something un-cached.
        addr_names = frappe.db.sql(
            "SELECT DISTINCT parent FROM `tabDynamic Link` "
            "WHERE parenttype='Address' AND link_doctype='Customer' AND link_name=%s",
            (c,), pluck=True,
        )
        for a in addr_names:
            frappe.db.set_value("Address", a, "state", "Wakanda", update_modified=False)
        frappe.db.commit()

        client = MagicMock()
        out = push_one_customer(c, client=client)
        self.assertEqual(out.operation, "flagged")
        self.assertTrue(
            any("Wakanda" in r and "not in EasyEcom State cache" in r
                for r in out.flag_reasons)
        )
        client.post.assert_not_called()


class TestPushCreateHappy(FrappeTestCase):
    """A clean Customer + Addresses → CreateCustomer call with all
    manufactured fields + state resolution + writeback."""

    def setUp(self) -> None:
        _wipe_state()
        _seed_lookups()

    def tearDown(self) -> None:
        _wipe_state()

    def test_create_payload_carries_manufactured_fields(self) -> None:
        c = _make_customer(f"{PREFIX}CREATE-1")
        client = MagicMock()
        client.post.return_value = {
            "code": 200,
            "message": "Customer created Successfully!",
            "data": {"c_id": 999000001},  # EE returns c_id, not customerId
        }
        out = push_one_customer(c, client=client)

        self.assertEqual(out.operation, "create")
        self.assertTrue(out.pushed)
        self.assertEqual(out.ee_customer_id, "999000001")
        client.post.assert_called_once()
        endpoint, _, kwargs = (
            client.post.call_args.args[0],
            client.post.call_args.args[1:],
            client.post.call_args.kwargs,
        )
        self.assertEqual(endpoint, WHOLESALE_CUSTOMER_CREATE)
        payload = kwargs["payload"]
        # Manufactured: random password (non-empty string).
        self.assertIn("password", payload)
        self.assertIsInstance(payload["password"], str)
        self.assertGreater(len(payload["password"]), 0)
        # State resolved to int id (Delhi = 30).
        self.assertEqual(payload["billingStateId"], 30)
        self.assertEqual(payload["dispatchStateId"], 30)
        # Update-only field NOT present.
        self.assertNotIn("billingState", payload)
        self.assertNotIn("dispatchState", payload)
        # Mandatory: companyName, email, contactNumber, currency, country.
        self.assertEqual(payload["companyName"], f"{PREFIX}CREATE-1")
        self.assertEqual(payload["email"], "test@example.local")
        self.assertEqual(payload["contactNumber"], "9999900001")
        self.assertEqual(payload["currency"], "INR")
        self.assertEqual(payload["country"], "India")
        # URP substitution for Unregistered Customer with empty gstin.
        self.assertEqual(payload["taxIdentificationNumber"], "URP")

    def test_writeback_uses_c_id_response_key(self) -> None:
        """EE's CreateCustomer returns data.c_id (not data.customerId).
        The map row gets the c_id value written to BOTH ee_c_id and
        ee_customer_id (Stage 4 finding: same value, two names)."""
        c = _make_customer(f"{PREFIX}CIDKEY")
        client = MagicMock()
        client.post.return_value = {
            "code": 200, "data": {"c_id": 12345678},
        }
        push_one_customer(c, client=client)
        m = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"erpnext_doctype": "Customer", "erpnext_name": c},
            ["ee_c_id", "ee_customer_id", "status"],
            as_dict=True,
        )
        self.assertEqual(m.ee_c_id, "12345678")
        self.assertEqual(m.ee_customer_id, "12345678")
        self.assertEqual(m.status, "Mapped")

    def test_legacy_customerid_response_key_fallback(self) -> None:
        """Defensive fallback if EE ever switches the response key
        from c_id to customerId."""
        c = _make_customer(f"{PREFIX}LEGACY")
        client = MagicMock()
        client.post.return_value = {
            "code": 200, "data": {"customerId": 87654321},
        }
        out = push_one_customer(c, client=client)
        self.assertEqual(out.ee_customer_id, "87654321")

    def test_create_saves_snapshot_for_next_update(self) -> None:
        c = _make_customer(f"{PREFIX}SNAP")
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {"c_id": 50000001}}
        push_one_customer(c, client=client)
        snap = frappe.db.get_value(
            "EasyEcom Customer Map",
            {"erpnext_doctype": "Customer", "erpnext_name": c},
            "ecs_last_pushed_payload",
        )
        self.assertIsNotNone(snap)
        decoded = json.loads(snap)
        self.assertEqual(decoded["companyName"], f"{PREFIX}SNAP")
        self.assertIn("billingStateId", decoded)


class TestPushUpdateSparse(FrappeTestCase):
    """Sparse update keyed on customerId (int); state as NAME on
    update (not id)."""

    def setUp(self) -> None:
        _wipe_state()
        _seed_lookups()

    def tearDown(self) -> None:
        _wipe_state()

    def _seed_mapped_customer(self, name: str, c_id: int = 50000099) -> str:
        c = _make_customer(name)
        # Pretend a prior Create happened.
        frappe.get_doc({
            "doctype": "EasyEcom Customer Map",
            "ee_c_id": str(c_id),
            "ee_customer_id": str(c_id),
            "erpnext_doctype": "Customer",
            "erpnext_name": c,
            "status": "Mapped",
            "ecs_last_pushed_payload": json.dumps({
                "companyName": name,
                "email": "test@example.local",
                "contactNumber": "9999900001",
                "currency": "INR",
                "country": "India",
                "billingStateId": 30,
                "dispatchStateId": 30,
                "billingPostalCode": 110001,
                "dispatchPostalCode": 110001,
                "billingStreet": "Test Street",
                "billingCity": "Delhi",
                "dispatchStreet": "Test Street",
                "dispatchCity": "Delhi",
                "taxIdentificationNumber": "URP",
                "customerId": c_id,
            }),
        }).insert(ignore_permissions=True)
        return c

    def test_update_routes_to_update_endpoint(self) -> None:
        c = self._seed_mapped_customer(f"{PREFIX}UPD-1")
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {}, "message": "ok"}
        out = push_one_customer(c, client=client)

        self.assertEqual(out.operation, "update")
        self.assertTrue(out.pushed)
        endpoint = client.post.call_args.args[0]
        self.assertEqual(endpoint, WHOLESALE_CUSTOMER_UPDATE)

    def test_update_payload_uses_state_name_not_id(self) -> None:
        c = self._seed_mapped_customer(f"{PREFIX}UPD-NAME")
        # Change something so the diff produces fields.
        frappe.db.set_value(
            "Customer", c, "customer_name", f"{PREFIX}UPD-NAME-RENAMED",
            update_modified=False,
        )
        frappe.db.commit()
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {}}
        push_one_customer(c, client=client)
        payload = client.post.call_args.kwargs["payload"]
        # Update path strips Create-only fields.
        self.assertNotIn("password", payload)
        self.assertNotIn("billingStateId", payload)
        self.assertNotIn("dispatchStateId", payload)
        # customerId is the wire key (int).
        self.assertEqual(payload["customerId"], 50000099)

    def test_update_is_sparse_against_snapshot(self) -> None:
        """Change only customer_name. The sparse payload should carry
        only customerId + companyName (not the whole 12-field set)."""
        c = self._seed_mapped_customer(f"{PREFIX}UPD-SPARSE")
        frappe.db.set_value(
            "Customer", c, "customer_name", f"{PREFIX}UPD-SPARSE-NEW",
            update_modified=False,
        )
        frappe.db.commit()
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {}}
        push_one_customer(c, client=client)
        payload = client.post.call_args.kwargs["payload"]

        # Only customerId + companyName (the changed field) should be in
        # the sparse payload.
        self.assertEqual(payload.get("customerId"), 50000099)
        self.assertEqual(payload.get("companyName"), f"{PREFIX}UPD-SPARSE-NEW")
        # Unchanged fields are absent.
        for unchanged in ("email", "contactNumber", "currency",
                          "billingPostalCode", "billingStreet"):
            self.assertNotIn(unchanged, payload)


class TestAutoPushHook(FrappeTestCase):
    """The auto-push hook is GATED by:
      - auto_push_customers_on_save=1 on an enabled Account (default 0)
      - frappe.flags.easyecom_customer_pull_in_flight is False (no ping-pong)
      - customer_type=Company (§8e is wholesale only)
    """

    def setUp(self) -> None:
        _wipe_state()

    def tearDown(self) -> None:
        _wipe_state()
        # Always reset the ping-pong flag (request-local but test isolation).
        frappe.flags.__setattr__(PING_PONG_FLAG, False)

    def test_hook_skips_when_no_account_has_auto_push_enabled(self) -> None:
        """Default state: no account has auto-push on → no enqueue."""
        c_name = _make_customer(f"{PREFIX}HOOK-OFF")
        doc = frappe.get_doc("Customer", c_name)
        with patch(
            "ecommerce_super.easyecom.queue.enqueue_easyecom_job"
        ) as mock_enqueue:
            enqueue_on_customer_change(doc, method="on_update")
            mock_enqueue.assert_not_called()

    def test_hook_skips_when_ping_pong_flag_set(self) -> None:
        """The pull flow sets this flag while creating Customers — the
        push hook must skip during pull to avoid a re-push echo."""
        c_name = _make_customer(f"{PREFIX}HOOK-PP")
        doc = frappe.get_doc("Customer", c_name)
        frappe.flags.__setattr__(PING_PONG_FLAG, True)
        try:
            with patch(
                "ecommerce_super.easyecom.queue.enqueue_easyecom_job"
            ) as mock_enqueue:
                enqueue_on_customer_change(doc, method="on_update")
                mock_enqueue.assert_not_called()
        finally:
            frappe.flags.__setattr__(PING_PONG_FLAG, False)

    def test_hook_skips_individual_customer_type(self) -> None:
        """customer_type=Individual is out of §8e scope."""
        c_name = _make_customer(f"{PREFIX}HOOK-INDIV", customer_type="Individual")
        doc = frappe.get_doc("Customer", c_name)
        with patch(
            "ecommerce_super.easyecom.queue.enqueue_easyecom_job"
        ) as mock_enqueue:
            enqueue_on_customer_change(doc, method="on_update")
            mock_enqueue.assert_not_called()


class TestUrpSubstitution(FrappeTestCase):
    """gst_category='Unregistered' + empty gstin → taxIdentificationNumber
    sent as 'URP' on Create (EE's sentinel)."""

    def setUp(self) -> None:
        _wipe_state()
        _seed_lookups()

    def tearDown(self) -> None:
        _wipe_state()

    def test_urp_substitution(self) -> None:
        c = _make_customer(
            f"{PREFIX}URP", gstin="", gst_category="Unregistered"
        )
        client = MagicMock()
        client.post.return_value = {"code": 200, "data": {"c_id": 600001}}
        push_one_customer(c, client=client)
        payload = client.post.call_args.kwargs["payload"]
        self.assertEqual(payload["taxIdentificationNumber"], "URP")
