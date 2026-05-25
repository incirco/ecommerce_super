"""EasyEcomClient — the single HTTP entry point for EasyEcom.

SPEC §3.6 / §31.4.1. No code outside this class talks to EasyEcom directly.
Every public method goes through `_request()` so rate limiting, JWT
refresh, idempotency, redaction, and API Call logging happen automatically.

Construction:
  EasyEcomClient(company=<frappe-company>, location_key=<ee-location>)

  - `company` may be None for foundational calls (§7.7).
  - `location_key` is required for all entity-sync calls; only None for
    /getAllLocation which is account-scoped.

Logging contract (§7.2):
  Every call writes exactly one EasyEcom API Call row, success or failure,
  with credentials redacted in the stored payload.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

import frappe
import requests

from ecommerce_super.easyecom.client.auth import (
    force_reauth,
    get_account,
    get_or_acquire_jwt,
)
from ecommerce_super.easyecom.client.endpoints import is_foundational
from ecommerce_super.easyecom.client.rate_limit import acquire_token
from ecommerce_super.easyecom.client.retry import classify_response, with_retry
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomAuthError,
    EasyEcomDuplicateError,
    EasyEcomRateLimitError,
    EasyEcomServerError,
    EasyEcomTimeoutError,
    EasyEcomValidationError,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.hashing import sha256_hex
from ecommerce_super.easyecom.utils.redaction import redact, redact_url


class EasyEcomClient:
    """Single class encapsulating every EasyEcom interaction.

    Per §31.4.1, every public method:
      - acquires a tier-aware rate-limit token,
      - sends both mandatory headers (x-api-key, Authorization: Bearer),
      - sends a request_id (UUID4) for cross-system trace correlation,
      - logs request and response to EasyEcom API Call with redaction,
      - honours the §3.6 retry policy via the retry wrapper.
    """

    def __init__(
        self,
        company: str | None = None,
        location_key: str | None = None,
    ) -> None:
        self.company = company
        self.location_key = location_key
        # Resolve the (single) Account up front — credentials live there.
        self._account = get_account()

    # ----- Public API (§31.4.1) -----

    def get_jwt(self) -> str:
        """Return the cached or freshly-acquired JWT for this client's location."""
        if not self.location_key:
            raise EasyEcomAuthError("get_jwt() requires a location_key.")
        return get_or_acquire_jwt(self.location_key, account=self._account)

    def refresh_jwt(self) -> str:
        """Force re-acquisition of the JWT (used by the 401 recovery path)."""
        if not self.location_key:
            raise EasyEcomAuthError("refresh_jwt() requires a location_key.")
        return force_reauth(self.location_key)

    def get(
        self,
        endpoint: str,
        params: dict | None = None,
        *,
        timeout: int = 60,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        return self._request(
            "GET",
            endpoint,
            params=params,
            payload=None,
            timeout=timeout,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    def post(
        self,
        endpoint: str,
        payload: dict,
        *,
        timeout: int = 60,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            endpoint,
            params=None,
            payload=payload,
            timeout=timeout,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    def paginated(
        self,
        endpoint: str,
        params: dict,
        *,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> Iterator[dict]:
        """Yield successive page responses from an EE bulk endpoint.

        EE returns a Next-Page URL as the pagination cursor (§31.4.1 note);
        we follow it (resolved against the base URL) until exhausted. Each
        yielded value is the parsed JSON body of one page.
        """
        params = dict(params or {})
        params.setdefault("page_size", page_size)

        correlation_id = new_correlation_id()
        current_url: str | None = (
            None  # First call uses endpoint+params; subsequent use Next-Page URL.
        )
        pages_seen = 0

        while True:
            if current_url is None:
                page = self.get(endpoint, params=params, correlation_id=correlation_id)
            else:
                page = self._request(
                    "GET",
                    endpoint=current_url,
                    params=None,
                    payload=None,
                    timeout=60,
                    correlation_id=correlation_id,
                    _is_absolute_url=True,
                )

            yield page
            pages_seen += 1

            next_url = self._extract_next_page_url(page)
            if not next_url or (max_pages is not None and pages_seen >= max_pages):
                return
            current_url = next_url

    # ----- Internal -----

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None,
        payload: dict | None,
        timeout: int,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        _is_absolute_url: bool = False,
    ) -> dict:
        correlation_id = correlation_id or new_correlation_id()
        sub_correlation_id = str(uuid.uuid4())

        # Foundational endpoints are account-scoped (§7.7).
        foundational = is_foundational(endpoint)

        # Throttle (raises EasyEcomRateLimitError on daily quota exhaustion).
        acquire_token(self._account.name, self.location_key)

        # Build URL.
        if _is_absolute_url:
            url = endpoint
            # Strip the base URL for logging-as-path if EE returned an
            # absolute Next-Page URL on the same host.
            log_endpoint = (
                endpoint.replace(self._account.api_endpoint, "")
                if endpoint.startswith(self._account.api_endpoint)
                else endpoint
            )
        else:
            url = f"{self._account.api_endpoint}{endpoint}"
            log_endpoint = endpoint

        request_id = str(uuid.uuid4())
        creds = self._account.get_credentials_for_client()

        def do_call() -> dict:
            # JWT is required for everything except the token-acquisition
            # call itself, which goes through auth.acquire_jwt directly.
            headers = {
                "x-api-key": creds["api_key"],
                "Accept": "application/json",
                "X-Request-Id": request_id,
            }
            if not foundational or endpoint != "/access/token":
                # Foundational getAllLocation still needs the JWT (auth scope
                # is account, but EE requires Bearer on the request).
                if self.location_key:
                    headers["Authorization"] = f"Bearer {self.get_jwt()}"
                elif foundational:
                    # No location → use the account's default_location_key
                    # to acquire a JWT.
                    default_loc = self._account.default_location_key
                    if default_loc:
                        # Look up location_key from the Link
                        actual_key = frappe.db.get_value(
                            "EasyEcom Location", default_loc, "location_key"
                        )
                        if actual_key:
                            headers["Authorization"] = (
                                f"Bearer {get_or_acquire_jwt(actual_key, account=self._account)}"
                            )

            if idempotency_key and method != "GET":
                headers["X-Idempotency-Key"] = idempotency_key
            if method != "GET":
                headers["Content-Type"] = "application/json"

            try:
                resp = requests.request(
                    method,
                    url,
                    params=params,
                    json=payload if method != "GET" else None,
                    headers=headers,
                    timeout=timeout,
                )
            except requests.exceptions.Timeout as e:
                self._log_failed_call(
                    method=method,
                    log_endpoint=log_endpoint,
                    url=url,
                    headers=headers,
                    payload=payload,
                    foundational=foundational,
                    correlation_id=correlation_id,
                    sub_correlation_id=sub_correlation_id,
                    status="Timeout",
                    error_class="EasyEcomTimeoutError",
                    error_message=str(e),
                )
                raise EasyEcomTimeoutError(str(e), endpoint=log_endpoint) from e

            try:
                response_body = resp.json() if resp.content else {}
            except ValueError:
                response_body = {"raw": resp.text[:1000]}

            status_class = classify_response(resp.status_code)

            # Log every call, success or failure (§7.2).
            log_api_call(
                account=self._account.name,
                company=self.company if not foundational else None,
                is_foundational=foundational,
                location_key=self.location_key,
                endpoint=log_endpoint,
                http_method=method,
                request_url=url,
                request_headers=headers,
                request_payload=payload if method != "GET" else params,
                response_status=resp.status_code,
                response_headers=dict(resp.headers),
                response_payload=response_body,
                status={"success": "Success"}.get(status_class, "Failed"),
                correlation_id=correlation_id,
                sub_correlation_id=sub_correlation_id,
                parent_sync_record=None,
                parent_queue_job=None,
                error_class=(
                    None
                    if status_class == "success"
                    else _exception_name_for_status(resp.status_code)
                ),
                error_message=(
                    None if status_class == "success" else f"HTTP {resp.status_code}"
                ),
            )

            if status_class == "success":
                return response_body
            if status_class == "auth":
                raise EasyEcomAuthError(
                    f"HTTP 401 from {log_endpoint}",
                    status_code=resp.status_code,
                    response_body=response_body,
                    endpoint=log_endpoint,
                    correlation_id=correlation_id,
                )
            if status_class == "rate_limit":
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                raise EasyEcomRateLimitError(
                    f"HTTP 429 from {log_endpoint}",
                    retry_after=retry_after,
                    status_code=resp.status_code,
                    response_body=response_body,
                    endpoint=log_endpoint,
                    correlation_id=correlation_id,
                )
            if status_class == "transient":
                raise EasyEcomServerError(
                    f"HTTP {resp.status_code} from {log_endpoint}",
                    status_code=resp.status_code,
                    response_body=response_body,
                    endpoint=log_endpoint,
                    correlation_id=correlation_id,
                )
            # permanent — 4xx other than 401/429
            if _is_duplicate(response_body):
                raise EasyEcomDuplicateError(
                    f"HTTP {resp.status_code} duplicate from {log_endpoint}",
                    existing_id=_extract_existing_id(response_body),
                    status_code=resp.status_code,
                    response_body=response_body,
                    endpoint=log_endpoint,
                    correlation_id=correlation_id,
                )
            raise EasyEcomValidationError(
                f"HTTP {resp.status_code} from {log_endpoint}",
                validation_problems=_extract_validation_problems(response_body),
                status_code=resp.status_code,
                response_body=response_body,
                endpoint=log_endpoint,
                correlation_id=correlation_id,
            )

        return with_retry(do_call, on_auth_failure=self._on_auth_failure)

    def _on_auth_failure(self) -> None:
        """Re-auth callback used by retry.with_retry on 401."""
        if self.location_key:
            self.refresh_jwt()

    def _log_failed_call(
        self,
        *,
        method: str,
        log_endpoint: str,
        url: str,
        headers: dict,
        payload: Any,
        foundational: bool,
        correlation_id: str,
        sub_correlation_id: str,
        status: str,
        error_class: str,
        error_message: str,
    ) -> None:
        log_api_call(
            account=self._account.name,
            company=self.company if not foundational else None,
            is_foundational=foundational,
            location_key=self.location_key,
            endpoint=log_endpoint,
            http_method=method,
            request_url=url,
            request_headers=headers,
            request_payload=payload,
            response_status=None,
            response_headers=None,
            response_payload=None,
            status=status,
            correlation_id=correlation_id,
            sub_correlation_id=sub_correlation_id,
            error_class=error_class,
            error_message=error_message,
        )

    @staticmethod
    def _extract_next_page_url(page: dict) -> str | None:
        """EE returns the next-page cursor as `next_page_url` in v2 bulk
        endpoints. Other shape variants surface in spec_sections; for now
        we look for the common case."""
        for key in ("next_page_url", "nextPageUrl", "next_page", "next"):
            val = page.get(key)
            if val:
                return val
        return None


# ----- Module-level logging helper used by both client.py and auth.py -----


def log_api_call(
    *,
    account: str,
    company: str | None,
    is_foundational: bool,
    location_key: str | None,
    endpoint: str,
    http_method: str,
    request_url: str,
    request_headers: dict | None,
    request_payload: Any,
    response_status: int | None,
    response_headers: dict | None,
    response_payload: Any,
    status: str,
    correlation_id: str | None = None,
    sub_correlation_id: str | None = None,
    parent_sync_record: str | None = None,
    parent_queue_job: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> str:
    """Write an EasyEcom API Call row with all credentials/PII redacted.

    Returns the inserted docname. This function is the SINGLE log-write
    path for outbound calls — the EasyEcomClient and auth module both
    funnel through here so we cannot accidentally bypass redaction.
    """
    redacted_headers = redact(request_headers or {})
    redacted_request = redact(request_payload) if request_payload is not None else None
    redacted_response = (
        redact(response_payload) if response_payload is not None else None
    )
    redacted_response_headers = (
        redact(response_headers or {}) if response_headers else None
    )

    request_payload_text = (
        frappe.as_json(redacted_request) if redacted_request is not None else None
    )

    doc = frappe.new_doc("EasyEcom API Call")
    doc.update(
        {
            "easyecom_account": account,
            "company": company,
            "is_foundational": 1 if is_foundational else 0,
            "location_key": location_key,
            "endpoint": endpoint,
            "http_method": http_method,
            "request_url": redact_url(request_url)[:2000],
            "request_headers": frappe.as_json(redacted_headers),
            "request_payload": request_payload_text,
            "request_payload_hash": sha256_hex(redacted_request or {}),
            "response_status_code": response_status,
            "response_headers": (
                frappe.as_json(redacted_response_headers)
                if redacted_response_headers
                else None
            ),
            "response_payload": (
                frappe.as_json(redacted_response)
                if redacted_response is not None
                else None
            ),
            "response_payload_hash": (
                sha256_hex(redacted_response or {})
                if redacted_response is not None
                else None
            ),
            "status": status,
            "attempted_at": frappe.utils.now_datetime(),
            "completed_at": frappe.utils.now_datetime(),
            "attempt_number": 1,
            "correlation_id": correlation_id or new_correlation_id(),
            "sub_correlation_id": sub_correlation_id or str(uuid.uuid4()),
            "parent_sync_record": parent_sync_record,
            "parent_queue_job": parent_queue_job,
            "error_class": error_class,
            "error_message": error_message,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _exception_name_for_status(status_code: int) -> str:
    if status_code == 401:
        return "EasyEcomAuthError"
    if status_code == 429:
        return "EasyEcomRateLimitError"
    if 500 <= status_code < 600:
        return "EasyEcomServerError"
    return "EasyEcomValidationError"


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_duplicate(response_body: Any) -> bool:
    if not isinstance(response_body, dict):
        return False
    error_str = str(
        response_body.get("error", "") or response_body.get("message", "")
    ).lower()
    return "duplicate" in error_str or "already exists" in error_str


def _extract_existing_id(response_body: dict) -> str | None:
    for key in ("existing_id", "id", "existingId"):
        if key in response_body:
            return str(response_body[key])
    return None


def _extract_validation_problems(response_body: Any) -> list[dict]:
    if not isinstance(response_body, dict):
        return []
    for key in ("errors", "validation_errors", "problems"):
        val = response_body.get(key)
        if isinstance(val, list):
            return [
                item if isinstance(item, dict) else {"message": str(item)}
                for item in val
            ]
    return []
