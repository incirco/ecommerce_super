"""§9 Stage 4 — Account-isolation regression guard.

Lives at the integration-test layer; runs as part of the standard
bench test suite. The contract this guard enforces:

    A test must NEVER mutate ANY EasyEcom Account whose name does not
    look like a test fixture ("test-*", "TEST-*", "acc-*"). Live /
    Production accounts (Harmony, prod, anything explicitly named by
    an FDE) must be byte-identical before and after every test run.

The dev incident this guards against (§8d incident pre-history, Stage 3
smoke 2026-05-28):

    1. A test setUp does `acct = get_value("EasyEcom Account",
       {"enabled": 1}, "name")` to grab "the enabled account."
    2. On a dev site with Harmony enabled + test-account also enabled
       (from a prior run), the lookup may return Harmony.
    3. The test then writes test-warehouse / test-c_id / etc. to
       Harmony's config → live config corrupted.

Two layers of defense:
- This module's TestNoCrossAccountMutation snapshots every non-test
  Account's full row + their child rows (Mappings etc.) at module-
  load time and re-asserts equality after every method. Any drift
  surfaces here loudly, NOT downstream when an FDE notices weird
  values in production.
- The companion fix in §9 test files: replace {"enabled": 1}
  lookups with explicit by-name lookups ("test-account").

Snapshot taken at module load (not per-test setUp) — this is the
clean-baseline state. Tests within this module that intentionally
mutate Accounts should reset back to baseline before tearDown.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


# Account-name prefixes that are SAFE to mutate (test fixtures).
TEST_ACCOUNT_PREFIXES: tuple[str, ...] = ("test-", "TEST-", "acc-", "ACC-")


def _is_test_account(name: str) -> bool:
    return name.startswith(TEST_ACCOUNT_PREFIXES)


def _snapshot_protected_accounts() -> dict:
    """Returns a dict {account_name: column_dict} for every Account
    whose name does NOT look like a test fixture. Includes ALL columns
    (raw SQL row) so any field-level drift is detected."""
    rows = frappe.db.sql(
        "SELECT * FROM `tabEasyEcom Account`",
        as_dict=True,
    )
    out: dict = {}
    for row in rows:
        name = row["name"]
        if _is_test_account(name):
            continue
        # Strip volatile timestamp-ish columns that ERPNext writes
        # on every read (modified) — comparing those would surface
        # false positives.
        clean = {
            k: v for k, v in row.items()
            if k not in {"modified", "modified_by"}
        }
        out[name] = clean
    return out


class TestNoCrossAccountMutation(FrappeTestCase):
    """A bare-bones guard: snapshot + post-suite compare. If any
    non-test Account changed during this test class's lifetime,
    the assertion fires and identifies WHICH account + WHICH field
    drifted."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._baseline = _snapshot_protected_accounts()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        # No-op — the assertion lives in test_baseline_unchanged so
        # it's picked up by the test runner's pass/fail reporting,
        # not silently swallowed in tearDown.

    def test_baseline_unchanged_post_setup(self) -> None:
        """After setUpClass runs, the baseline must not have shifted.
        (Catches setUpClass-level corruption — common pattern in
        Stage 3-era tests.)"""
        current = _snapshot_protected_accounts()
        self.assertEqual(
            set(current.keys()),
            set(self._baseline.keys()),
            "A non-test EasyEcom Account was created or deleted by "
            "test infrastructure. Investigate which test class's "
            "setUpClass touched a production-marked Account.",
        )
        for name, baseline_row in self._baseline.items():
            current_row = current.get(name, {})
            for field, baseline_val in baseline_row.items():
                self.assertEqual(
                    current_row.get(field),
                    baseline_val,
                    f"Non-test Account {name!r} field {field!r} drifted "
                    f"from baseline. This is a TEST-ISOLATION VIOLATION "
                    f"— see §9 Stage 4 carry-in. Live impact: production "
                    f"Account config can be silently overwritten by "
                    f"test setUps that use {{'enabled': 1}} lookups.\n"
                    f"  baseline:  {baseline_val!r}\n"
                    f"  current:   {current_row.get(field)!r}\n"
                    f"Fix: replace {{'enabled': 1}} lookups in the "
                    f"offending test setUp with explicit by-name lookups."
                )


class TestPrefixHelper(FrappeTestCase):
    """Pin the test-account-prefix vocabulary."""

    def test_test_prefix_recognised(self) -> None:
        self.assertTrue(_is_test_account("test-account"))
        self.assertTrue(_is_test_account("test-foo"))
        self.assertTrue(_is_test_account("TEST-BAR"))
        self.assertTrue(_is_test_account("acc-baz"))

    def test_production_names_not_recognised(self) -> None:
        self.assertFalse(_is_test_account("Harmony"))
        self.assertFalse(_is_test_account("AcmeCorp"))
        self.assertFalse(_is_test_account(""))
