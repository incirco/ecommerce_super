"""JWT acquisition, caching, refresh and the day-85 renewal scheduler hook.

SPEC §3.6: EasyEcom JWTs are 90-day. Per-location cache lives on
EasyEcom Location.jwt_token (encrypted at rest via the controller). A
daily scheduled job renews aging JWTs at day 85 (5-day margin); on any
401 the client re-authenticates immediately as a fallback.

This module is the only place that calls the /access/token endpoint. The
EasyEcomClient uses `get_or_acquire_jwt` and `force_reauth` from here.
"""

from __future__ import annotations

import random
from typing import Any

import frappe
import requests

from ecommerce_super.easyecom.client.endpoints import TOKEN
from ecommerce_super.easyecom.client.rate_limit import acquire_token
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomAuthError,
    EasyEcomRateLimitError,
    EasyEcomTimeoutError,
)

# Token-acquisition rate limit per location, per §31.3.1: "1 call per
# location per 60 seconds maximum". The token bucket above enforces the
# general tier rate; this is a secondary guard specifically for the
# token endpoint.
TOKEN_CACHE_LOCKOUT_KEY = "easyecom:token-lockout:{location_key}"


def get_account() -> Any:
    """Return the single enabled EasyEcom Account, or raise if absent.

    There is exactly one Account per deployment (§3.1). If multiple
    Accounts exist (shouldn't happen but defensively guard), pick the
    first enabled one and log.
    """
    name = frappe.db.get_value(
        "EasyEcom Account", filters={"enabled": 1}, fieldname="name"
    )
    if not name:
        raise EasyEcomAuthError("No enabled EasyEcom Account is configured.")
    return frappe.get_doc("EasyEcom Account", name)


def get_or_acquire_jwt(location_key: str, *, account=None) -> str:
    """Return a cached JWT for this location, acquiring if absent/expired.

    Called by EasyEcomClient at the top of every authenticated request.
    Avoids re-acquiring tokens that are still valid (§3.6: token caching
    is mandatory).
    """
    account = account or get_account()
    location = _get_location_or_raise(location_key)

    cached = location.get_jwt_plaintext()
    if cached and not _is_expired(location):
        return cached

    return acquire_jwt(account, location)


def force_reauth(location_key: str) -> str:
    """Clear the cached JWT and acquire a fresh one. Called by the client
    on 401 to recover from an unexpected invalidation (§3.6)."""
    account = get_account()
    location = _get_location_or_raise(location_key)
    location.clear_jwt()
    return acquire_jwt(account, location)


