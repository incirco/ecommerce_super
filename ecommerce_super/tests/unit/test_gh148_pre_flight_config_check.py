"""gh#148 — pre-flight config validation on EasyEcom Account.

The pre-flight checklist runs when `self.enabled == 1`:
  Hard blockers throw (refuse save).
  Soft warnings post as a timeline Comment (don't block save).

Also callable read-only via the whitelisted `config_check(account)`
endpoint so a workspace shortcut can preview blockers/warnings
without saving.

Tests verify:
  - Disabled accounts skip pre-flight entirely (silent-inert preserved).
  - Every hard blocker fires with a message pointing at the specific gap.
  - Every soft warning fires as a Comment when triggered.
  - config_check returns structured findings without side-effects.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.doctype.easyecom_account import easyecom_account as mod


def _mock_account(**overrides):
    """Build a MagicMock EasyEcomAccount-like object suitable for
    `_collect_pre_flight_findings`. Overrides drive individual test
    conditions. Defaults represent a healthy Account."""
    a = MagicMock()
    a.name = "EE-ACC-TEST"
    a.enabled = 1
    a.ecs_b2b_module = "New B2B"
    a.gsp_mint_einvoice = 1
    a._has_credential = MagicMock(return_value=True)
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


class TestGh148PreFlightBlockers(unittest.TestCase):
    """Each blocker must fire independently on its trigger condition."""

    def _run(self, *, account, live_locations=None, company_gstin=True,
             warehouse_has_state=True, warehouse_has_fk=True,
             unmapped_customers=0, item_map_count=1,
             ic_installed=True):
        """Call _collect_pre_flight_findings with all data-access
        helpers mocked to controllable values.

        `live_locations=None` uses a healthy default. Pass explicit
        `[]` to test the no-locations blocker (using `or` here would
        swap [] back to default because empty list is falsy).
        """
        if live_locations is None:
            live_locations = [{
                "name": "EE-LOC-01", "frappe_company": "MMPL",
                "mapped_warehouse": "WH-01",
            }]
        with (
            patch.object(mod, "_fetch_live_locations",
                         return_value=live_locations),
            patch.object(mod, "_company_gstin",
                         return_value=("29ABCDE1234F1Z5" if company_gstin else None)),
            patch.object(mod, "_warehouse_has_state",
                         return_value=warehouse_has_state),
            patch.object(mod, "_warehouse_has_ecs_ee_location_fk",
                         return_value=warehouse_has_fk),
            patch.object(mod, "_count_customers_without_ee_c_id",
                         return_value=unmapped_customers),
            patch.object(mod, "_item_map_count",
                         return_value=item_map_count),
            patch.object(mod, "_india_compliance_installed",
                         return_value=ic_installed),
        ):
            return mod._collect_pre_flight_findings(account)

    def test_all_healthy_no_blockers_no_warnings(self):
        blockers, warnings = self._run(account=_mock_account())
        self.assertEqual(blockers, [])
        self.assertEqual(warnings, [])

    def test_no_live_locations_blocks(self):
        blockers, _ = self._run(account=_mock_account(), live_locations=[])
        self.assertTrue(any(b["category"] == "location" for b in blockers))
        self.assertTrue(any("Live" in b["message"] for b in blockers))

    def test_location_without_company_blocks(self):
        blockers, _ = self._run(
            account=_mock_account(),
            live_locations=[{
                "name": "EE-LOC-01", "frappe_company": None,
                "mapped_warehouse": "WH-01",
            }],
        )
        self.assertTrue(any(b["category"] == "company" for b in blockers))
        self.assertTrue(any(
            "no Frappe Company" in b["message"] for b in blockers
        ))

    def test_company_without_gstin_blocks(self):
        blockers, _ = self._run(account=_mock_account(), company_gstin=False)
        self.assertTrue(any(b["category"] == "company" for b in blockers))
        self.assertTrue(any("GSTIN" in b["message"] for b in blockers))
        self.assertTrue(any("MMPL" in b["message"] for b in blockers))

    def test_ecs_b2b_module_without_basic_auth_secret_blocks(self):
        account = _mock_account()
        account._has_credential = MagicMock(return_value=False)
        blockers, _ = self._run(account=account)
        self.assertTrue(any(b["category"] == "gsp" for b in blockers))
        self.assertTrue(any(
            "gsp_basic_auth_secret" in b["message"] for b in blockers
        ))

    def test_ecs_b2b_module_empty_skips_gsp_check_silent_inert(self):
        """Accounts without ecs_b2b_module aren't §11-configured; the
        GSP secret check must not fire (preserves silent-inert path)."""
        account = _mock_account(ecs_b2b_module="")
        account._has_credential = MagicMock(return_value=False)
        blockers, _ = self._run(account=account)
        self.assertFalse(any(b["category"] == "gsp" for b in blockers))

    def test_gsp_mint_einvoice_without_ic_blocks(self):
        blockers, _ = self._run(account=_mock_account(), ic_installed=False)
        self.assertTrue(any(b["category"] == "ic" for b in blockers))
        self.assertTrue(any(
            "India Compliance" in b["message"] for b in blockers
        ))

    def test_gsp_mint_einvoice_off_skips_ic_check(self):
        """When mint is disabled, IC absence is not a blocker."""
        blockers, _ = self._run(
            account=_mock_account(gsp_mint_einvoice=0),
            ic_installed=False,
        )
        self.assertFalse(any(b["category"] == "ic" for b in blockers))

    def test_multiple_blockers_all_reported_not_short_circuited(self):
        """FDE fixes N problems per save cycle, not one at a time."""
        account = _mock_account()
        account._has_credential = MagicMock(return_value=False)
        blockers, _ = self._run(
            account=account,
            company_gstin=False,
            ic_installed=False,
        )
        categories = {b["category"] for b in blockers}
        # gsp, company, and ic all present
        self.assertIn("gsp", categories)
        self.assertIn("company", categories)
        self.assertIn("ic", categories)


class TestGh148PreFlightWarnings(unittest.TestCase):
    """Soft warnings — surfaced as Comments; don't block save."""

    def _run(self, **overrides):
        return TestGh148PreFlightBlockers._run(self, **overrides)  # reuse

    def test_warehouse_without_state_warns(self):
        _, warnings = self._run(account=_mock_account(), warehouse_has_state=False)
        self.assertTrue(any(
            w["category"] == "warehouse" and "state" in w["message"]
            for w in warnings
        ))

    def test_warehouse_without_ecs_ee_location_warns(self):
        _, warnings = self._run(account=_mock_account(), warehouse_has_fk=False)
        self.assertTrue(any(
            w["category"] == "warehouse" and "ecs_ee_location" in w["message"]
            for w in warnings
        ))

    def test_location_without_mapped_warehouse_warns(self):
        _, warnings = self._run(
            account=_mock_account(),
            live_locations=[{
                "name": "EE-LOC-02", "frappe_company": "MMPL",
                "mapped_warehouse": None,
            }],
        )
        self.assertTrue(any(
            w["category"] == "warehouse" and "mapped_warehouse" in w["message"]
            for w in warnings
        ))

    def test_unmapped_customers_warns_with_count(self):
        _, warnings = self._run(account=_mock_account(), unmapped_customers=7)
        matching = [w for w in warnings if w["category"] == "customer"]
        self.assertEqual(len(matching), 1)
        self.assertIn("7", matching[0]["message"])

    def test_zero_customers_without_ee_c_id_no_warning(self):
        _, warnings = self._run(account=_mock_account(), unmapped_customers=0)
        self.assertFalse(any(w["category"] == "customer" for w in warnings))

    def test_zero_item_maps_warns(self):
        _, warnings = self._run(account=_mock_account(), item_map_count=0)
        self.assertTrue(any(w["category"] == "item" for w in warnings))


