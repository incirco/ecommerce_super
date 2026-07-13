"""§11.5.1 Mode 1 — Custom GSP endpoints exposed to EE.

When the EE-side FDE clicks "Generate Invoice", EE calls our endpoints
as if we were a third-party Custom GSP. We:
  1. Resolve the EE order to an ERPNext Sales Invoice (find or create)
  2. Submit the SI (India Compliance requires submitted SI to mint IRN)
  3. Call India Compliance's generate_e_invoice / generate_e_waybill
  4. Return the IRN/QR/PDF in the response body — bytewise matching
     EE's contract

Three endpoints (all whitelisted with allow_guest=True since EE isn't
a Frappe user — auth is via Basic/Bearer headers, not Frappe session):

  POST /api/method/...gsp.gettoken          — Basic → Bearer mint
  POST /api/method/...gsp.einvoice_update   — Bearer + EE order JSON → IRN
  POST /api/method/...gsp.ewaybill_update   — Bearer + EE order JSON → eway

Failure modes:
  - Auth failure → HTTP 401
  - Payload validation (missing reference_code, etc.) → HTTP 422
  - SI find/create failure (missing Customer/Item Map) → HTTP 422 + clear msg
  - India Compliance NIC rejection → HTTP 422 with NIC error detail
  - NIC timeout / 5xx → HTTP 502 with retry suggestion

Idempotency:
  - Keyed on EE's `invoice_id` (always populated, the natural key)
  - Re-hit with same invoice_id → return cached IRN, NEVER re-mint
    (re-minting on NIC IRP creates a duplicate IRN that cannot be
    deleted — the ONLY remediation is calling NIC support)
"""
from __future__ import annotations

import json
from functools import wraps
from typing import Any

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
    EasyEcomGSPAuthError,
    issue_bearer,
    validate_basic_auth,
    validate_bearer,
)


# ============================================================
# gh#123 — Pre-auth header normalisation for GSP routes
# ============================================================
#
# Frappe's validate_auth() (frappe/auth.py) runs during request init,
# BEFORE dispatching to the whitelisted method. For an
# `Authorization: Basic <b64>` header it base64-decodes to `key:secret`,
# looks up a User whose api_key matches `key`, finds none, and raises
# `AuthenticationError`. `@whitelist(allow_guest=True)` governs
# permission, not credential validation — so a Basic header on our
# /gettoken endpoint dies before we ever see it.
#
# Same trap for Bearer on /einvoice/update + /ewaybill/update: even
# though Frappe's api_key path skips Bearer, the tail of validate_auth
# checks "any 2-part Authorization header AND session still Guest → raise"
# (the same shape as gh#1's webhook fix).
#
# Fix (mirrors gh#1 `normalise_webhook_auth_header`): before validate_auth
# runs, we shift the header out of `HTTP_AUTHORIZATION` into a custom
# stash environ key `HTTP_ECS_GSP_AUTHORIZATION`. Frappe then sees no
# Authorization header and skips its check. The GSP endpoints below read
# from the stash first, fall back to the standard `Authorization` header
# (so a bypassing test or an already-fixed environment still works).
# Non-GSP paths are untouched.

# Path suffixes the GSP endpoints resolve to under any site mount.
_GSP_PATH_SUFFIXES = (
    "/api/method/ecommerce_super.easyecom.api.gsp.gettoken",
    "/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update",
    "/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update",
)

# gh#130 — EE's Custom GSP client calls ROOT paths at the site root
# (POST <site>/gettoken, /einvoice/update, /ewaybill/update) — not the
# dotted /api/method/... URLs Frappe normally exposes. The EE "Add
# Channel → Custom GSP" form only takes a single Base URL; EE appends
# the sub-paths itself. We rewrite the WSGI PATH_INFO on the way in so
# Frappe's normal route dispatcher lands on the correct whitelisted
# method.
_GSP_ROOT_PATH_MAP = {
    "/gettoken":         "/api/method/ecommerce_super.easyecom.api.gsp.gettoken",
    "/einvoice/update":  "/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update",
    "/ewaybill/update":  "/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update",
}

# WSGI environ key used to stash the Authorization value past Frappe's
# validate_auth. Not a real HTTP header — Frappe upcase-prefixes real
# headers with HTTP_ but this key is set by our own hook so validate_auth
# can't observe it, and downstream we read the environ directly.
_GSP_AUTH_STASH_KEY = "HTTP_ECS_GSP_AUTHORIZATION"


