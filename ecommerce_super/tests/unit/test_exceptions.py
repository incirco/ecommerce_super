"""Unit tests for the §31.5 exception hierarchy."""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom import exceptions as exc


class TestExceptionHierarchy(unittest.TestCase):
    def test_all_inherit_from_base(self) -> None:
        for cls in (
            exc.EasyEcomAPIError,
            exc.EasyEcomAuthError,
            exc.EasyEcomRateLimitError,
            exc.EasyEcomTimeoutError,
            exc.EasyEcomServerError,
            exc.EasyEcomValidationError,
            exc.EasyEcomDuplicateError,
            exc.FieldMappingError,
            exc.SyncError,
            exc.WebhookError,
            exc.ReplayError,
            exc.ConfigurationError,
            exc.MultiCompanyError,
            exc.CompanyConcurrencyExceeded,
        ):
            self.assertTrue(
                issubclass(cls, exc.EasyEcomError), f"{cls.__name__} not in hierarchy"
            )

    def test_api_subclasses_are_api_errors(self) -> None:
        for cls in (
            exc.EasyEcomAuthError,
            exc.EasyEcomRateLimitError,
            exc.EasyEcomTimeoutError,
            exc.EasyEcomServerError,
            exc.EasyEcomValidationError,
            exc.EasyEcomDuplicateError,
        ):
            self.assertTrue(issubclass(cls, exc.EasyEcomAPIError))

    def test_retry_policy_classification(self) -> None:
        """Transient = retry; permanent = land Failed immediately. §6.3.8."""
        transient = {
            exc.EasyEcomRateLimitError,
            exc.EasyEcomServerError,
            exc.EasyEcomTimeoutError,
            exc.CompanyConcurrencyExceeded,
        }
        permanent = {
            exc.EasyEcomAuthError,
            exc.EasyEcomValidationError,
            exc.EasyEcomDuplicateError,
        }
        for cls in transient:
            self.assertEqual(
                cls.retry_policy, "transient", f"{cls.__name__} should be transient"
            )
        for cls in permanent:
            self.assertEqual(
                cls.retry_policy, "permanent", f"{cls.__name__} should be permanent"
            )

    def test_error_codes_present_and_stable(self) -> None:
        """error_code is the matcher key for Error Translation Library (§25).
        It must be present and stable on every class."""
        self.assertEqual(exc.EasyEcomError.error_code, "ECS_ERROR")
        self.assertEqual(exc.EasyEcomAPIError.error_code, "ECS_API_ERROR")
        self.assertEqual(exc.EasyEcomAuthError.error_code, "ECS_API_AUTH_ERROR")
        self.assertEqual(exc.EasyEcomRateLimitError.error_code, "ECS_API_RATE_LIMIT")
        self.assertEqual(exc.EasyEcomServerError.error_code, "ECS_API_SERVER_ERROR")
        self.assertEqual(exc.FieldMappingError.error_code, "ECS_FM_ERROR")
        self.assertEqual(exc.WebhookError.error_code, "ECS_WH_ERROR")

    def test_rate_limit_error_carries_retry_after(self) -> None:
        e = exc.EasyEcomRateLimitError("throttled", retry_after=60, status_code=429)
        self.assertEqual(e.retry_after, 60)
        self.assertEqual(e.status_code, 429)


if __name__ == "__main__":
    unittest.main()