class TestGh148ConfigCheckEndpoint(unittest.TestCase):
    """The @whitelist'd config_check endpoint — read-only preview."""

    def test_config_check_returns_structured_findings(self):
        fake_doc = _mock_account(enabled=1)
        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(mod.frappe, "get_doc", return_value=fake_doc),
            patch.object(mod, "_collect_pre_flight_findings", return_value=(
                [{"category": "gsp", "message": "test blocker"}],
                [{"category": "warehouse", "message": "test warning"}],
            )),
        ):
            result = mod.config_check("EE-ACC-TEST")
        self.assertEqual(result["account"], "EE-ACC-TEST")
        self.assertEqual(result["enabled"], 1)
        self.assertEqual(len(result["blockers"]), 1)
        self.assertEqual(result["blockers"][0]["category"], "gsp")
        self.assertEqual(len(result["warnings"]), 1)

    def test_config_check_refuses_without_read_permission(self):
        with (
            patch.object(mod.frappe, "has_permission", return_value=False),
            self.assertRaises(Exception) as ctx,
        ):
            mod.config_check("EE-ACC-TEST")
        # frappe.throw raises ValidationError-like exception; message
        # should mention "permitted".
        self.assertIn("permitted", str(ctx.exception).lower())

    def test_config_check_calls_helpers_read_only(self):
        """Endpoint must not mutate the doc — no save, no insert, no
        add_comment. The helper is pure-read."""
        fake_doc = _mock_account()
        with (
            patch.object(mod.frappe, "has_permission", return_value=True),
            patch.object(mod.frappe, "get_doc", return_value=fake_doc),
            patch.object(mod, "_collect_pre_flight_findings", return_value=([], [])),
        ):
            mod.config_check("EE-ACC-TEST")
        # No mutation methods invoked on the doc.
        fake_doc.save.assert_not_called()
        fake_doc.insert.assert_not_called()
        fake_doc.add_comment.assert_not_called()


