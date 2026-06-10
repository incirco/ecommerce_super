"""gh#38 — EasyEcom-Item-Push ruleset must include `stock_uom →
accounting_unit` so ERPNext-side UOM updates propagate to EE.

The Pull side has carried `accounting_unit → stock_uom` since §8d
Stage 2. The Push side was missing the inverse rule entirely — the
asymmetry meant `bench migrate` worked, runs were green, but
ERPNext-side UOM corrections silently disappeared from the EE payload.

This test freezes the contract on the ruleset's child rules so a
future fixture-edit that drops the rule fails here, not in production
when a customer reports another silent UOM divergence.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


_RULESET_NAME = "EasyEcom-Item-Push"


class TestItemPushRulesetCoversStockUom(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if not frappe.db.exists("EasyEcom Field Mapping", _RULESET_NAME):
            cls.skipTest(
                cls,
                f"{_RULESET_NAME} fixture not present on this site — "
                "skipping (the fixture loader hasn't run, or the site "
                "predates §8d Stage 3).",
            )
        cls.doc = frappe.get_doc("EasyEcom Field Mapping", _RULESET_NAME)

    def test_push_ruleset_has_stock_uom_rule(self) -> None:
        match = [
            r
            for r in (self.doc.get("rules") or [])
            if r.erpnext_path == "stock_uom"
            and r.easyecom_path == "accounting_unit"
        ]
        self.assertEqual(
            len(match),
            1,
            "EasyEcom-Item-Push must include exactly one rule mapping "
            "stock_uom → accounting_unit (gh#38 contract). The Pull side "
            "has the inverse; the Push side ships symmetrically so "
            "ERPNext-side UOM updates propagate to EE.",
        )

    def test_stock_uom_push_rule_uses_identity_transform(self) -> None:
        rule = next(
            r
            for r in self.doc.get("rules") or []
            if r.erpnext_path == "stock_uom"
            and r.easyecom_path == "accounting_unit"
        )
        # Identity push: ERPNext's stock_uom string is the value EE
        # receives. EE accepts arbitrary strings on accounting_unit
        # (per their FAQ); no normalisation here.
        self.assertEqual(rule.transform_push, "identity")
        # Pull direction stays identity too — the dirty-UOM substitution
        # lives in the FLOW (item_pull.py), not the ruleset, because the
        # validity check requires a tabUOM DB lookup.
        self.assertEqual(rule.transform_pull, "identity")
