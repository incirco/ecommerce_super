"""gh#158 regression — sparse update payload must always include EE's
mandatory-on-every-call fields (TaxRuleName, TaxRate, ProductTaxCode)
regardless of whether they've changed since the baseline snapshot.

Live symptom (Garv 2026-07-11 mmpl16, FG06476-CHOUHAN):
  POST /Products/UpdateMasterProduct → {"code":400,"message":
  "TaxRuleName is a mandatory parameter"}. Root cause: baseline was
  written from the successful Create with TaxRuleName=GST5; every
  subsequent Update diffs it as unchanged and drops it.
"""
from __future__ import annotations

import json
from unittest.mock import patch


def _run_builder(*, full_payload: dict, prior: dict | None) -> dict:
    """Invoke _build_sparse_update_payload with a mocked snapshot read."""
    from ecommerce_super.easyecom.flows.item_push import (
        _build_sparse_update_payload,
    )
    snapshot_text = json.dumps(prior) if prior is not None else None

    def _fake_get_value(*args, **kwargs):
        # First call is on Item Map; second is the Product Bundle
        # fallback. We return the snapshot on the first, None on the
        # second (this test only covers the Item-typed path).
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


def test_taxrulename_survives_unchanged_diff() -> None:
    """The exact live scenario: prior baseline already carries
    TaxRuleName=GST5; new full payload also carries GST5; sparse
    delta MUST still include TaxRuleName in the output."""
    prior = {
        "productId": 39046740,
        "sku": "FG06476-CHOUHAN",
        "productName": "01Test",
        "TaxRuleName": "GST5",
        "TaxRate": 5,
        "ProductTaxCode": "52081110",
        "weight": 100,
    }
    full = dict(prior)  # nothing changed
    delta = _run_builder(full_payload=full, prior=prior)
    # All EE-mandatory fields must be present even though nothing
    # differs from the baseline.
    assert delta.get("TaxRuleName") == "GST5", delta
    assert delta.get("TaxRate") == 5, delta
    assert delta.get("ProductTaxCode") == "52081110", delta
    assert delta.get("productId") == 39046740, delta
    # And the truly-unchanged non-mandatory field is NOT sent —
    # sparse-diff behavior preserved for everything else.
    assert "weight" not in delta, delta


def test_changed_field_still_wins() -> None:
    """When a non-mandatory field DOES change, sparse diff still emits
    it alongside the mandatory always-sends."""
    prior = {
        "productId": 39046740,
        "TaxRuleName": "GST5",
        "TaxRate": 5,
        "ProductTaxCode": "52081110",
        "weight": 100,
    }
    full = dict(prior)
    full["weight"] = 250  # changed
    delta = _run_builder(full_payload=full, prior=prior)
    assert delta.get("TaxRuleName") == "GST5"
    assert delta.get("weight") == 250, delta


def test_no_baseline_returns_full_payload() -> None:
    """When no snapshot exists (e.g. first push after Item Map created
    but before the Create write-back), builder returns the full payload."""
    full = {
        "productId": 1,
        "sku": "NEW-ITEM",
        "TaxRuleName": "GST5",
        "TaxRate": 5,
        "ProductTaxCode": "99999999",
    }
    delta = _run_builder(full_payload=full, prior=None)
    assert delta == full, delta


def test_always_send_set_contents() -> None:
    """Regression guard: the set must include the three fields EE is
    documented to require on every Update. If a field is added or
    removed, this test forces a review."""
    from ecommerce_super.easyecom.flows.item_push import (
        _ALWAYS_SEND_UPDATE_FIELDS,
    )
    assert "TaxRuleName" in _ALWAYS_SEND_UPDATE_FIELDS
    assert "TaxRate" in _ALWAYS_SEND_UPDATE_FIELDS
    assert "ProductTaxCode" in _ALWAYS_SEND_UPDATE_FIELDS
    assert "productId" in _ALWAYS_SEND_UPDATE_FIELDS
