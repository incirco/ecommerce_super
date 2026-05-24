"""EasyEcom integration exception hierarchy.

Authoritative source: SPEC.md §31.5. Every subclass carries:
  - `error_code`: stable string for the Error Translation library matchers
  - `retry_policy`: "transient" (worker should retry with back-off) or
                    "permanent" (worker should land Failed immediately)

Never raise bare `Exception` from integration code (CLAUDE.md "Anti-patterns").
Always pick the most specific subclass.
"""

from __future__ import annotations

from typing import Any


class EasyEcomError(Exception):
    """Base class for all integration errors."""

    error_code: str = "ECS_ERROR"
    retry_policy: str = "permanent"


# ============ API client errors ============


class EasyEcomAPIError(EasyEcomError):
    error_code = "ECS_API_ERROR"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: dict | None = None,
        endpoint: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.endpoint = endpoint
        self.correlation_id = correlation_id


class EasyEcomAuthError(EasyEcomAPIError):
    """HTTP 401 — credentials invalid or JWT expired/invalid."""

    error_code = "ECS_API_AUTH_ERROR"
    retry_policy = "permanent"


class EasyEcomRateLimitError(EasyEcomAPIError):
    """HTTP 429 — rate-limit or daily-quota exceeded."""

    error_code = "ECS_API_RATE_LIMIT"
    retry_policy = "transient"

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class EasyEcomTimeoutError(EasyEcomAPIError):
    """TCP/HTTP timeout."""

    error_code = "ECS_API_TIMEOUT"
    retry_policy = "transient"


class EasyEcomServerError(EasyEcomAPIError):
    """HTTP 5xx response."""

    error_code = "ECS_API_SERVER_ERROR"
    retry_policy = "transient"


class EasyEcomValidationError(EasyEcomAPIError):
    """HTTP 4xx with a structured validation problem."""

    error_code = "ECS_API_VALIDATION_ERROR"
    retry_policy = "permanent"

    def __init__(
        self,
        message: str,
        *,
        validation_problems: list[dict] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.validation_problems = validation_problems or []


class EasyEcomDuplicateError(EasyEcomAPIError):
    """EE rejected the call because the entity already exists."""

    error_code = "ECS_API_DUPLICATE"
    retry_policy = "permanent"

    def __init__(
        self,
        message: str,
        *,
        existing_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.existing_id = existing_id


# ============ Field Mapping errors ============


class FieldMappingError(EasyEcomError):
    error_code = "ECS_FM_ERROR"


class FieldMappingCompileError(FieldMappingError):
    error_code = "ECS_FM_COMPILE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        rule_index: int | None = None,
        parse_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rule_index = rule_index
        self.parse_error = parse_error


class FieldMappingRuleError(FieldMappingError):
    error_code = "ECS_FM_RULE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        rule_id: str | None = None,
        erpnext_path: str | None = None,
        easyecom_path: str | None = None,
        transform: str | None = None,
        source_value: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.erpnext_path = erpnext_path
        self.easyecom_path = easyecom_path
        self.transform = transform
        self.source_value = source_value


class FieldMappingMissingRequiredError(FieldMappingError):
    error_code = "ECS_FM_MISSING_REQUIRED"

    def __init__(
        self,
        message: str,
        *,
        rule_id: str | None = None,
        field_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.field_name = field_name


class FieldMappingValidationError(FieldMappingError):
    error_code = "ECS_FM_VALIDATION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        rule_id: str | None = None,
        validate_against: str | None = None,
        invalid_value: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.validate_against = validate_against
        self.invalid_value = invalid_value


# ============ Sync errors ============


class SyncError(EasyEcomError):
    error_code = "ECS_SYNC_ERROR"


class SyncPreconditionError(SyncError):
    """Source record cannot be synced because a precondition is unmet
    (e.g. HSN code missing, customer not yet pushed)."""

    error_code = "ECS_SYNC_PRECONDITION"

    def __init__(self, message: str, *, precondition: str | None = None) -> None:
        super().__init__(message)
        self.precondition = precondition


class SyncConflictError(SyncError):
    """Bidirectional conflict resolution failed: two sides hold incompatible values."""

    error_code = "ECS_SYNC_CONFLICT"

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        erpnext_value: Any | None = None,
        easyecom_value: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.erpnext_value = erpnext_value
        self.easyecom_value = easyecom_value


class SyncCancelledError(SyncError):
    error_code = "ECS_SYNC_CANCELLED"


# ============ Webhook errors ============


class WebhookError(EasyEcomError):
    error_code = "ECS_WH_ERROR"


class WebhookTokenInvalidError(WebhookError):
    error_code = "ECS_WH_TOKEN_INVALID"


class WebhookIPNotAllowedError(WebhookError):
    error_code = "ECS_WH_IP_NOT_ALLOWED"


class WebhookTooOldError(WebhookError):
    error_code = "ECS_WH_TOO_OLD"


class WebhookDuplicateError(WebhookError):
    """Duplicate webhook detected via the (event_type, ee_event_id, company)
    UNIQUE constraint. Returned to EE as 200 OK so they stop retrying; not
    raised externally."""

    error_code = "ECS_WH_DUPLICATE"


# ============ Replay errors ============


class ReplayError(EasyEcomError):
    error_code = "ECS_REPLAY_ERROR"


class ReplayDryRunRequiredError(ReplayError):
    error_code = "ECS_REPLAY_DRY_RUN_REQUIRED"


class ReplayApprovalRequiredError(ReplayError):
    error_code = "ECS_REPLAY_APPROVAL_REQUIRED"

    def __init__(self, message: str, *, threshold: str | None = None) -> None:
        super().__init__(message)
        # threshold is 'records_count' | 'financial_impact'
        self.threshold = threshold


# ============ Configuration errors ============


class ConfigurationError(EasyEcomError):
    error_code = "ECS_CONFIG_ERROR"


class CredentialsMissingError(ConfigurationError):
    error_code = "ECS_CONFIG_CREDS_MISSING"

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class LocationNotMappedError(ConfigurationError):
    error_code = "ECS_CONFIG_LOCATION_NOT_MAPPED"

    def __init__(self, message: str, *, location_key: str | None = None) -> None:
        super().__init__(message)
        self.location_key = location_key


# ============ Multi-Company errors ============


class MultiCompanyError(EasyEcomError):
    error_code = "ECS_MC_ERROR"


class CompanyAccessDeniedError(MultiCompanyError):
    error_code = "ECS_MC_ACCESS_DENIED"

    def __init__(
        self,
        message: str,
        *,
        user: str | None = None,
        company: str | None = None,
    ) -> None:
        super().__init__(message)
        self.user = user
        self.company = company


class CompanyContextRequiredError(MultiCompanyError):
    error_code = "ECS_MC_CONTEXT_REQUIRED"


class CompanyConcurrencyExceeded(MultiCompanyError):
    """Per-Company concurrency cap reached; the worker should release the slot
    and re-enqueue with a short back-off (SPEC §6.3.7)."""

    error_code = "ECS_MC_CONCURRENCY_EXCEEDED"
    retry_policy = "transient"


# ============ SLA errors ============


class SLABreachError(EasyEcomError):
    """Not raised — constructed and persisted as an SLA Breach record."""

    error_code = "ECS_SLA_BREACH"
