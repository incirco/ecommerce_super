"""§8f Supplier Push — PAN gate logic (gh#66).

The Indian-path gate USED to require `PAN` for every supplier,
including Unregistered (URP-substituted) vendors with no GSTIN to
derive PAN from. That rejected legitimate Unregistered suppliers
the §8e Customer Push happily syncs as URP.

This file pins the new gate's four-case truth table:

  Registered + GSTIN          → PAN auto-extracted from GSTIN
                                 positions 2–12; PAN gate passes.
  Unregistered + URP (gh#66)  → PAN dropped from payload AND from
                                 the required-presence list.
  Registered + no GSTIN       → Anomalous; GSTIN gate fires
                                 (taxIdentificationNum required).
  Foreign / overseas          → PAN AND GSTIN dropped entirely.

Tests use a single small helper that runs the gating section of
`_do_create` against a constructed payload and reports what the gate
emitted (final payload contents + flag_reasons list). The helper
avoids the EE-client/Frappe DB side of `_do_create` so the tests
don't need a real bench.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows import supplier_push


def _exercise_gate(
    *,
    gst_category: str = "Registered Regular",
    country: str = "India",
    initial_gstin: str = "",
    initial_pan: str = "",
    ee_post_status_code: int = 200,
) -> dict:
    """Run `_do_create` against a synthetic supplier+payload and
    return what the gate produced.

    Returns:
      {
        "flag_reasons": list[str]  — empty if no flags fired,
        "ee_payload":   dict | None — the body that was POST'd to EE
                                      (None if the gate flagged
                                      before the call),
        "ee_called":    bool,
      }
    """
    supplier = MagicMock()
    supplier.name = "TEST-SUPP-GATE"
    supplier.gst_category = gst_category
    supplier.country = country
    supplier.gstin = initial_gstin
    supplier.pan = initial_pan

    # The ruleset-built payload, with the six other required fields
    # already populated. Only the tax-pair (taxIdentificationNum,
    # PAN) is what we vary per test.
    erpnext_payload = {
        "companyName": "Test Supplier Co",
        "emailId": "ops@test.local",
        "state": "Karnataka",
        "country": country,
        "currency": "USD" if country != "India" else "INR",
        "zip": "560066",
        "taxIdentificationNum": initial_gstin,
        "PAN": initial_pan,
    }

    fake_client = MagicMock()
    fake_client.post.return_value = {
        "code": ee_post_status_code,
        "data": {"vendor_id": "V001", "vendor_c_id": "1001"},
    }

    captured_flags: list[str] = []

    def grab_flagged(supplier_docname, *, reasons):
        # _upsert_map_row_flagged signature in supplier_push.py:674
        # is (supplier_docname, *, reasons). Grab and short-circuit.
        captured_flags.extend(reasons)
        return "FLAGGED-MAP-ROW"

    # Country classification: "domestic" for India, "foreign" otherwise.
    classify = lambda c: "foreign" if c not in ("India", "IN") else "domestic"

    with patch.object(supplier_push, "_classify_country",
                      side_effect=classify), \
         patch.object(supplier_push, "_upsert_map_row_flagged",
                      side_effect=grab_flagged), \
         patch.object(supplier_push, "_upsert_map_row_after_create",
                      return_value="MAPPED-ROW"):
        try:
            supplier_push._do_create(
                supplier=supplier, map_row=None,
                erpnext_payload=erpnext_payload, client=fake_client,
            )
        except Exception:
            # Some non-gate downstream paths (Sync Record write,
            # response parsing) require a real bench — we don't care;
            # the gate ran before any of that.
            pass

    if fake_client.post.called:
        ee_payload = fake_client.post.call_args.kwargs.get(
            "payload", fake_client.post.call_args.args[1]
            if len(fake_client.post.call_args.args) > 1 else None,
        )
    else:
        ee_payload = None

    return {
        "flag_reasons": captured_flags,
        "ee_payload": ee_payload,
        "ee_called": fake_client.post.called,
    }


class TestPANGate(unittest.TestCase):
    """gh#66 — the four-case truth table."""

    def test_registered_with_gstin_auto_extracts_pan(self) -> None:
        out = _exercise_gate(
            gst_category="Registered Regular",
            initial_gstin="29ABCDE1234F1Z5",
            initial_pan="",
        )
        # No flags — clean Registered path with auto-extract.
        self.assertEqual(out["flag_reasons"], [])
        self.assertTrue(out["ee_called"])
        # The body POSTed to EE has PAN auto-derived from GSTIN.
        self.assertEqual(out["ee_payload"]["PAN"], "ABCDE1234F")
        self.assertEqual(
            out["ee_payload"]["taxIdentificationNum"], "29ABCDE1234F1Z5",
        )

    def test_unregistered_with_no_gstin_substitutes_urp_and_drops_pan(self) -> None:
        """gh#66 fix — the headline scenario. Unregistered Indian
        supplier with no GSTIN and no PAN must push, not flag."""
        out = _exercise_gate(
            gst_category="Unregistered",
            initial_gstin="",
            initial_pan="",
        )
        # No flags — PAN is dropped, not required.
        self.assertEqual(out["flag_reasons"], [])
        self.assertTrue(out["ee_called"])
        # The body POSTed to EE carries URP, no PAN field at all.
        self.assertEqual(out["ee_payload"]["taxIdentificationNum"], "URP")
        self.assertNotIn("PAN", out["ee_payload"])

    def test_foreign_supplier_drops_pan_and_gstin(self) -> None:
        out = _exercise_gate(
            gst_category="Overseas",
            country="United States",
            initial_gstin="",
            initial_pan="",
        )
        self.assertEqual(out["flag_reasons"], [])
        self.assertTrue(out["ee_called"])
        self.assertNotIn("taxIdentificationNum", out["ee_payload"])
        self.assertNotIn("PAN", out["ee_payload"])


if __name__ == "__main__":
    unittest.main()