def rewrite_gsp_root_paths() -> None:
    """gh#130 pre-dispatch hook: rewrite EE's root-path calls to the
    dotted /api/method/... URLs Frappe's route dispatcher understands.

    EE's Custom GSP client is hard-coded (verified 2026-07-08 with EE
    tech) to call:

        POST <base>/gettoken
        POST <base>/einvoice/update
        POST <base>/ewaybill/update

    where <base> is the single Base URL the FDE configures in EE's
    'Add Channel → Custom GSP' form. EE does NOT append a dotted
    method path. Without this rewrite the request falls through to
    the website router, which has no page at those root paths and
    returns Frappe's 'Session Expired' HTML → EE sees an opaque 401
    and never reaches gettoken() / einvoice_update() / ewaybill_update().

    Runs BEFORE normalise_gsp_auth_header so that hook's suffix check
    (which looks for /api/method/... paths) also matches after we
    rewrite.

    Must be registered BEFORE normalise_gsp_auth_header in hooks.py's
    before_request list.
    """
    try:
        request = getattr(frappe.local, "request", None)
        if request is None:
            return
        path = (getattr(request, "path", "") or "")
        new_path = _GSP_ROOT_PATH_MAP.get(path)
        if not new_path:
            return
        # gh#130 crash post-mortem (2026-07-09):
        #   werkzeug's Request.__init__ copies PATH_INFO out of environ
        #   into `self.path` as a **plain instance attribute** (see
        #   werkzeug/sansio/request.py: `self.path = "/" + path.lstrip("/")`).
        #   It is NOT a cached_property that reads back from environ on
        #   subsequent access. So:
        #     - Mutating `environ["PATH_INFO"]` alone does NOT change
        #       `request.path` — Frappe's dispatcher (`request.path.startswith("/api/")`)
        #       still sees the ORIGINAL root path and falls through to the
        #       website handler.
        #     - `delattr(request, "path")` REMOVES the instance attribute,
        #       so every subsequent `request.path` access raises
        #       AttributeError → Frappe's exception handler ALSO reads
        #       request.path → cascading crash → bare 500 (werkzeug's
        #       default, not Frappe's styled page). This is what Garv
        #       reported on mmpl16 + ee-uat, 2026-07-09.
        #
        # The correct rewrite is a plain attribute assignment. We also
        # keep the PATH_INFO mutation for downstream code that reads
        # environ directly (werkzeug's Map.bind_to_environ in
        # frappe.api.handle uses environ, not request.path).
        request.path = new_path
        request.environ["PATH_INFO"] = new_path
    except Exception:
        # before_request must NEVER block the request.
        return


def _write_gsp_flat_response(payload: dict[str, Any], http_status: int) -> None:
    """Write the payload's keys directly onto `frappe.local.response` so
    the emitted HTTP JSON is FLAT ({"status": 200, "access_token": ...})
    and pop the default `message` wrapper so it doesn't survive.

    gh#130: EE's contract (guide §3) reads response fields at the top
    level, not under `{"message": {...}}`. Frappe's default handler
    would re-wrap our return value under `message` — so the endpoints
    below use `_gsp_endpoint` (decorator) which invokes this helper
    then returns None to prevent the wrap.
    """
    frappe.local.response["http_status_code"] = http_status
    # gh#142: pop Frappe's default `message` wrapper BEFORE writing the
    # payload keys — the original order dropped OUR intentional `message`
    # field on error responses (bare `{"status":422}` with no reason,
    # observed on SO-2610382 einvoice attempt 2026-07-09).
    frappe.local.response.pop("message", None)
    for key, value in payload.items():
        frappe.local.response[key] = value


def _gsp_endpoint(fn):
    """gh#130 endpoint decorator: transforms a `return {"status": ...}`
    dict from a GSP handler into a FLAT `frappe.local.response` write
    and returns None to prevent Frappe's handler layer from re-wrapping
    it in `{"message": {...}}`.

    gh#147: also logs an EasyEcom API Call row (direction=Inbound) with
    request + response snapshots on EVERY hit — 2xx, 4xx, 5xx alike —
    so successful calls also leave an audit trail.
    """
    endpoint_path = f"/{fn.__name__.replace('_', '/')}"

    @wraps(fn)
    def wrapper(*args, **kwargs):
        import time
        started = time.time()
        result = None
        exc_class: str | None = None
        exc_message: str | None = None
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            # Never let a handler crash escape unlogged. The outer
            # Frappe exception path will still shape the 500 response;
            # we just capture the entry in the inbound log.
            exc_class = type(exc).__name__
            exc_message = str(exc)
            _log_inbound_gsp_call(
                endpoint=endpoint_path,
                started_at=started,
                result=None,
                error_class=exc_class,
                error_message=exc_message,
            )
            raise
        try:
            if isinstance(result, dict):
                http_status = int(result.get("status") or 200)
                _write_gsp_flat_response(result, http_status=http_status)
                _log_inbound_gsp_call(
                    endpoint=endpoint_path,
                    started_at=started,
                    result=result,
                )
                return None
            if result is None:
                # Handler already wrote to frappe.local.response.
                _log_inbound_gsp_call(
                    endpoint=endpoint_path,
                    started_at=started,
                    result=None,
                )
                return None
            # Non-dict return — leave alone (unlikely in these endpoints).
            _log_inbound_gsp_call(
                endpoint=endpoint_path,
                started_at=started,
                result=None,
            )
            return result
        except Exception:
            # Response-shaping / logging failure must not shadow the
            # actual handler result.
            return result
    return wrapper


