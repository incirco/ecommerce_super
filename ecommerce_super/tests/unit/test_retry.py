"""Unit tests for the client retry policy (§3.6 / §31.4)."""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.client import retry
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAuthError,
    EasyEcomRateLimitError,
    EasyEcomServerError,
    EasyEcomTimeoutError,
    EasyEcomValidationError,
)


class TestClassifyResponse(unittest.TestCase):
    def test_2xx_is_success(self) -> None:
        for code in (200, 201, 204, 299):
            self.assertEqual(retry.classify_response(code), "success")

    def test_401_is_auth(self) -> None:
        self.assertEqual(retry.classify_response(401), "auth")

    def test_429_is_rate_limit(self) -> None:
        self.assertEqual(retry.classify_response(429), "rate_limit")

    def test_5xx_is_transient(self) -> None:
        for code in (500, 502, 503, 504):
            self.assertEqual(retry.classify_response(code), "transient")

    def test_other_4xx_is_permanent(self) -> None:
        for code in (400, 403, 404, 422):
            self.assertEqual(retry.classify_response(code), "permanent")


class TestComputeBackoff(unittest.TestCase):
    def test_exponential_until_60s_cap(self) -> None:
        # attempt 1 → ~1s, 2 → ~2s, 3 → ~4s, ..., capped at 60s
        self.assertTrue(1.0 <= retry.compute_backoff(1) < 2.0)
        self.assertTrue(2.0 <= retry.compute_backoff(2) < 3.0)
        self.assertTrue(4.0 <= retry.compute_backoff(3) < 5.0)
        # Cap: 2^9 = 512s, but max 60s. attempt 10 → 60s + jitter.
        self.assertTrue(
            retry.MAX_BACKOFF_S <= retry.compute_backoff(10) < retry.MAX_BACKOFF_S + 1.0
        )


class TestWithRetry(unittest.TestCase):
    def test_succeeds_on_first_try(self) -> None:
        calls = [0]

        def call():
            calls[0] += 1
            return {"ok": True}

        result = retry.with_retry(call)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0], 1)

    def test_retries_transient_then_succeeds(self) -> None:
        calls = [0]

        def call():
            calls[0] += 1
            if calls[0] < 3:
                raise EasyEcomServerError("flaky 503", status_code=503)
            return {"ok": True}

        result = retry.with_retry(call)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0], 3)

    def test_does_not_retry_permanent_error(self) -> None:
        calls = [0]

        def call():
            calls[0] += 1
            raise EasyEcomValidationError("bad input", status_code=400)

        with self.assertRaises(EasyEcomValidationError):
            retry.with_retry(call)
        self.assertEqual(calls[0], 1)

    def test_on_auth_failure_called_once(self) -> None:
        """On 401, the callback fires once. If 401 persists after re-auth,
        the second 401 is raised."""
        calls = [0]
        reauth_calls = [0]

        def call():
            calls[0] += 1
            raise EasyEcomAuthError("401")

        def on_auth():
            reauth_calls[0] += 1

        with self.assertRaises(EasyEcomAuthError):
            retry.with_retry(call, on_auth_failure=on_auth)
        self.assertEqual(reauth_calls[0], 1)
        # 2 call attempts: original + post-reauth retry.
        self.assertEqual(calls[0], 2)

    def test_rate_limit_honours_retry_after(self) -> None:
        """When the rate-limit error carries retry_after, the backoff
        is at least that long."""
        import time

        calls = [0]
        timestamps = []

        def call():
            calls[0] += 1
            timestamps.append(time.monotonic())
            if calls[0] < 2:
                raise EasyEcomRateLimitError("429", retry_after=1)
            return {"ok": True}

        retry.with_retry(call)
        elapsed = timestamps[1] - timestamps[0]
        self.assertGreaterEqual(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