class TestGh148DisabledAccountsSkipPreFlight(unittest.TestCase):
    """Disabled accounts (enabled=0) never invoke the pre-flight check.
    Silent-inert path — no throw, no comment, no db reads."""

    def test_validate_does_not_call_pre_flight_when_disabled(self):
        """Verifies the guard in validate() short-circuits before the
        pre-flight logic runs."""
        acc = MagicMock(spec=mod.EasyEcomAccount)
        acc.enabled = 0
        # Reroute other validate helpers to no-op so we isolate the
        # pre-flight guard behavior.
        for method_name in (
            "_validate_api_endpoint", "_validate_rate_limit_tier",
            "_clamp_throughput_to_tier", "_validate_webhook_config",
            "_warn_if_default_tier_in_production",
            "_update_webhook_endpoint_display",
            "_validate_single_enabled_account",
            "_run_pre_flight_config_checks",
        ):
            setattr(acc, method_name, MagicMock())
        mod.EasyEcomAccount.validate(acc)
        acc._run_pre_flight_config_checks.assert_not_called()

    def _validate_with_flag(self, *, in_test: bool):
        """Run EasyEcomAccount.validate on a stubbed enabled=1 doc with
        frappe.flags.in_test set to the given value. Restores the flag
        after. Returns the doc so caller can inspect method calls.

        `frappe.flags` is a _dict subclass — patch.object doesn't work
        reliably on it, so use direct assignment with restore."""
        acc = MagicMock(spec=mod.EasyEcomAccount)
        acc.enabled = 1
        for method_name in (
            "_validate_api_endpoint", "_validate_rate_limit_tier",
            "_clamp_throughput_to_tier", "_validate_webhook_config",
            "_warn_if_default_tier_in_production",
            "_update_webhook_endpoint_display",
            "_validate_single_enabled_account",
            "_run_pre_flight_config_checks",
        ):
            setattr(acc, method_name, MagicMock())
        original = mod.frappe.flags.get("in_test", None)
        mod.frappe.flags.in_test = in_test
        try:
            mod.EasyEcomAccount.validate(acc)
        finally:
            if original is None:
                mod.frappe.flags.pop("in_test", None)
            else:
                mod.frappe.flags.in_test = original
        return acc

    def test_validate_calls_pre_flight_when_enabled_on_real_site(self):
        """On a real site (frappe.flags.in_test=False), enabling the
        account MUST trigger the pre-flight. Tests get skipped so
        fixture setup doesn't spuriously block."""
        acc = self._validate_with_flag(in_test=False)
        acc._run_pre_flight_config_checks.assert_called_once()

    def test_validate_skips_pre_flight_in_test_mode(self):
        """gh#148 test-mode guard: fixture setup with enabled=1 must
        NOT trigger the pre-flight check (would spuriously block on
        missing Live Locations etc.). Real-site behavior verified by
        the sibling test above."""
        acc = self._validate_with_flag(in_test=True)
        acc._run_pre_flight_config_checks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