def _log_inbound_gsp_call(
    *,
    endpoint: str,
    started_at: float,
    result: dict | None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    """gh#147 — write one EasyEcom API Call row (direction=Inbound) for
    every hit on a GSP endpoint (success, 4xx, 5xx, unhandled crash).

    Never raises. Redacts Authorization header. Best-effort — if the
    write itself fails, we log a single warning and move on.
    """
    import hashlib
    import time
    try:
        request = getattr(frappe.local, "request", None)
        if request is None:
            return
        http_status = (
            int((result or {}).get("status") or 200)
            if result is not None
            else (500 if error_class else 200)
        )
        latency_ms = int((time.time() - started_at) * 1000)
        # Redact Authorization (Basic + Bearer both) from the headers
        # snapshot. Everything else is fine to capture.
        redacted_headers = {}
        try:
            for k, v in dict(request.headers).items():
                if k.lower() == "authorization":
                    parts = str(v).split(None, 1)
                    scheme = parts[0] if parts else "Auth"
                    redacted_headers[k] = f"{scheme} <REDACTED>"
                elif k.lower() == "cookie":
                    redacted_headers[k] = "<REDACTED>"
                else:
                    redacted_headers[k] = str(v)
        except Exception:
            redacted_headers = {}
        # Request body — capped at 32 KB for storage sanity.
        try:
            raw_body = request.get_data(as_text=True) or ""
        except Exception:
            raw_body = ""
        req_body_capped = raw_body[:32_000]
        req_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
        # Response body: assemble from frappe.local.response, capped.
        try:
            resp_body = json.dumps(
                {k: v for k, v in dict(frappe.local.response).items() if k != "docs"},
                default=str,
            )
        except Exception:
            resp_body = ""
        resp_body_capped = resp_body[:32_000]
        resp_hash = hashlib.sha256(resp_body.encode("utf-8")).hexdigest()
        # Resolve account: we don't always know it (e.g. /gettoken pre-
        # auth check). Fall back to any enabled account so the required
        # link resolves. Not ideal — but the alternative is skipping the
        # row entirely, which defeats the purpose.
        account_name = None
        # Prefer a bound account when the handler picked one up.
        for attr in ("_gsp_ee_account",):
            v = getattr(frappe.local, attr, None) if hasattr(frappe.local, attr) else None
            if v:
                account_name = v
                break
        if not account_name:
            account_name = frappe.db.get_value(
                "EasyEcom Account", {"enabled": 1}, "name"
            )
        if not account_name:
            # No account on this bench — skip (nothing to link).
            return
        # gh#147 hotfix (2026-07-11 mmpl16): EasyEcom Account has NO
        # `company` field — company lives on EasyEcom Location, not the
        # Account. Trying to read it crashes with "Unknown column
        # 'company'" and kills the entire inbound-log write silently.
        # `EasyEcom API Call.company` is reqd=None, so leave it blank
        # when we can't resolve it cleanly.
        company = None
        # Correlation ID: prefer inbound header if EE sent one (gh#153).
        # Else, generate.
        correlation_id = (
            request.headers.get("X-ECS-Correlation-Id")
            or frappe.generate_hash(length=32)
        )
        status = "Success" if 200 <= http_status < 300 else "Failed"
        doc = frappe.new_doc("EasyEcom API Call")
        doc.update({
            "direction": "Inbound",
            "easyecom_account": account_name,
            "company": company,
            "is_foundational": 1 if endpoint == "/gettoken" else 0,
            "status": status,
            "attempted_at": frappe.utils.now_datetime(),
            "completed_at": frappe.utils.now_datetime(),
            "correlation_id": correlation_id,
            "sub_correlation_id": correlation_id,
            "endpoint": endpoint,
            "http_method": (request.method or "POST"),
            "request_url": str(request.url or endpoint)[:1000],
            "request_headers": json.dumps(redacted_headers, indent=2)[:32_000],
            "request_payload": req_body_capped,
            "request_payload_hash": req_hash,
            "response_status_code": http_status,
            "response_headers": "",
            "response_payload": resp_body_capped,
            "response_payload_hash": resp_hash,
            "latency_ms": latency_ms,
            "attempt_number": 1,
            "error_class": error_class,
            "error_message": (error_message or "")[:8000] if error_message else None,
        })
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        try:
            frappe.log_error(
                title=f"gh#147: inbound API Call log write failed for {endpoint}",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass


def normalise_gsp_auth_header() -> None:
    """gh#123 pre-auth hook: shift `Authorization: ...` to a stash environ
    key for the three GSP endpoints (gettoken / einvoice_update /
    ewaybill_update) so Frappe's validate_auth() doesn't reject the
    request as `AuthenticationError` before our endpoint runs.

    Applies to Basic on /gettoken (the primary bug) AND to Bearer on
    /einvoice/update + /ewaybill/update (same underlying trap — any
    2-part Authorization header + session-still-Guest at the end of
    validate_auth raises).
    """
    try:
        request = getattr(frappe.local, "request", None)
        if request is None:
            return
        path = (getattr(request, "path", "") or "")
        if not any(path.endswith(suffix) for suffix in _GSP_PATH_SUFFIXES):
            return
        environ = request.environ
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if not auth:
            return
        # Stash then remove so validate_auth sees no Authorization header.
        environ.setdefault(_GSP_AUTH_STASH_KEY, auth)
        environ.pop("HTTP_AUTHORIZATION", None)
    except Exception:
        # before_request must NEVER block the request — leave the headers
        # as-is on any unexpected error and let the downstream layers
        # handle whatever comes through.
        return


import contextlib


# gh#166 security hardening — least-privilege user for _elevated_session.
# Created + role-permissioned by patches/v0_1/create_easyecom_integration_user.py
# with narrowly-scoped permissions on the exact DocTypes the inbound
# handler touches (Sales Invoice / DN / Customer Map / etc.). Falls
# back to Administrator on sites where the patch hasn't run yet.
_INTEGRATION_USER_EMAIL = "easyecom-integration@internal.local"


def _enforce_gsp_rate_limit(
    *,
    endpoint: str,
    invoice_id: str,
    ee_account: str,
) -> None:
    """gh#166 rate limit — per (endpoint, invoice_id) per rolling 60s.
    Reads the per-account limit from EasyEcom Account.gsp_rate_limit_per_min
    (default 6 = one call per 10s). Zero / negative / unset = disabled.

    Raises EasyEcomGSPRateLimited on breach. Caller shapes the 429
    response.
    """
    try:
        limit_raw = frappe.db.get_value(
            "EasyEcom Account", ee_account, "gsp_rate_limit_per_min"
        )
    except Exception:
        return  # field not migrated yet
    try:
        limit = int(limit_raw or 6)
    except (TypeError, ValueError):
        limit = 6
    if limit <= 0:
        return  # explicit disable
    key = f"ecs:gsp:ratelimit:{endpoint}:{invoice_id}"
    try:
        # Use Frappe's Redis cache — incr is atomic, TTL is per-key.
        cache = frappe.cache()
        count = cache.get_value(key)
        count = (int(count) if count else 0) + 1
        cache.set_value(key, count, expires_in_sec=60)
    except Exception:
        return  # Redis blip — don't block, just skip enforcement
    if count > limit:
        raise EasyEcomGSPRateLimited(
            f"Rate limit exceeded for {endpoint} on invoice_id "
            f"{invoice_id} — {count} calls in the last 60s "
            f"(limit {limit})."
        )


class EasyEcomGSPRateLimited(Exception):
    """Raised by _enforce_gsp_rate_limit; caller returns HTTP 429."""


def _resolve_elevation_target() -> str:
    """Prefer the dedicated integration user; fall back to Administrator
    only when the user hasn't been created yet (patch not run)."""
    try:
        if frappe.db.exists("User", _INTEGRATION_USER_EMAIL):
            return _INTEGRATION_USER_EMAIL
    except Exception:
        pass
    return "Administrator"


@contextlib.contextmanager
def _elevated_session(user: str | None = None):
    """gh#166 — elevate the Frappe session for the duration of an
    inbound GSP handler.

    Why this exists.

      The three GSP endpoints (gettoken, einvoice_update, ewaybill_update)
      are @whitelist(allow_guest=True) because EasyEcom is NOT a Frappe
      user — they authenticate via our own Bearer token, not a Frappe
      sid/api-key. Frappe's validate_auth() therefore has no user to
      resolve and defaults session.user='Guest'.

      When we then call `si.insert()`, our own `ignore_permissions=True`
      flag skips Frappe's insert-time permission check on the SI —
      BUT the validate() hook then fires, and validate hooks in other
      installed apps (e.g. modernmarwar's `set_total_overdue_amount`
      calling `frappe.get_list("Sales Invoice", ...)`) enforce SELECT
      permission via the current session user. Guest has none → hard
      PermissionError.

      No amount of `ignore_permissions` on our end propagates into
      third-party validate hooks. The only durable answer is to
      temporarily run the whole SI insert/submit/mint chain under a
      user that DOES have those permissions.

    Semantics.

      - Bearer auth has already succeeded upstream (validate_bearer
        returned a real EasyEcom Account) — this is not a security
        elevation from an unauthenticated caller; it's a role-swap
        from Guest to Administrator AFTER trust is established.
      - The elevation is scoped to a single request; the finally
        clause restores the original user on ANY exit path.
      - No-op when the current session is already non-Guest (leaves
        real Frappe user sessions untouched — useful for smoke tests
        that run this handler as an API-key user).
      - Uses the least-privilege 'EasyEcom Integration' user when
        that user exists on this site (created by the
        create_easyecom_integration_user patch); falls back to
        Administrator only when the patch hasn't run yet.
    """
    original_user = frappe.session.user if frappe.session else "Guest"
    should_elevate = original_user == "Guest"
    target_user = user or _resolve_elevation_target()
    if should_elevate:
        frappe.set_user(target_user)
    try:
        yield
    finally:
        if should_elevate:
            try:
                frappe.set_user(original_user)
            except Exception:
                # If restoration fails, the request is about to end
                # anyway; the next request creates a fresh session.
                pass


def _get_gsp_auth_header() -> str | None:
    """Read the Authorization value for a GSP endpoint, preferring the
    stash environ key that `normalise_gsp_auth_header` populates. Falls
    back to the standard Authorization header so tests or environments
    where the pre-request hook doesn't fire still work.
    """
    try:
        request = getattr(frappe.local, "request", None)
        if request is not None:
            stashed = request.environ.get(_GSP_AUTH_STASH_KEY)
            if stashed:
                return stashed
    except Exception:
        pass
    return frappe.get_request_header("Authorization")


def _log_inbound_gsp_failure(
    *,
    endpoint: str,
    ee_row: dict[str, Any] | None,
    ee_account: str | None,
    reason: str,
    http_status: int,
) -> None:
    """gh#142: on any inbound GSP-callback failure, emit both an Error
    Log (traceback + payload snapshot) AND an EasyEcom Sync Record
    (direction=Inbound API, status=Failed) so the failure is auditable
    when EE only sees a status code.

    Never raises — best-effort observability layer, must not shadow the
    actual failure being reported to EE.
    """
    ee_row = ee_row or {}
    ref = str(ee_row.get("reference_code") or ee_row.get("order_id") or "") or None
    inv_id = str(ee_row.get("invoice_id") or "") or None
    # 1. Error Log — always.
    try:
        frappe.log_error(
            title=(
                f"§11.5.1 {endpoint} failed "
                f"(HTTP {http_status}) ref={ref or '?'} inv={inv_id or '?'}"
            ),
            message=(
                f"reason: {reason}\n"
                f"account: {ee_account or '?'}\n"
                f"ee_row: {json.dumps(ee_row, default=str, indent=2)[:8000]}\n"
                f"traceback:\n{frappe.get_traceback()}"
            ),
        )
    except Exception:
        pass
    # 2. Inbound Sync Record — best-effort. Falls back to a bare log if
    # the Sync Record write itself blows up (schema drift, etc.).
    #
    # gh#143 landed with missing mandatory fields (verified live 2026-07-10:
    # 5 /einvoice/update failures logged to Error Log but zero SR rows
    # written — silent mandatory-field rejection). Sync Record's schema
    # requires: company, entity_doctype, entity_name (Dynamic Link on
    # entity_doctype), entity_type, direction, status, correlation_id,
    # idempotency_key, attempts. The entity_name field is a Dynamic Link
    # so Frappe validates the (entity_doctype, entity_name) pair references
    # a REAL record — we can't stuff arbitrary strings.
    #
    # Strategy: use Sales Order + reference_code as the entity when the SO
    # exists on our side (the common case — EE only calls /einvoice/update
    # after we successfully pushed the SO). If the SO can't be resolved
    # (rare: EE is calling for an unknown reference), skip the SR write
    # cleanly — Error Log above already carries the full detail.
    try:
        entity_doctype = "Sales Order"
        entity_name = ref if ref and frappe.db.exists(entity_doctype, ref) else None
        if not entity_name:
            # Fall back gracefully — no SR row, Error Log has it.
            return
        company = None
        if ee_account:
            company = frappe.db.get_value(
                "EasyEcom Account", ee_account, "company"
            )
        if not company:
            company = frappe.db.get_value(
                "Sales Order", entity_name, "company"
            )
        if not company:
            company = frappe.db.get_value("Company", {}, "name")
        # Idempotency key: identify THIS particular inbound attempt so
        # the SR row for a repeat re-fire of the SAME payload is an
        # upsert (same key) rather than a new row every time.
        inv_id_str = str(ee_row.get("invoice_id") or "") or "no-invoice-id"
        idem = f"gsp{endpoint}:{inv_id_str}"
        sr = frappe.new_doc("EasyEcom Sync Record")
        sr.update({
            "company": company or "",
            "entity_type": "Sales Order",
            "entity_doctype": entity_doctype,
            "entity_name": entity_name,
            "direction": "Inbound API",
            "status": "Failed",
            "correlation_id": frappe.generate_hash(length=32),
            "idempotency_key": idem,
            "attempts": 1,
            "easyecom_account": ee_account or "",
            "last_error": f"[{endpoint} HTTP {http_status}] {reason}",
        })
        sr.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        try:
            frappe.log_error(
                title=f"§11.5.1 {endpoint}: failed to write inbound Sync Record",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass


# ============================================================
# /gettoken — Basic auth → Bearer mint
# ============================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_gsp_endpoint
def gettoken() -> dict[str, Any]:
    """EE's /gettoken contract:

    Headers:
        Authorization: Basic <base64-encoded user:secret>

    HTTP response body (FLAT — @_gsp_endpoint transforms the returned
    dict into a top-level response, no {"message": {...}} wrapper):
        {"status": 200, "access_token": "<token>",
         "token_type": "Bearer", "expires_in": 3600}

    On auth failure:
        {"status": 401, "message": "<reason>"}
    """
    try:
        account_name = validate_basic_auth(
            _get_gsp_auth_header()
        )
    except EasyEcomGSPAuthError as exc:
        return {"status": 401, "message": str(exc)}

    try:
        minted = issue_bearer(
            account_name,
            issued_to_ip=frappe.local.request_ip if frappe.local else None,
        )
    except Exception as exc:
        frappe.log_error(
            title=f"§11.5.1 /gettoken: token mint failed for {account_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"status": 500, "message": "Token mint failed."}

    return {
        "status": 200,
        "access_token": minted["token"],
        "token_type": "Bearer",
        "expires_in": minted["expires_in"],
    }


# ============================================================
# /einvoice/update — Bearer + EE order JSON → IRN
# ============================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_gsp_endpoint
def einvoice_update() -> dict[str, Any]:
    """EE's /einvoice/update contract:

    Headers:
        Authorization: Bearer <token from /gettoken>
        Content-Type: application/json

    Body:
        {"orders": [<EE order object — same shape as getOrderDetails>]}

    Returns on success:
        {"status": 200, "message": "Invoice fetched successfully",
         "data": {"invoice_details": {
             "invoice_id": "<EE's invoice_id>",
             "erp_invoice_num": "<our SI docname>",
             "irn": "<64-char IRN from NIC IRP>",
             "ack_number": "<NIC ack number>",
             "ack_date": "<ISO timestamp>",
             "invoice_pdf": "<URL>",
             "irn_qr": "<base64 or text>",
             "invoice_base64": "<base64 PDF or empty>"
         }}}

    On auth failure:        HTTP 401
    On payload errors:      HTTP 422
    On NIC mint failure:    HTTP 422 with NIC error in body
    """
    try:
        ee_account = validate_bearer(
            _get_gsp_auth_header()
        )
    except EasyEcomGSPAuthError as exc:
        frappe.response.http_status_code = 401
        return {"status": 401, "message": str(exc)}

    try:
        body = frappe.request.get_json() or {}
    except Exception as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": f"Invalid JSON body: {exc}"}

    # gh#142: EE's live /einvoice/update call sends `orders` as a
    # SINGLE OBJECT (not an array). Contract doc §3 shows an array; live
    # sample from Garv 2026-07-10 sends an object with the same fields
    # flat. Same defensive both-shapes pattern as /ewaybill/update.
    orders = body.get("orders")
    if isinstance(orders, list):
        if not orders:
            frappe.response.http_status_code = 422
            return {
                "status": 422,
                "message": "Body 'orders' array is empty.",
            }
        ee_row = orders[0]
    elif isinstance(orders, dict):
        ee_row = orders
    else:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": (
                "Body must have 'orders' as a single object or a "
                f"non-empty array; got {type(orders).__name__}."
            ),
        }

    # gh#166 rate limit — cheap check BEFORE we do any SI work.
    invoice_id_str = str(ee_row.get("invoice_id") or "").strip() or "no-id"
    try:
        _enforce_gsp_rate_limit(
            endpoint="/einvoice/update",
            invoice_id=invoice_id_str,
            ee_account=ee_account,
        )
    except EasyEcomGSPRateLimited as exc:
        frappe.response.http_status_code = 429
        return {"status": 429, "message": str(exc)}

    return _einvoice_handler(ee_row=ee_row, ee_account=ee_account)


# ============================================================
# /ewaybill/update — Bearer + EE order JSON → eway
# ============================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_gsp_endpoint
def ewaybill_update() -> dict[str, Any]:
    """EE's /ewaybill/update contract:

    Headers:
        Authorization: Bearer <token from /gettoken>
        Content-Type: application/json

    Body:
        {"orders": {<single EE order object with transport_* fields>}}

    Note: unlike /einvoice/update, EE sends `orders` as a SINGLE OBJECT
    (not an array). The shape diff is per EE's contract — we accept
    both shapes defensively.

    Returns on success:
        {"status": 200, "message": "E-Way Bill fetched successfully",
         "data": {"invoice_details": {
             "invoice_id": "<EE's invoice_id>",
             "erp_invoice_num": "<our SI docname>",
             "eway_bill_number": "<12-digit>",
             "eway_bill_date": "<ISO>",
             "eway_bill_pdf": "<URL>",
             "transport_mode": "Road",
             "vehicle_number": "...",
             "vehicle_type": "...",
             "transporter_gst": "...",
             "transporter_name": "...",
             "eway_bill_base64": "<base64 PDF or empty>"
         }}}
    """
    try:
        ee_account = validate_bearer(
            _get_gsp_auth_header()
        )
    except EasyEcomGSPAuthError as exc:
        frappe.response.http_status_code = 401
        return {"status": 401, "message": str(exc)}

    try:
        body = frappe.request.get_json() or {}
    except Exception as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": f"Invalid JSON body: {exc}"}

    orders = body.get("orders")
    if isinstance(orders, list) and orders:
        ee_row = orders[0]
    elif isinstance(orders, dict):
        ee_row = orders
    else:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": "Body must have orders as dict or non-empty array.",
        }

    # gh#166 rate limit — same treatment as /einvoice/update.
    invoice_id_str = str(ee_row.get("invoice_id") or "").strip() or "no-id"
    try:
        _enforce_gsp_rate_limit(
            endpoint="/ewaybill/update",
            invoice_id=invoice_id_str,
            ee_account=ee_account,
        )
    except EasyEcomGSPRateLimited as exc:
        frappe.response.http_status_code = 429
        return {"status": 429, "message": str(exc)}

    return _ewaybill_handler(ee_row=ee_row, ee_account=ee_account)


# ============================================================
# Handler internals
# ============================================================


def _einvoice_handler(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Find/create SI, submit + mint IRN, return EE-shape response.

    Delegates to _einvoice_handler_impl wrapped in _elevated_session
    (gh#166) so third-party validate hooks (modernmarwar's
    set_total_overdue_amount, IC, etc.) that call frappe.get_list
    survive the Guest session that allow_guest=True imposes.
    """
    with _elevated_session():
        return _einvoice_handler_impl(ee_row=ee_row, ee_account=ee_account)


def _einvoice_handler_impl(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Actual find/create/submit/mint logic. Runs under elevated session."""
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_or_create_si_for_gsp,
        GSPHandlerError,
        mint_irn_for_si,
    )
    ref = str(ee_row.get("reference_code") or "") or None

    try:
        si_name = find_or_create_si_for_gsp(
            ee_row=ee_row, ee_account=ee_account,
        )
    except GSPHandlerError as exc:
        reason = str(exc)
        _log_inbound_gsp_failure(
            endpoint="/einvoice/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=422,
        )
        frappe.response.http_status_code = 422
        return {"status": 422, "message": reason, "reference_code": ref}
    except Exception as exc:
        # gh#142: Frappe validation throws (India Compliance,
        # place-of-supply, missing template) come through here — surface
        # the real message instead of swallowing behind a bare 422.
        reason = f"{type(exc).__name__}: {exc}"
        _log_inbound_gsp_failure(
            endpoint="/einvoice/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=422,
        )
        frappe.response.http_status_code = 422
        return {"status": 422, "message": reason, "reference_code": ref}

    try:
        irn_data = mint_irn_for_si(si_name, ee_account=ee_account)
    except GSPHandlerError as exc:
        reason = str(exc)
        _log_inbound_gsp_failure(
            endpoint="/einvoice/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=422,
        )
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": reason,
            "reference_code": ref,
            "data": {"invoice_details": {
                "invoice_id": str(ee_row.get("invoice_id") or ""),
                "erp_invoice_num": si_name,
            }},
        }
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _log_inbound_gsp_failure(
            endpoint="/einvoice/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=502,
        )
        frappe.response.http_status_code = 502
        return {
            "status": 502,
            "message": f"NIC IRP mint failed: {reason}",
            "reference_code": ref,
        }

    return {
        "status": 200,
        "message": "Invoice fetched successfully",
        "data": {"invoice_details": irn_data},
    }


