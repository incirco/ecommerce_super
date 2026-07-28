"""gh#230: on-demand Held-Pre-QC re-sweep entrypoint + chained sweep
at the end of `scheduled_grn_pull`.

Locks two contracts:

  1. `resweep_one_held_grn(grn_map_name)` — FDE-triggered per-row
     entrypoint. Called from the GRN Map form button.
     - Rejects unknown Map / non-Held-Pre-QC rows with clear messages.
     - Short-circuits on already-Receipted rows (idempotent).
     - Refuses when the row has no grn_created_at anchor.
     - Refuses when the warehouse_c_id doesn't resolve to an EE Location.
     - Delegates to pull_grns_for_location with the earliest-safe
       created_after anchor (row's grn_created_at minus 1 second).
     - Returns a clear result dict for the JS button to render.

  2. `scheduled_grn_pull` chains `resweep_held_pre_qc_grns` at the end
     so an FDE-triggered pull picks up Held-Pre-QC rows in the same
     session, without waiting for the hourly cron.
     - The chained resweep's failure doesn't fail the overall
       scheduled_grn_pull response (belt-and-suspenders).

Related to and complements:
  - gh#120 — hourly `resweep_held_pre_qc_grns` cron (unchanged)
  - gh#234 — rolling look-back window on the forward delta (unchanged)
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401

from ecommerce_super.easyecom.flows import grn_pull as mod


# ============================================================
# resweep_one_held_grn — the per-row entrypoint
# ============================================================


class TestResweepOneHeldGrn(unittest.TestCase):

    def _patch_ctx(self, *, map_row, location=None, sweep_side_effect=None,
                   post_sweep_status=None, account="Test Account"):
        """Build the mock stack for a resweep_one_held_grn call."""
        stubs = {"post_sweep_status": post_sweep_status}

        def _exists(doctype, name):
            if doctype == "EasyEcom GRN Map":
                return map_row is not None
            return False

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            filters = kwargs.get("filters") or (args[1] if len(args) > 1 else None)
            field = (
                kwargs.get("field")
                or kwargs.get("fieldname")
                or (args[2] if len(args) > 2 else None)
            )
            if doctype == "EasyEcom GRN Map":
                if kwargs.get("as_dict") or (
                    len(args) >= 3 and isinstance(field, list)
                ):
                    # Return as frappe._dict for attribute-style access
                    import frappe as _frappe
                    return _frappe._dict(map_row) if map_row else None
                # scalar read for post-sweep status
                if field == "status":
                    return stubs["post_sweep_status"]
            if doctype == "EasyEcom Account":
                return account
            return None

        def _fake_only_for(*_args, **_kwargs):
            return True

        def _fake_pull(**_kwargs):
            if sweep_side_effect is not None:
                if isinstance(sweep_side_effect, Exception):
                    raise sweep_side_effect
            return None

        patchers = [
            patch.object(mod.frappe.db, "exists", side_effect=_exists),
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe, "only_for", side_effect=_fake_only_for),
            patch.object(mod.frappe, "log_error", MagicMock()),
            patch.object(
                mod, "_resolve_location_for_warehouse_c_id",
                return_value=location,
            ),
            patch.object(mod, "pull_grns_for_location", side_effect=_fake_pull),
            patch.object(mod, "EasyEcomClient", MagicMock()),
        ]
        return patchers

    def _run(self, **kw):
        patchers = self._patch_ctx(**{k: v for k, v in kw.items()
                                      if k != "grn_map_name"})
        for p in patchers:
            p.start()
        try:
            return mod.resweep_one_held_grn(
                grn_map_name=kw.get("grn_map_name", "ECS-GRN-999"),
            )
        finally:
            for p in patchers:
                p.stop()

    def test_missing_grn_map_name_rejected(self):
        # Nothing else runs — return early
        result = mod.resweep_one_held_grn(grn_map_name="")
        self.assertFalse(result["ok"])
        self.assertIn("required", result["message"])

    def test_nonexistent_map_rejected(self):
        result = self._run(map_row=None, grn_map_name="ECS-GRN-DOES-NOT-EXIST")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["message"])

    def test_already_receipted_short_circuits_ok(self):
        """Idempotent: safe to click the button on a row already done."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": datetime(2026, 7, 20, 10, 0, 0),
                     "inwarded_warehouse_c_id": 42,
                     "status": "Receipted"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["new_status"], "Receipted")
        self.assertIn("Already Receipted", result["message"])

    def test_non_held_status_rejected(self):
        """Failed / Discrepancy / STN-Routed etc. → refuse with hint."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": datetime(2026, 7, 20, 10, 0, 0),
                     "inwarded_warehouse_c_id": 42,
                     "status": "Failed"},
        )
        self.assertFalse(result["ok"])
        self.assertIn("not 'Held-Pre-QC'", result["message"])

    def test_missing_grn_created_at_rejected(self):
        """No anchor → can't build a safe created_after → refuse."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": None,
                     "inwarded_warehouse_c_id": 42,
                     "status": "Held-Pre-QC"},
        )
        self.assertFalse(result["ok"])
        self.assertIn("grn_created_at", result["message"])

    def test_unresolvable_warehouse_rejected(self):
        """warehouse_c_id doesn't map to an EE Location → refuse."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": datetime(2026, 7, 20, 10, 0, 0),
                     "inwarded_warehouse_c_id": 999999,
                     "status": "Held-Pre-QC"},
            location=None,  # unresolved
        )
        self.assertFalse(result["ok"])
        self.assertIn("EE Location", result["message"])

    def test_happy_path_receipted_after_sweep(self):
        """QC completed on EE since last poll → sweep converts row →
        status flips Held-Pre-QC → Receipted."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": datetime(2026, 7, 20, 10, 0, 0),
                     "inwarded_warehouse_c_id": 42,
                     "status": "Held-Pre-QC"},
            location={"location_key": "LOC-001"},
            post_sweep_status="Receipted",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["old_status"], "Held-Pre-QC")
        self.assertEqual(result["new_status"], "Receipted")
        self.assertIn("PR created", result["message"])

    def test_still_held_after_sweep(self):
        """QC not yet completed on EE → row stays Held-Pre-QC → message
        tells FDE to retry later."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": datetime(2026, 7, 20, 10, 0, 0),
                     "inwarded_warehouse_c_id": 42,
                     "status": "Held-Pre-QC"},
            location={"location_key": "LOC-001"},
            post_sweep_status="Held-Pre-QC",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["new_status"], "Held-Pre-QC")
        self.assertIn("Retry later", result["message"])

    def test_sweep_exception_returns_error(self):
        """EE call exception → return not-ok with the exception detail
        (don't crash the button)."""
        result = self._run(
            map_row={"name": "ECS-GRN-1", "ee_grn_id": "GRN-111",
                     "grn_created_at": datetime(2026, 7, 20, 10, 0, 0),
                     "inwarded_warehouse_c_id": 42,
                     "status": "Held-Pre-QC"},
            location={"location_key": "LOC-001"},
            sweep_side_effect=RuntimeError("EE 500"),
        )
        self.assertFalse(result["ok"])
        self.assertIn("EE 500", result["message"])


# ============================================================
# scheduled_grn_pull chains the resweep at the end
# ============================================================


class TestScheduledGrnPullChainsResweep(unittest.TestCase):
    """gh#230: manual/on-demand `scheduled_grn_pull` invocation must
    chain the Held-Pre-QC re-sweep so an FDE-triggered pull picks up
    stuck rows in-session."""

    def _run_scheduled(self, *, resweep_result=None, resweep_raises=None,
                       locations=None):
        locations = locations or ["LOC-001"]
        captured = {"resweep_called": False}

        def _get_value(*args, **kwargs):
            doctype = kwargs.get("doctype") or (args[0] if len(args) > 0 else None)
            if doctype == "EasyEcom Account":
                return "Test Account"
            return None

        def _get_all(doctype, **kw):
            if doctype == "EasyEcom Location":
                return locations
            return []

        def _fake_pull(**_kw):
            sweep = mod.GRNSweepOutcome()
            sweep.pages_walked = 1
            sweep.grns_processed = 0
            return sweep

        def _fake_resweep(**_kw):
            captured["resweep_called"] = True
            if resweep_raises:
                raise resweep_raises
            return resweep_result or {
                "ok": True, "held_scanned": 0, "receipted": 0,
                "still_held": 0, "failed": 0,
            }

        with (
            patch.object(mod.frappe.db, "get_value", side_effect=_get_value),
            patch.object(mod.frappe.db, "get_all", side_effect=_get_all),
            patch.object(mod.frappe, "log_error", MagicMock()),
            patch.object(mod, "pull_grns_for_location", side_effect=_fake_pull),
            patch.object(mod, "resweep_held_pre_qc_grns", side_effect=_fake_resweep),
        ):
            result = mod.scheduled_grn_pull(account_name="Test Account")
        return result, captured

    def test_resweep_is_chained_after_forward_delta(self):
        """The core gh#230 promise: a manual scheduled_grn_pull picks up
        Held-Pre-QC rows in-session, not on the next cron tick."""
        result, captured = self._run_scheduled(
            resweep_result={"ok": True, "receipted": 2, "still_held": 0},
        )
        self.assertTrue(captured["resweep_called"])
        self.assertIn("held_pre_qc_resweep", result)
        self.assertEqual(result["held_pre_qc_resweep"]["receipted"], 2)

    def test_resweep_failure_does_not_fail_the_overall_pull(self):
        """Belt-and-suspenders: chained resweep exception is captured
        as a partial-failure signal, doesn't fail the whole response.
        The forward-delta sweeps above already succeeded."""
        result, captured = self._run_scheduled(
            resweep_raises=RuntimeError("resweep exploded"),
        )
        self.assertTrue(captured["resweep_called"])
        # Overall still OK because forward delta ran
        self.assertTrue(result["ok"])
        # But the resweep failure is surfaced
        self.assertFalse(result["held_pre_qc_resweep"]["ok"])

    def test_resweep_receives_the_account_name(self):
        """Regression guard: the chained call must scope to the same
        account, not accidentally re-resolve to a different one."""
        captured_args = {}

        def _fake_resweep(**kw):
            captured_args.update(kw)
            return {"ok": True}

        with (
            patch.object(mod.frappe.db, "get_value", return_value="Test Account"),
            patch.object(mod.frappe.db, "get_all", return_value=["LOC-001"]),
            patch.object(mod, "pull_grns_for_location", return_value=mod.GRNSweepOutcome()),
            patch.object(mod, "resweep_held_pre_qc_grns", side_effect=_fake_resweep),
        ):
            mod.scheduled_grn_pull(account_name="Test Account")

        self.assertEqual(captured_args.get("account_name"), "Test Account")


if __name__ == "__main__":
    unittest.main()
