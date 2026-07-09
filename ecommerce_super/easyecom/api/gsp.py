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
    for key, value in payload.items():
        frappe.local.response[key] = value
    frappe.local.response.pop("message", None)


def _gsp_endpoint(fn):
    """gh#130 endpoint decorator: transforms a `return {"status": ...}`
    dict from a GSP handler into a FLAT `frappe.local.response` write
    and returns None to prevent Frappe's handler layer from re-wrapping
    it in `{"message": {...}}`.

    Handlers keep the ergonomic `return {...}` shape (easy to read,
    easy to unit-test — the dict is the sole source of truth for the
    response). The decorator does the transport-layer wiring.

    The `status` key in the returned dict is used as the HTTP status
    code (defaults to 200 if absent — but every handler in this
    module always sets `status` explicitly).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if result is None:
            # Handler already wrote to frappe.local.response directly.
            return None
        if isinstance(result, dict):
            http_status = int(result.get("status") or 200)
            _write_gsp_flat_response(result, http_status=http_status)
            return None
        # Non-dict return — leave alone (unlikely in these endpoints).
        return result
    return wrapper


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

    orders = body.get("orders") or []
    if not isinstance(orders, list) or not orders:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": "Body must have orders[] array with at least one row.",
        }

    ee_row = orders[0]
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

    return _ewaybill_handler(ee_row=ee_row, ee_account=ee_account)


# ============================================================
# Handler internals
# ============================================================


def _einvoice_handler(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Find/create SI, submit + mint IRN, return EE-shape response."""
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_or_create_si_for_gsp,
        GSPHandlerError,
        mint_irn_for_si,
    )

    try:
        si_name = find_or_create_si_for_gsp(
            ee_row=ee_row, ee_account=ee_account,
        )
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": str(exc)}

    try:
        irn_data = mint_irn_for_si(si_name, ee_account=ee_account)
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": str(exc),
            "data": {"invoice_details": {
                "invoice_id": str(ee_row.get("invoice_id") or ""),
                "erp_invoice_num": si_name,
            }},
        }
    except Exception as exc:
        frappe.log_error(
            title=f"§11.5.1 /einvoice mint failed for {si_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        frappe.response.http_status_code = 502
        return {
            "status": 502,
            "message": f"NIC IRP mint failed: {type(exc).__name__}",
        }

    return {
        "status": 200,
        "message": "Invoice fetched successfully",
        "data": {"invoice_details": irn_data},
    }


def _ewaybill_handler(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Find SI (must already exist from einvoice call), mint eway."""
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_si_by_invoice_id,
        GSPHandlerError,
        mint_eway_for_si,
    )

    ee_invoice_id = str(ee_row.get("invoice_id") or "").strip()
    if not ee_invoice_id:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": "Body missing invoice_id."}

    try:
        si_name = find_si_by_invoice_id(ee_invoice_id)
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 409
        return {"status": 409, "message": str(exc)}

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
        frappe.response.http_status_code = 422
        return {"status": 422, "message": str(exc)}
    except Exception as exc:
        frappe.log_error(
            title=f"§11.5.1 /ewaybill mint failed for {si_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        frappe.response.http_status_code = 502
        return {
            "status": 502,
            "message": f"NIC EWB mint failed: {type(exc).__name__}",
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
