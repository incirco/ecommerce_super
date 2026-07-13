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
