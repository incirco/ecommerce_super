"""gh#149 — dry-run diagnostic endpoints for /einvoice/update and
/ewaybill/update.

Verifies:
  - Permission gate: only System Manager / EasyEcom FDE can invoke
  - Structured checklist returned, one entry per step
  - Missing SO → hard fail on step 1 (short-circuit)
  - Missing B2B Order Map → hard fail on step 2 (short-circuit)
  - Idempotent path: existing SI on Map → returns that, skips
    downstream simulation
  - Missing Customer Map / Item Map surface as specific-step failures
  - Successful simulation path returns "would create SI ..." with
    variance_pct, and DOES roll back (no persistent SI)
  - Savepoint rollback runs even when mirror throws
  - Eway prechecks fire only when include_eway=True
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.api import gsp_dry_run as mod


def _mock_so(name="SO-DRY-TEST", customer="CUST-A", grand_total=1050.0):
    """Build a MagicMock Sales Order suitable for dry-run stubbing."""
    so = MagicMock()
    so.name = name
    so.customer = customer
    so.grand_total = grand_total
    so.currency = "INR"
    so.transaction_date = "2026-07-16"
    so.taxes_and_charges = "Output GST In-state - MMPL"
    so.docstatus = 1
    so.items = [
        SimpleNamespace(
            idx=1, item_code="ITEM-A", qty=1, amount=1000.0,
            item_tax_template="GST 5% - MMPL",
        ),
    ]
    for f in ("transporter", "vehicle_no", "distance"):
        setattr(so, f, "SET")
    return so


def _mock_map(name="ECS-B2B-DRY", sales_order="SO-DRY-TEST", sales_invoice=None):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.status = "Pushed"
    m.sales_invoice = sales_invoice
    m.get = lambda field, default=None: {
        "sales_invoice": sales_invoice,
    }.get(field, default)
    m.ee_order_id = "EE-ORD-001"
    return m


def _allow(*_a, **_kw):
    """Bypass permission gate for tests — the gate is separately covered."""
    return None


class TestGh149PermissionGate(unittest.TestCase):
    """`frappe.session` is a _dict subclass — patch.object doesn't work
    reliably, so use direct set/restore in a small helper."""

    def _with_session_user(self, user: str, roles=None):
        """Set frappe.session.user for the test; optionally stub
        get_roles. Restores original after."""
        original = mod.frappe.session.get("user")
        mod.frappe.session.user = user
        get_roles_patch = None
        if roles is not None:
            get_roles_patch = patch.object(mod.frappe, "get_roles", return_value=roles)
            get_roles_patch.start()
        try:
            mod._require_dry_run_permission()
        finally:
            if get_roles_patch is not None:
                get_roles_patch.stop()
            if original is None:
                mod.frappe.session.pop("user", None)
            else:
                mod.frappe.session.user = original

    def test_administrator_always_passes(self):
        self._with_session_user("Administrator")  # no roles needed

    def test_system_manager_role_passes(self):
        self._with_session_user(
            "sm@x.com", roles=["System Manager", "Item Manager"],
        )

    def test_easyecom_fde_role_passes(self):
        self._with_session_user("fde@x.com", roles=["EasyEcom FDE"])

    def test_random_user_refused(self):
        with self.assertRaises(Exception) as ctx:
            self._with_session_user("sales@x.com", roles=["Sales User"])
        # frappe.throw serialises the message body (not the title) to str.
        msg = str(ctx.exception)
        self.assertIn("System Manager", msg)
        self.assertIn("EasyEcom FDE", msg)
        self.assertIn("Sales User", msg)  # actual roles surfaced


class TestGh149DryRunEinvoiceCheckSequence(unittest.TestCase):
    """Verify the checklist shape for representative scenarios."""

    def _run(self, *, so=None, map_doc=None, customer_map_hit="CUST-A",
             item_map_hit="ITEM-A", mirror_result=None, mirror_exc=None):
        """Invoke dry_run_einvoice with all Frappe side-effects mocked."""

        def _exists(doctype, name=None):
            if doctype == "Sales Order":
                return so is not None
            if doctype == "EasyEcom B2B Order Map":
                return map_doc is not None
            if doctype == "DocType":
                return True
            return False

        def _get_value(doctype, filters=None, field=None, **_kw):
            if doctype == "EasyEcom B2B Order Map":
                return map_doc.name if map_doc else None
            if doctype == "EasyEcom Customer Map":
                return customer_map_hit
            if doctype == "EasyEcom Item Map":
                return item_map_hit
            return None

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            return MagicMock()

        def _mirror(**_kw):
            if mirror_exc:
                raise mirror_exc
            return mirror_result or {
                "sales_invoice": "SI-DRY-001",
                "si_total": 1050.0,
                "variance_pct": 0.0,
            }

        # Bypass permission gate — separately tested.
        with (
            patch.object(mod, "_require_dry_run_permission", _allow),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe, "get_doc", side_effect=_get_doc),
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback"),
            patch.object(mod.frappe, "generate_hash", return_value="TESTHASH"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.mirror_si_from_ee_response",
                side_effect=_mirror,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror._resolve_customer",
                return_value=customer_map_hit,
            ),
        ):
            return mod.dry_run_einvoice(reference_code="SO-DRY-TEST")

    def test_missing_so_short_circuits(self):
        result = self._run(so=None)
        self.assertFalse(result["ok"])
        # Only step 1 executed
        self.assertEqual(len(result["checks"]), 1)
        self.assertEqual(result["checks"][0]["step"], "so_exists")
        self.assertFalse(result["checks"][0]["ok"])

    def test_missing_map_short_circuits(self):
        result = self._run(so=_mock_so(), map_doc=None)
        steps = [c["step"] for c in result["checks"]]
        self.assertEqual(steps, ["so_exists", "b2b_order_map"])
        self.assertFalse(result["ok"])

    def test_existing_si_on_map_returns_early_without_simulation(self):
        """Idempotency preview: real endpoint would return the existing
        SI; dry-run does the same without simulating SI insert."""
        result = self._run(
            so=_mock_so(),
            map_doc=_mock_map(sales_invoice="SI-EXISTING-001"),
        )
        steps = [c["step"] for c in result["checks"]]
        # so_exists → b2b_order_map → existing_si — then stop
        self.assertIn("existing_si", steps)
        self.assertNotIn("mirror_si_insert", steps)
        existing = next(c for c in result["checks"] if c["step"] == "existing_si")
        self.assertEqual(existing["sales_invoice"], "SI-EXISTING-001")

    def test_missing_customer_map_reports_specific_step_failure(self):
        result = self._run(
            so=_mock_so(),
            map_doc=_mock_map(),
            customer_map_hit=None,
        )
        buyer = next(c for c in result["checks"] if c["step"] == "buyer_resolution")
        self.assertFalse(buyer["ok"])
        self.assertIn("Customer Map", buyer["reason"])
        # Mirror insert should NOT be attempted when upstream failed
        mirror = next(c for c in result["checks"] if c["step"] == "mirror_si_insert")
        self.assertFalse(mirror["ok"])
        self.assertIn("Skipped", mirror["reason"])

    def test_missing_item_map_reports_per_line_detail(self):
        result = self._run(
            so=_mock_so(),
            map_doc=_mock_map(),
            item_map_hit=None,
        )
        item_step = next(c for c in result["checks"] if c["step"] == "item_map")
        self.assertFalse(item_step["ok"])
        self.assertEqual(len(item_step["items"]), 1)
        self.assertFalse(item_step["items"][0]["resolved"])
        self.assertEqual(item_step["items"][0]["item_code"], "ITEM-A")

    def test_successful_simulation_reports_would_create_si(self):
        result = self._run(so=_mock_so(), map_doc=_mock_map())
        self.assertTrue(result["ok"])
        mirror = next(c for c in result["checks"] if c["step"] == "mirror_si_insert")
        self.assertTrue(mirror["ok"])
        self.assertEqual(mirror["simulated_si_name"], "SI-DRY-001")
        self.assertIn("would create SI", mirror["note"])

    def test_mirror_exception_captured_not_propagated(self):
        result = self._run(
            so=_mock_so(),
            map_doc=_mock_map(),
            mirror_exc=RuntimeError("simulated mirror failure"),
        )
        # Endpoint returns cleanly (doesn't propagate the exception)
        self.assertFalse(result["ok"])
        mirror = next(c for c in result["checks"] if c["step"] == "mirror_si_insert")
        self.assertFalse(mirror["ok"])
        self.assertIn("RuntimeError", mirror["reason"])
        self.assertIn("simulated mirror failure", mirror["reason"])

    def test_rollback_called_even_on_mirror_success(self):
        """The savepoint MUST roll back on the success path too —
        otherwise dry-run leaves a real SI in the DB."""
        rollback_mock = MagicMock()

        def _exists(doctype, name=None):
            return True

        def _get_value(doctype, filters=None, field=None, **_kw):
            if doctype == "EasyEcom B2B Order Map":
                return "MAP-01"
            if doctype in ("EasyEcom Customer Map", "EasyEcom Item Map"):
                return "HIT"
            return None

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return _mock_so()
            if doctype == "EasyEcom B2B Order Map":
                return _mock_map()
            return MagicMock()

        with (
            patch.object(mod, "_require_dry_run_permission", _allow),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe, "get_doc", side_effect=_get_doc),
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback", side_effect=rollback_mock),
            patch.object(mod.frappe, "generate_hash", return_value="H"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.mirror_si_from_ee_response",
                return_value={
                    "sales_invoice": "SI-DRY-OK",
                    "si_total": 1050.0,
                    "variance_pct": 0.0,
                },
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror._resolve_customer",
                return_value="CUST",
            ),
        ):
            mod.dry_run_einvoice(reference_code="SO-DRY-TEST")
        rollback_mock.assert_called_once()

    def test_rollback_called_even_on_mirror_failure(self):
        rollback_mock = MagicMock()

        def _exists(doctype, name=None):
            return True

        def _get_value(doctype, filters=None, field=None, **_kw):
            if doctype == "EasyEcom B2B Order Map":
                return "MAP-01"
            if doctype in ("EasyEcom Customer Map", "EasyEcom Item Map"):
                return "HIT"
            return None

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return _mock_so()
            if doctype == "EasyEcom B2B Order Map":
                return _mock_map()
            return MagicMock()

        def _mirror_throws(**_kw):
            raise ValueError("mirror bad")

        with (
            patch.object(mod, "_require_dry_run_permission", _allow),
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe, "get_doc", side_effect=_get_doc),
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback", side_effect=rollback_mock),
            patch.object(mod.frappe, "generate_hash", return_value="H"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.mirror_si_from_ee_response",
                side_effect=_mirror_throws,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror._resolve_customer",
                return_value="CUST",
            ),
        ):
            mod.dry_run_einvoice(reference_code="SO-DRY-TEST")
        rollback_mock.assert_called_once()


class TestGh149DryRunEwaybill(unittest.TestCase):
    """Eway path — runs the einvoice checklist first, then adds eway
    prechecks. Does NOT actually mint eway (rolled-back SI can't be
    minted against)."""

    def _base_patches(self, *, so, map_doc, mirror_result=None):
        """Common patch stack for eway tests."""
        return [
            patch.object(mod, "_require_dry_run_permission", _allow),
            patch.object(mod.frappe.db, "exists", side_effect=lambda dt, n=None: (
                (dt == "Sales Order" and so is not None) or
                (dt == "EasyEcom B2B Order Map" and map_doc is not None) or
                (dt == "DocType")  # e-Waybill Log existence check
            )),
            patch.object(mod.frappe.db, "get_value", side_effect=lambda dt, f=None, field=None, **_kw: {
                "EasyEcom B2B Order Map": (map_doc.name if map_doc else None),
                "EasyEcom Customer Map": "CUST",
                "EasyEcom Item Map": "ITEM",
            }.get(dt)),
            patch.object(mod.frappe, "get_doc", side_effect=lambda dt, n=None: (
                so if dt == "Sales Order" else (map_doc if dt == "EasyEcom B2B Order Map" else MagicMock())
            )),
            patch.object(mod.frappe.db, "savepoint"),
            patch.object(mod.frappe.db, "rollback"),
            patch.object(mod.frappe, "generate_hash", return_value="H"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.mirror_si_from_ee_response",
                return_value=mirror_result or {
                    "sales_invoice": "SI-EWAY-DRY",
                    "si_total": 1050.0,
                    "variance_pct": 0.0,
                },
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror._resolve_customer",
                return_value="CUST",
            ),
        ]

    def _enter(self, contexts):
        for c in contexts:
            c.__enter__()

    def _exit(self, contexts):
        for c in contexts:
            c.__exit__(None, None, None)

    def test_eway_precheck_included_only_when_flag_set(self):
        so = _mock_so()
        map_doc = _mock_map()
        # First: dry_run_einvoice — no eway step
        ctxs = self._base_patches(so=so, map_doc=map_doc)
        self._enter(ctxs)
        try:
            einvoice_result = mod.dry_run_einvoice(reference_code="SO-DRY-TEST")
        finally:
            self._exit(ctxs)
        einvoice_steps = [c["step"] for c in einvoice_result["checks"]]
        self.assertNotIn("eway_precheck", einvoice_steps)

        # Second: dry_run_ewaybill — eway step included
        ctxs = self._base_patches(so=so, map_doc=map_doc)
        self._enter(ctxs)
        try:
            eway_result = mod.dry_run_ewaybill(reference_code="SO-DRY-TEST")
        finally:
            self._exit(ctxs)
        eway_steps = [c["step"] for c in eway_result["checks"]]
        self.assertIn("eway_precheck", eway_steps)

    def test_eway_precheck_reports_missing_transport_fields(self):
        so = _mock_so()
        so.transporter = None
        so.vehicle_no = None
        map_doc = _mock_map()
        ctxs = self._base_patches(so=so, map_doc=map_doc)
        self._enter(ctxs)
        try:
            result = mod.dry_run_ewaybill(reference_code="SO-DRY-TEST")
        finally:
            self._exit(ctxs)
        eway = next(c for c in result["checks"] if c["step"] == "eway_precheck")
        self.assertFalse(eway["ok"])
        self.assertIn("transporter", eway["reason"])
        self.assertIn("vehicle_no", eway["reason"])

    def test_eway_precheck_passes_when_all_fields_present(self):
        so = _mock_so()  # defaults have all transport fields set to "SET"
        map_doc = _mock_map()
        ctxs = self._base_patches(so=so, map_doc=map_doc)
        self._enter(ctxs)
        try:
            result = mod.dry_run_ewaybill(reference_code="SO-DRY-TEST")
        finally:
            self._exit(ctxs)
        eway = next(c for c in result["checks"] if c["step"] == "eway_precheck")
        self.assertTrue(eway["ok"])


if __name__ == "__main__":
    unittest.main()