def _ewaybill_handler(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Find SI + mint eway. Delegates to _impl under _elevated_session
    (gh#166) — same rationale as _einvoice_handler."""
    with _elevated_session():
        return _ewaybill_handler_impl(ee_row=ee_row, ee_account=ee_account)


def _ewaybill_handler_impl(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Actual find + eway-mint logic. Runs under elevated session."""
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_si_by_invoice_id,
        GSPHandlerError,
        mint_eway_for_si,
    )

    ee_invoice_id = str(ee_row.get("invoice_id") or "").strip()
    ref = str(ee_row.get("reference_code") or "") or None
    if not ee_invoice_id:
        _log_inbound_gsp_failure(
            endpoint="/ewaybill/update", ee_row=ee_row,
            ee_account=ee_account, reason="Body missing invoice_id.",
            http_status=422,
        )
        frappe.response.http_status_code = 422
        return {
            "status": 422, "message": "Body missing invoice_id.",
            "reference_code": ref,
        }

    try:
        si_name = find_si_by_invoice_id(ee_invoice_id)
    except GSPHandlerError as exc:
        reason = str(exc)
        _log_inbound_gsp_failure(
            endpoint="/ewaybill/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=409,
        )
        frappe.response.http_status_code = 409
        return {"status": 409, "message": reason, "reference_code": ref}

    transport_values = {
        "transporter_gst_no": ee_row.get("transporter_gst"),
        "transporter_name": ee_row.get("transporter_name"),
        "vehicle_no": ee_row.get("vehicle_number"),
        "vehicle_type": ee_row.get("vehicle_type"),
        "mode_of_transport": _resolve_transport_mode(ee_row),
        "lr_no": ee_row.get("transport_document_number"),
    }

    try:
        eway_data = mint_eway_for_si(
            si_name,
            transport_values=transport_values,
            ee_account=ee_account,
        )
    except GSPHandlerError as exc:
        reason = str(exc)
        _log_inbound_gsp_failure(
            endpoint="/ewaybill/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=422,
        )
        frappe.response.http_status_code = 422
        return {"status": 422, "message": reason, "reference_code": ref}
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _log_inbound_gsp_failure(
            endpoint="/ewaybill/update", ee_row=ee_row,
            ee_account=ee_account, reason=reason, http_status=502,
        )
        frappe.response.http_status_code = 502
        return {
            "status": 502,
            "message": f"NIC EWB mint failed: {reason}",
            "reference_code": ref,
        }

    return {
        "status": 200,
        "message": "E-Way Bill fetched successfully",
        "data": {"invoice_details": eway_data},
    }


def _resolve_transport_mode(ee_row: dict) -> str:
    """EE sends transport_mode as string ("1"=Road, "2"=Rail, "3"=Air,
    "4"=Ship) per their contract. India Compliance expects the named
    enum. Map per NIC EWB convention."""
    raw = str(ee_row.get("transport_mode") or "1").strip()
    return {
        "1": "Road", "2": "Rail", "3": "Air", "4": "Ship",
        "Road": "Road", "Rail": "Rail", "Air": "Air", "Ship": "Ship",
    }.get(raw, "Road")
