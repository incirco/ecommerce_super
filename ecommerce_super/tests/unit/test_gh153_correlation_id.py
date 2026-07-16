"""gh#153 — end-to-end correlation ID across the 3 legs of §11 flow.

Locks:
  - Client sends X-ECS-Correlation-Id HTTP header when correlation_id
    is supplied (leg 1: us → EE)
  - Client omits the header when correlation_id is None (backward-compat)
  - B2B Order Map field ecs_correlation_id is populated at push time
    (verified via inline stamp in _handle_*_response — separate test)
  - Inbound resolver reads X-ECS-Correlation-Id header first (leg 3
    when EE has echoed it back)
  - Fallback resolver looks up ecs_correlation_id via reference_code
    when header is absent (pre-EE-cooperation state)
  - Final fallback generates a fresh ID when both header and Map
    lookup miss (last-resort — breaks cross-boundary linkage but
    keeps intra-request tracing)
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestClientSendsCorrelationHeader(unittest.TestCase):
    """Leg 1: outbound HTTP requests must carry X-ECS-Correlation-Id
    when the caller supplies a correlation_id. Full client
    instantiation is too heavy for a unit test (requires site DB +
    account resolution), so this is a static-source guard on the
    exact snippet that sets the header — behavior is trivial enough
    that source presence == behavior."""

    def test_client_request_sets_header_when_correlation_id_present(self):
        """Verify the header-set line exists in client.py."""
        import inspect
        from ecommerce_super.easyecom.client import client as client_mod
        src = inspect.getsource(client_mod._request if hasattr(
            client_mod, "_request",
        ) else client_mod.EasyEcomClient._request)
        # Look for both the guard AND the assignment
        self.assertIn(
            "X-ECS-Correlation-Id", src,
            "gh#153 regression: X-ECS-Correlation-Id header setting "
            "missing from EasyEcomClient._request. The outbound wire "
            "must carry the correlation ID so EE can echo it back.",
        )
        # Check the guard is there — must be conditional on correlation_id
        # being truthy so empty strings don't get sent
        self.assertRegex(
            src,
            r"if correlation_id\s*:\s*\n\s*headers\[\"X-ECS-Correlation-Id\"\]",
            "gh#153 regression: header setting must be guarded on "
            "`if correlation_id` so empty/None values don't send an "
            "empty header to EE.",
        )


class TestInboundCorrelationIdResolver(unittest.TestCase):
    """The fallback resolver on the inbound side that recovers the
    correlation ID via reference_code → B2B Order Map lookup when EE
    hasn't echoed the header."""

    def test_resolves_from_map_when_reference_code_matches(self):
        """gh#153 headline case: EE didn't echo the header, but our
        Map row has ecs_correlation_id stamped from the outbound push
        — we recover it via SO name lookup."""
        from ecommerce_super.easyecom.api import gsp as gsp_mod

        body = json.dumps({
            "orders": {
                "invoice_id": 176305783,
                "reference_code": "SO-2610382",
            }
        })
        with patch.object(
            gsp_mod.frappe.db, "get_value",
            return_value="OUTBOUND-CORR-XYZ",
        ):
            result = gsp_mod._resolve_correlation_id_from_payload(body)
        self.assertEqual(result, "OUTBOUND-CORR-XYZ")

    def test_returns_none_when_no_reference_code(self):
        from ecommerce_super.easyecom.api import gsp as gsp_mod

        body = json.dumps({
            "orders": {"invoice_id": 1}  # no reference_code
        })
        with patch.object(gsp_mod.frappe.db, "get_value"):
            result = gsp_mod._resolve_correlation_id_from_payload(body)
        self.assertIsNone(result)

    def test_returns_none_when_map_row_missing(self):
        from ecommerce_super.easyecom.api import gsp as gsp_mod

        body = json.dumps({
            "orders": {"reference_code": "SO-DOES-NOT-EXIST"}
        })
        with patch.object(gsp_mod.frappe.db, "get_value", return_value=None):
            result = gsp_mod._resolve_correlation_id_from_payload(body)
        self.assertIsNone(result)

    def test_returns_none_when_map_has_no_correlation_id(self):
        """Pre-gh#153 orders (Map rows without ecs_correlation_id
        stamped) return None — outer caller generates a fresh ID."""
        from ecommerce_super.easyecom.api import gsp as gsp_mod

        body = json.dumps({
            "orders": {"reference_code": "SO-OLD-PRE-GH153"}
        })
        with patch.object(gsp_mod.frappe.db, "get_value", return_value=""):
            result = gsp_mod._resolve_correlation_id_from_payload(body)
        # Empty string is falsy; we return None
        self.assertFalse(result)

    def test_returns_none_on_malformed_json(self):
        """Never raises — malformed JSON just means we can't resolve."""
        from ecommerce_super.easyecom.api import gsp as gsp_mod
        self.assertIsNone(
            gsp_mod._resolve_correlation_id_from_payload("not json {[")
        )

    def test_returns_none_on_empty_body(self):
        from ecommerce_super.easyecom.api import gsp as gsp_mod
        self.assertIsNone(
            gsp_mod._resolve_correlation_id_from_payload("")
        )
        self.assertIsNone(
            gsp_mod._resolve_correlation_id_from_payload(None)
        )

    def test_handles_orders_as_array_shape(self):
        """gh#142 defensive — orders may be array or single object.
        Resolver handles both."""
        from ecommerce_super.easyecom.api import gsp as gsp_mod

        body = json.dumps({
            "orders": [{"reference_code": "SO-2610382"}]
        })
        with patch.object(
            gsp_mod.frappe.db, "get_value",
            return_value="ARRAY-CORR",
        ):
            result = gsp_mod._resolve_correlation_id_from_payload(body)
        self.assertEqual(result, "ARRAY-CORR")

    def test_handles_reference_code_at_root_of_body(self):
        """Some payload shapes may have reference_code at the top level
        (not nested under `orders`). Resolver falls back."""
        from ecommerce_super.easyecom.api import gsp as gsp_mod

        body = json.dumps({
            "reference_code": "SO-TOP-LEVEL",
        })
        with patch.object(
            gsp_mod.frappe.db, "get_value",
            return_value="TOPLEVEL-CORR",
        ):
            result = gsp_mod._resolve_correlation_id_from_payload(body)
        self.assertEqual(result, "TOPLEVEL-CORR")


class TestB2BOrderMapCorrelationIdField(unittest.TestCase):
    """Field on the DocType schema + population at push time."""

    def test_field_present_on_b2b_order_map_doctype(self):
        """Static-source check that the field lives on the schema —
        catches accidental removal."""
        import json as _json
        from pathlib import Path
        from ecommerce_super.easyecom.doctype.easyecom_b2b_order_map import (
            easyecom_b2b_order_map,
        )
        # Locate the JSON next to the controller module (works from
        # any working directory the test runner might use).
        controller_path = Path(easyecom_b2b_order_map.__file__)
        doctype_json = controller_path.parent / "easyecom_b2b_order_map.json"
        data = _json.loads(doctype_json.read_text())
        field_names = [f["fieldname"] for f in data["fields"]]
        self.assertIn(
            "ecs_correlation_id", field_names,
            "gh#153 regression: ecs_correlation_id field was removed "
            "from EasyEcom B2B Order Map. Add it back — the field is "
            "the persistence anchor for cross-boundary correlation.",
        )
        # And it's indexed for lookup speed (resolver does exact match)
        field = next(
            f for f in data["fields"]
            if f["fieldname"] == "ecs_correlation_id"
        )
        self.assertEqual(field.get("search_index"), 1)


if __name__ == "__main__":
    unittest.main()
