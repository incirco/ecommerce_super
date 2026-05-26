"""Unit tests for the endpoints module."""
from __future__ import annotations

import unittest

from ecommerce_super.easyecom.client.endpoints import (
    FOUNDATIONAL_ENDPOINTS,
    PRODUCT_MASTER_GET,
    TOKEN,
    is_foundational,
)


class TestIsFoundational(unittest.TestCase):
    def test_exact_path_match(self) -> None:
        self.assertTrue(is_foundational(TOKEN))
        self.assertTrue(is_foundational(PRODUCT_MASTER_GET))

    def test_cursor_follow_with_query_string_still_classified(self) -> None:
        """Regression: cursor-follow calls pass endpoint with a long
        query string. The exact-string membership check would miss
        these, splitting observability across foundational vs
        non-foundational buckets for the same logical endpoint AND
        breaking JWT acquisition (client._request only auto-acquires
        a JWT from default_location_key when foundational=True; a
        non-foundational + no-location_key call sets no Auth header
        and 401s)."""
        cursor_url = f"{PRODUCT_MASTER_GET}?cursor=ABC123XYZ" + "DEF" * 50
        self.assertTrue(
            is_foundational(cursor_url),
            f"Cursor follow URL {cursor_url[:60]}... should classify "
            "the same as the bare endpoint",
        )

    def test_unknown_endpoint_remains_non_foundational(self) -> None:
        self.assertFalse(is_foundational("/Some/Unknown/Endpoint"))
        # Query string shouldn't trick a non-foundational endpoint
        # into being foundational either.
        self.assertFalse(is_foundational("/Some/Unknown/Endpoint?cursor=X"))

    def test_product_master_get_in_foundational_set(self) -> None:
        """Document that §8d Product Master IS foundational (account-
        wide, no per-location). Removing it from the set would break
        JWT auto-acquire — see client._request line ~218."""
        self.assertIn(PRODUCT_MASTER_GET, FOUNDATIONAL_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