def acquire_jwt(account, location) -> str:
    """POST /access/token and cache the result. The actual HTTP call here
    intentionally does NOT go through EasyEcomClient.post — that would
    create a chicken-and-egg dependency (the client needs a JWT to make
    any call, including the token call). The token call carries only the
    x-api-key header and the credentials in the body.

    Logs to EasyEcom API Call as is_foundational=1, company=blank per §7.7.
    """
    # Honour the per-location token-acquisition rate limit (§31.3.1).
    _check_token_lockout(location.location_key)

    # Consume one rate-limit token from the account's tier budget.
    acquire_token(account.name, location.location_key)

    creds = account.get_credentials_for_client()
    url = f"{account.api_endpoint}{TOKEN}"
    headers = {
        "x-api-key": creds["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "email": creds["email"],
        "password": creds["password"],
        "location_key": location.location_key,
    }

    # The API Call log row is written by the caller pattern in client.py;
    # for the foundational token call we write inline because client.py
    # is not on the stack here.
    from ecommerce_super.easyecom.client.client import log_api_call

    try:
        response = requests.post(url, json=body, headers=headers, timeout=30)
    except requests.exceptions.Timeout as e:
        log_api_call(
            account=account.name,
            company=None,
            is_foundational=True,
            location_key=location.location_key,
            endpoint=TOKEN,
            http_method="POST",
            request_url=url,
            request_headers=headers,
            request_payload=body,
            response_status=None,
            response_headers=None,
            response_payload=None,
            status="Timeout",
            error_class="EasyEcomTimeoutError",
            error_message=str(e),
        )
        raise EasyEcomTimeoutError(f"Token acquisition timed out: {e}") from e

    try:
        response_payload = response.json() if response.content else {}
    except ValueError:
        response_payload = {"raw": response.text[:1000]}

    # Classify the failure for the API Call log so dashboards/queries can
    # distinguish "cooldown lockout (transient, harmless)" from "credentials
    # rejected (permanent, blocks every flow)". Generic 4xx/5xx fall through
    # to the catch-all EasyEcomAPIError name.
    if response.ok:
        _error_class = None
    elif response.status_code == 401:
        _error_class = "EasyEcomAuthError"
    elif response.status_code == 403:
        _error_class = "EasyEcomRateLimitError"
    else:
        _error_class = "EasyEcomAPIError"
    log_api_call(
        account=account.name,
        company=None,
        is_foundational=True,
        location_key=location.location_key,
        endpoint=TOKEN,
        http_method="POST",
        request_url=url,
        request_headers=headers,
        request_payload=body,
        response_status=response.status_code,
        response_headers=dict(response.headers),
        response_payload=response_payload,
        status="Success" if response.ok else "Failed",
        error_class=_error_class,
        error_message=None if response.ok else f"HTTP {response.status_code}",
    )

    if response.status_code == 401:
        raise EasyEcomAuthError(
            f"EasyEcom rejected credentials for {location.location_key}: HTTP 401.",
            status_code=401,
            response_body=response_payload,
            endpoint=TOKEN,
        )
    if response.status_code == 403:
        # SPEC §31.3.1: EasyEcom rate-limits /access/token to 1 call per
        # location per 60s and enforces the cooldown server-side with HTTP
        # 403 once the limit is hit. Distinct from an auth failure — the
        # credentials are fine; the FDE just retried too soon. Classifying
        # as RateLimit lets test_connection.py surface the SPEC's "wait
        # ~60s" message instead of the generic "ECS_API_ERROR" the FDE
        # cannot act on (gh#2). Also reseat the local lockout so the next
        # call short-circuits before reaching EE even if our in-process
        # cache was cleared (process restart, fresh worker, etc.).
        _set_token_lockout(location.location_key, seconds=60)
        raise EasyEcomRateLimitError(
            f"Token cooldown for {location.location_key}: HTTP 403. "
            "EasyEcom permits one /access/token call per location per 60s "
            "(§31.3.1). Wait ~60s and retry.",
            status_code=403,
            response_body=response_payload,
            endpoint=TOKEN,
            retry_after=60,
        )
    if not response.ok:
        raise EasyEcomAPIError(
            f"Token acquisition failed for {location.location_key}: HTTP {response.status_code}.",
            status_code=response.status_code,
            response_body=response_payload,
            endpoint=TOKEN,
        )

    # EasyEcom's /access/token response shape (probed against real EE prod):
    #   {
    #     "data": {
    #       "companyname": "...", "userName": "...", "time_zone": "...",
    #       "token": { "jwt_token": "<the actual JWT>", ... }
    #     },
    #     "message": null
    #   }
    # The `token` field is a DICT, not a string; the JWT lives at
    # data.token.jwt_token. We also accept the simpler string shape (data.token
    # or top-level token/jwt) for robustness against future EE shape changes.
    data_envelope = (
        response_payload.get("data") if isinstance(response_payload, dict) else None
    )
    token_field = (
        data_envelope.get("token") if isinstance(data_envelope, dict) else None
    )
    jwt: str | None = None
    if isinstance(token_field, dict):
        jwt = (
            token_field.get("jwt_token")
            or token_field.get("token")
            or token_field.get("jwt")
        )
    elif isinstance(token_field, str) and token_field:
        jwt = token_field
    if not jwt and isinstance(data_envelope, dict):
        jwt = (
            data_envelope.get("jwt")
            if isinstance(data_envelope.get("jwt"), str)
            else None
        )
    if not jwt and isinstance(response_payload, dict):
        top_token = response_payload.get("token") or response_payload.get("jwt")
        jwt = top_token if isinstance(top_token, str) else None
    if not jwt:
        # JWT extraction failed despite HTTP 200 — surface a precise error
        # so the FDE can diff the actual EE response shape against this
        # parser. The API Call row was logged Success above (HTTP was 200);
        # the application-level failure surfaces here.
        keys = (
            list(response_payload.keys())
            if isinstance(response_payload, dict)
            else "non-dict response"
        )
        data_keys = (
            list(data_envelope.keys()) if isinstance(data_envelope, dict) else None
        )
        raise EasyEcomAuthError(
            f"Token response shape unrecognised for {location.location_key}. "
            f"Top-level keys: {keys}; data keys: {data_keys}. "
            "Expected token at response.data.token (or response.token).",
            status_code=response.status_code,
            response_body=response_payload,
            endpoint=TOKEN,
        )

    # Cache (encrypts via the Location controller, §3.7.2).
    # expires_in may live at top level or inside the data envelope; default
    # to EasyEcom's documented 90-day validity (§3.6) when absent.
    expires_in_s = int(
        response_payload.get("expires_in")
        or (isinstance(data_envelope, dict) and data_envelope.get("expires_in"))
        or (90 * 24 * 3600)
    )
    validity_days = max(int(expires_in_s / 86400), 1)
    location.set_jwt(jwt, validity_days=validity_days)

    # Update connection_status on the Account (informational).
    frappe.db.set_value(
        "EasyEcom Account", account.name, "connection_status", "Connected"
    )
    frappe.db.set_value(
        "EasyEcom Account",
        account.name,
        "last_successful_sync_at",
        frappe.utils.now_datetime(),
    )
    frappe.db.commit()

    # Apply the per-location token-acquisition lockout (60s, §31.3.1).
    _set_token_lockout(location.location_key, seconds=60)

    return jwt


def renew_aging_jwts() -> int:
    """Scheduler hook (§3.6, daily 02:00 IST). Renew JWTs that have reached
    85 days of age. Spreads renewals via random jitter so accounts with many
    locations don't fan out a thundering herd of token calls."""
    from ecommerce_super.easyecom.doctype.easyecom_location.easyecom_location import (
        get_aging_locations,
    )

    candidates = get_aging_locations()
    if not candidates:
        return 0

    account = get_account()
    renewed = 0
    for row in candidates:
        # Jitter: 0-3600s spread across the renewal window.
        import time as _time

        _time.sleep(random.randint(0, 60))
        try:
            location = frappe.get_doc("EasyEcom Location", row["name"])
            acquire_jwt(account, location)
            renewed += 1
        except Exception as e:
            frappe.log_error(
                title=f"EasyEcom JWT renewal failed for {row['name']}",
                message=f"{type(e).__name__}: {e}",
            )

    return renewed


# ----- Helpers -----


def _get_location_or_raise(location_key: str):
    name = frappe.db.get_value(
        "EasyEcom Location", {"location_key": location_key}, "name"
    )
    if not name:
        raise EasyEcomAuthError(
            f"EasyEcom Location with key {location_key!r} not found."
        )
    return frappe.get_doc("EasyEcom Location", name)


def _is_expired(location) -> bool:
    """True if the cached JWT has passed jwt_expires_at."""
    if not location.jwt_expires_at:
        return True
    # frappe.utils.get_datetime handles datetime objects, strings, and None.
    expires = frappe.utils.get_datetime(location.jwt_expires_at)
    return expires <= frappe.utils.now_datetime()


def _check_token_lockout(location_key: str) -> None:
    """Raise if a token call was made for this location in the last 60s.

    Per §31.3.1: EasyEcom rate-limits /access/token to 1 call per location
    per 60 seconds. We pre-empt that with a local cache. This is a rate-
    limit condition, NOT an auth failure — callers (especially the
    interactive Test Connection action) must be able to distinguish them.
    """
    key = TOKEN_CACHE_LOCKOUT_KEY.format(location_key=location_key)
    if frappe.cache().get_value(key):
        raise EasyEcomRateLimitError(
            f"Token call rate-limited for {location_key}: 1 call per 60s "
            "per §31.3.1. Wait a moment and retry.",
            retry_after=60,
        )


def _set_token_lockout(location_key: str, seconds: int) -> None:
    key = TOKEN_CACHE_LOCKOUT_KEY.format(location_key=location_key)
    frappe.cache().set_value(key, 1, expires_in_sec=seconds)
