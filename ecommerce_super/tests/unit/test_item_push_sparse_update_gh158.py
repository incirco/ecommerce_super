"""gh#158 regression — sparse update always includes EE-mandatory fields
(TaxRuleName, TaxRate, ProductTaxCode) regardless of whether they've
changed from the baseline. Live symptom: FG06476-CHOUHAN failed to
push with `400 "TaxRuleName is a mandatory parameter"`.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch


def _run_builder(full_payload, prior):
    """Invoke _build_sparse_update_payload with a mocked snapshot read."""
    from ecommerce_super.easyecom.flows.item_push import (
        _build_sparse_update_payload,
    )
    snapshot_text = json.dumps(prior) if prior is not None else None

    def _fake_get_value(*args, **kwargs):
        fieldname = args[2] if len(args) > 2 else kwargs.get("fieldname")
        if fieldname == "ecs_last_pushed_payload":
            return snapshot_text
        return None

    with patch(
        "ecommerce_super.easyecom.flows.item_push.frappe.db.get_value",
        side_effect=_fake_get_value,
    ):
        return _build_sparse_update_payload(
            full_payload=full_payload, item_code="FG06476-CHOUHAN"
        )


class TestGh158AlwaysSendMandatory(unittest.TestCase):
    def test_taxrulename_survives_unchanged_diff(self):
        """Prior baseline has TaxRuleName=GST5; new full has same;
        delta MUST still include it."""
        prior = {
            "productId": 39046740, "sku": "FG06476-CHOUHAN",
            "productName": "01Test", "TaxRuleName": "GST5",
            "TaxRate": 5, "ProductTaxCode": "52081110", "weight": 100,
        }
        full = dict(prior)  # nothing changed
        delta = _run_builder(full_payload=full, prior=prior)
        self.assertEqual(delta.get("TaxRuleName"), "GST5")
        self.assertEqual(delta.get("TaxRate"), 5)
        self.assertEqual(delta.get("ProductTaxCode"), "52081110")
        self.assertEqual(delta.get("productId"), 39046740)
        # Truly unchanged non-mandatory field is NOT sent
        self.assertNotIn("weight", delta)

    def test_changed_field_still_wins(self):
        """Changed non-mandatory field still emitted with mandatory always-sends."""
        prior = {
            "productId": 39046740, "TaxRuleName": "GST5",
            "TaxRate": 5, "ProductTaxCode": "52081110", "weight": 100,
        }
        full = dict(prior)
        full["weight"] = 250  # changed
        delta = _run_builder(full_payload=full, prior=prior)
        self.assertEqual(delta.get("TaxRuleName"), "GST5")
        self.assertEqual(delta.get("weight"), 250)

    def test_no_baseline_returns_full_payload(self):
        """No snapshot → return full payload."""
        full = {
            "productId": 1, "sku": "NEW-ITEM",
            "TaxRuleName": "GST5", "TaxRate": 5,
            "ProductTaxCode": "99999999",
        }
        delta = _run_builder(full_payload=full, prior=None)
        self.assertEqual(delta, full)

    def test_always_send_set_contents(self):
        """Regression guard: the set must include the three fields EE requires."""
        from ecommerce_super.easyecom.flows.item_push import (
            _ALWAYS_SEND_UPDATE_FIELDS,
        )
        self.assertIn("TaxRuleName", _ALWAYS_SEND_UPDATE_FIELDS)
        self.assertIn("TaxRate", _ALWAYS_SEND_UPDATE_FIELDS)
        self.assertIn("ProductTaxCode", _ALWAYS_SEND_UPDATE_FIELDS)
        self.assertIn("productId", _ALWAYS_SEND_UPDATE_FIELDS)


class TestGh236AccountingUnitAlwaysSent(unittest.TestCase):
    """gh#236 — item perpetually Created-Flagged because EE-side
    `accounting_unit` is blank and our sparse-diff never re-sends it.

    Root cause: snapshot has `accounting_unit=PCS`, ERPNext still
    maps stock_uom→accounting_unit=PCS, diff says "unchanged" → EE
    stays blank → next pull re-flags the item forever.

    Fix: add `accounting_unit` to `_ALWAYS_SEND_UPDATE_FIELDS` so it
    rides on every UpdateMasterProduct call regardless of diff, breaking
    the loop even when the snapshot is out of sync with EE reality.
    """

    def test_accounting_unit_in_always_send_set(self):
        """Guard against removal — the set membership IS the fix."""
        from ecommerce_super.easyecom.flows.item_push import (
            _ALWAYS_SEND_UPDATE_FIELDS,
        )
        self.assertIn("accounting_unit", _ALWAYS_SEND_UPDATE_FIELDS)

    def test_accounting_unit_sent_when_unchanged_from_snapshot(self):
        """The exact live scenario from FG06588-RATHORE-M: snapshot has
        accounting_unit=PCS, current also PCS. Delta MUST still include
        it so EE's blank value gets corrected on the next tick."""
        prior = {
            "productId": 219723416, "sku": "FG06588-RATHORE-M",
            "productName": "Rathore Set M",
            "TaxRuleName": "GST5", "TaxRate": 5,
            "ProductTaxCode": "62052000",
            "accounting_unit": "PCS",  # already in snapshot
        }
        full = dict(prior)  # nothing changed
        delta = _run_builder(full_payload=full, prior=prior)
        # accounting_unit MUST be in the delta despite unchanged
        self.assertEqual(delta.get("accounting_unit"), "PCS")

    def test_accounting_unit_omitted_when_absent_from_full_payload(self):
        """If the full payload doesn't include accounting_unit at all
        (e.g. mapping rule not yet applied on some legacy path), the
        always-send set doesn't fabricate the field — it just includes
        it WHEN PRESENT. Regression guard for the seeding logic."""
        prior = {
            "productId": 1, "TaxRuleName": "GST5",
            "TaxRate": 5, "ProductTaxCode": "99",
        }
        full = dict(prior)  # no accounting_unit in either side
        delta = _run_builder(full_payload=full, prior=prior)
        self.assertNotIn("accounting_unit", delta)

    def test_accounting_unit_new_value_sent(self):
        """If ERPNext's stock_uom just changed (e.g. FDE corrected from
        Nos to PCS) and snapshot has the old value, delta contains the
        new value. Locks that the always-send doesn't accidentally
        prefer the SNAPSHOT value over the CURRENT value."""
        prior = {
            "productId": 1, "TaxRuleName": "GST5",
            "TaxRate": 5, "ProductTaxCode": "99",
            "accounting_unit": "Nos",  # old snapshot value
        }
        full = dict(prior)
        full["accounting_unit"] = "PCS"  # FDE corrected in ERPNext
        delta = _run_builder(full_payload=full, prior=prior)
        # Sends the NEW value, not the stale snapshot value
        self.assertEqual(delta.get("accounting_unit"), "PCS")
