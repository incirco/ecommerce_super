"""Unit tests for the pure-Python utils: correlation, hashing, redaction, jsonpath."""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.utils import correlation, hashing, jsonpath, redaction


class TestCorrelation(unittest.TestCase):
    def test_new_correlation_id_is_uuidv7(self) -> None:
        cid = correlation.new_correlation_id()
        # canonical form, 36 chars with hyphens at the standard positions
        self.assertEqual(len(cid), 36)
        # version nibble — char index 14 (RFC 9562 §5.7)
        self.assertEqual(cid[14], "7")

    def test_correlation_ids_are_time_ordered(self) -> None:
        """UUIDv7 is time-ordered at millisecond resolution. IDs minted in
        the same millisecond differ only in the random tail; IDs minted in
        successive milliseconds are lexicographically ordered."""
        import time

        ids = []
        for _ in range(5):
            ids.append(correlation.new_correlation_id())
            time.sleep(0.002)  # 2ms — comfortably across the ms boundary
        # Each ID's first 12 hex chars are the unix_ts_ms — they should be
        # monotone non-decreasing across the sleeps.
        ts_prefixes = [cid[:13].replace("-", "") for cid in ids]
        self.assertEqual(ts_prefixes, sorted(ts_prefixes))

    def test_correlation_ids_are_unique(self) -> None:
        ids = {correlation.new_correlation_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)


class TestHashing(unittest.TestCase):
    def test_key_order_invariance(self) -> None:
        """Same content with different key order hashes identically (§6.1
        idempotency key formula relies on this)."""
        h1 = hashing.sha256_hex({"b": 1, "a": 2, "nested": {"y": 3, "x": 4}})
        h2 = hashing.sha256_hex({"a": 2, "b": 1, "nested": {"x": 4, "y": 3}})
        self.assertEqual(h1, h2)

    def test_different_content_different_hash(self) -> None:
        self.assertNotEqual(
            hashing.sha256_hex({"a": 1}),
            hashing.sha256_hex({"a": 2}),
        )

    def test_sha256_idempotency_formula(self) -> None:
        """Mirrors a §6.1 formula like sha256('item:co:item_code:loc:hash')."""
        key = hashing.sha256_idempotency("item", "ACME", "ITM-001", "LOC1", "abc123")
        self.assertEqual(len(key), 64)
        # Same parts → same key (idempotency on retry).
        same = hashing.sha256_idempotency("item", "ACME", "ITM-001", "LOC1", "abc123")
        self.assertEqual(key, same)


class TestRedaction(unittest.TestCase):
    def test_redacts_known_credential_field_names(self) -> None:
        payload = {
            "x_api_key": "sekret",
            "name": "public",
            "nested": {"password": "hush", "token": "tk", "fine": 1},
        }
        out = redaction.redact(payload)
        self.assertEqual(out["x_api_key"], redaction.REDACTED_PLACEHOLDER)
        self.assertEqual(out["name"], "public")
        self.assertEqual(out["nested"]["password"], redaction.REDACTED_PLACEHOLDER)
        self.assertEqual(out["nested"]["token"], redaction.REDACTED_PLACEHOLDER)
        self.assertEqual(out["nested"]["fine"], 1)

    def test_redacts_header_form(self) -> None:
        """Both header-style and snake_case variants are matched (§3.7.4)."""
        out = redaction.redact(
            {"x-api-key": "h", "Authorization": "Bearer abc.def.ghi"}
        )
        self.assertEqual(out["x-api-key"], redaction.REDACTED_PLACEHOLDER)
        self.assertEqual(out["Authorization"], redaction.REDACTED_PLACEHOLDER)

    def test_redacts_bearer_value_in_unknown_field(self) -> None:
        out = redaction.redact({"random_field": "Bearer abc123def456ghi789"})
        self.assertEqual(out["random_field"], redaction.REDACTED_PLACEHOLDER)

    def test_redacts_email_field(self) -> None:
        """email is a credential per §3.7.1."""
        out = redaction.redact({"email": "test@example.com"})
        self.assertEqual(out["email"], redaction.REDACTED_PLACEHOLDER)

    def test_redact_url_strips_credential_query_params(self) -> None:
        url = "https://api.easyecom.io/foo?api_key=secret&item=xyz"
        out = redaction.redact_url(url)
        self.assertIn("api_key=***REDACTED***", out)
        self.assertIn("item=xyz", out)


class TestJsonPath(unittest.TestCase):
    def test_dot_access(self) -> None:
        self.assertEqual(jsonpath.get_path({"a": {"b": 1}}, "a.b"), [1])

    def test_brackets_iteration(self) -> None:
        payload = {"items": [{"sku": "A"}, {"sku": "B"}]}
        self.assertEqual(jsonpath.get_path(payload, "items[].sku"), ["A", "B"])

    def test_filter_predicate(self) -> None:
        payload = {
            "items": [{"type": "CGST", "amount": 9}, {"type": "SGST", "amount": 9}]
        }
        self.assertEqual(jsonpath.get_path(payload, "items[?type='CGST'].amount"), [9])

    def test_index_access(self) -> None:
        self.assertEqual(
            jsonpath.get_path({"items": ["a", "b", "c"]}, "items[1]"), ["b"]
        )

    def test_wildcard_synonym(self) -> None:
        payload = {"items": [{"sku": "A"}, {"sku": "B"}]}
        self.assertEqual(jsonpath.get_path(payload, "items[*].sku"), ["A", "B"])

    def test_recursive_descent(self) -> None:
        payload = {"a": {"hsn_code": 1234}, "b": [{"hsn_code": 5678}]}
        result = jsonpath.get_path(payload, "..hsn_code")
        self.assertIn(1234, result)
        self.assertIn(5678, result)

    def test_sum_path(self) -> None:
        payload = {"items": [{"amt": 10}, {"amt": 20.5}, {"amt": 5}]}
        self.assertAlmostEqual(jsonpath.sum_path(payload, "items[].amt"), 35.5)


if __name__ == "__main__":
    unittest.main()
