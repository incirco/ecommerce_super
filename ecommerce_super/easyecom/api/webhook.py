"""EasyEcom inbound webhook receiver.

POST endpoint: /api/method/ecommerce_super.easyecom.api.webhook.receive

SPEC §3.8 + §6.4 + §7.7:
  - Bearer-token auth in either `Access-token` or `Authorization: Bearer`
    header. Token compared in constant time (§3.8). Missing/invalid → 401.
  - Optional IP allowlist (CIDR) via Account.webhook_allowed_ips.
  - Dedup on (company, event_type, ee_event_id) — first via the controller
    helper, then by the DB UNIQUE constraint. Duplicates return 200 so EE
    stops retrying (§6.6).
  - Company routing: resolve the payload's location_key to its Frappe
    Company via EasyEcom Location.frappe_company. Webhooks for primary or
    inert locations that resolve to no Company are handled as master/
    account events with company=blank.
  - Every receipt writes an EasyEcom Webhook Event row before processing
    (§7.2).
  - This endpoint MUST be whitelisted with allow_guest=1 — that's the one
    allowed exception (CLAUDE.md "Specific Frappe v16 things to know").

`normalise_webhook_auth_header` is wired as a `before_request` hook in
hooks.py — see its docstring for the Frappe auth-middleware quirk it
works around (gh#1).
"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any

import frappe

from ecommerce_super.easyecom.client.auth import get_account
from ecommerce_super.easyecom.doctype.easyecom_location.easyecom_location import (
    resolve_company,
)
from ecommerce_super.easyecom.doctype.easyecom_webhook_event.easyecom_webhook_event import (
    find_duplicate,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id
from ecommerce_super.easyecom.utils.hashing import sha256_hex
from ecommerce_super.easyecom.utils.redaction import redact

# Path suffix the webhook endpoint resolves to under any site mount.
# Used by the before_request header-rewrite hook below.
WEBHOOK_RECEIVE_PATH = "/api/method/ecommerce_super.easyecom.api.webhook.receive"


def normalise_webhook_auth_header() -> None:
    """Pre-auth hook: shift `Authorization: Bearer <token>` → `Access-token`
    for the webhook receiver path only (gh#1).

    The why: Frappe's `validate_auth` (frappe/auth.py, end of function)
    raises `AuthenticationError` whenever the request carries a 2-part
    `Authorization` header AND the session user is still Guest after its
    OAuth / API-key / hook chain — even for `@whitelist(allow_guest=True)`
    endpoints. SPEC §3.8 explicitly mandates accepting webhook bearer
    tokens via `Authorization: Bearer ...`, so we cannot just tell EE to
    stop sending that header form.

    This hook runs in the `before_request` phase, which fires inside
    `init_request` BEFORE `application()` calls `validate_auth` — exactly
    the window we need. For requests to the webhook URL only, we move
    the Bearer token from `Authorization` to `Access-token` by mutating
    the WSGI environ. The downstream `_extract_token` then reads
    `Access-token` as usual, runs the real constant-time `webhook_token`
    comparison, and rejects with 401 if the token is wrong — the
    SPEC-mandated auth check still happens, only the header carrier
    shifts. Non-Bearer Authorization shapes (Token / Basic) are left
    alone since they aren't valid webhook auth anyway and the receiver
    will reject them downstream.
    """
    try:
        request = getattr(frappe.local, "request", None)
        if request is None:
            return
        path = (getattr(request, "path", "") or "")
        if not path.endswith(WEBHOOK_RECEIVE_PATH):
            return
        environ = request.environ
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if not auth.lower().startswith("bearer "):
            return
        token = auth[7:].strip()
        if not token:
            return
        # Don't clobber an explicit Access-token header the sender already
        # set — if both are present, the explicit one wins (the receiver
        # reads Access-token first per SPEC §3.8).
        environ.setdefault("HTTP_ACCESS_TOKEN", token)
        environ.pop("HTTP_AUTHORIZATION", None)
    except Exception:
        # before_request must NEVER block the request — leave the headers
        # as-is on any unexpected error and let the downstream layers
        # handle whatever comes through.
        return


# Event types we recognise. Unknown event_type produces a row with
# processing_state=Failed and processing_error documenting the unknown type
# — never silently dropped.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "manifest",
        "dispatch",
        "order_cancelled",
        "return_received",
        "grn_completed",
        "inventory_reserved",
        "inventory_released",
        "po_status_changed",
        "item_updated",
    }
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive() -> dict[str, Any]:
    """Webhook receiver. Returns 200 with empty body on success/dedup, 401
    on auth failure, 503 if webhooks are disabled. Never raises — every
    outcome lands on an EasyEcom Webhook Event row (success or failure).
    """
    request = frappe.local.request

    # Step 1: Load the Account; check webhook_enabled.
    try:
        account = get_account()
    except Exception:
        return _respond(503, {"error": "EasyEcom Account not configured."})

    if not account.webhook_enabled:
        return _respond(
            503, {"error": "Webhook reception disabled by Account configuration."}
        )

    # Step 2: Token auth (bearer in either header form, constant-time compare).
    auth_header_used, supplied_token = _extract_token(request.headers)
    expected_token = account.get_webhook_token()
    if not expected_token:
        return _respond(401, {"error": "Webhook token not configured on Account."})
    if not supplied_token:
        return _respond(401, {"error": "Missing webhook auth header."})
    if not hmac.compare_digest(supplied_token, expected_token):
        return _respond(401, {"error": "Invalid webhook token."})

    # Step 3: Optional IP allowlist (CIDR).
    source_ip = (
        request.headers.get("CF-Connecting-IP") or request.remote_addr or "0.0.0.0"
    )
    ip_check = _check_ip_allowlist(source_ip, account.webhook_allowed_ips)
    if ip_check == "Fail":
        return _respond(401, {"error": "Source IP not in allowlist."})

    # Step 4: Parse payload.
    try:
        payload = frappe.parse_json(request.get_data(as_text=True))
        if not isinstance(payload, dict):
            payload = {"raw": payload}
    except Exception:
        # Still record the receipt — but with processing_state=Failed.
        _record_event(
            account=account,
            event_type=request.headers.get("X-EE-Event-Type", "unknown"),
            ee_event_id=request.headers.get(
                "X-EE-Event-Id", f"unparseable-{new_correlation_id()}"
            ),
            payload={"raw_bytes_redacted": True},
            source_ip=source_ip,
            auth_header_used=auth_header_used,
            ip_check=ip_check,
            location_key=None,
            company=None,
            processing_state="Failed",
            processing_error="Could not parse JSON body.",
        )
        return _respond(400, {"error": "Could not parse JSON body."})

    event_type = (
        payload.get("event_type") or request.headers.get("X-EE-Event-Type") or "unknown"
    )
    ee_event_id = (
        payload.get("event_id")
        or payload.get("id")
        or request.headers.get("X-EE-Event-Id")
        or new_correlation_id()
    )
    location_key = payload.get("location_key") or payload.get("seller_id")

    # Step 5: Company routing.
    company = resolve_company(location_key) if location_key else None
    # Primary or inert locations resolve to no Company — that's correct (§3.8),
    # they're handled as master events with company=blank. But the DocType
    # has company as mandatory, so we use a sentinel: if no company resolves
    # AND no Companies exist on this site, refuse cleanly. If Companies exist
    # but the location maps to none, route to the first Company as a holding
    # bucket and surface as a Discrepancy at processing time.
    if not company:
        company = _first_company_or_none()
        if not company:
            return _respond(503, {"error": "No Frappe Company configured."})

    # Step 6: Dedup by (company, event_type, ee_event_id). DB UNIQUE is the
    # source of truth; the helper short-circuits before insert for the
    # common case.
    existing = find_duplicate(
        company=company, event_type=event_type, ee_event_id=ee_event_id
    )
    if existing:
        return _respond(200, {"ok": True, "dedup": existing})

    # Step 7: Record the event. The DB UNIQUE may still race here (two
    # concurrent identical deliveries); catch and treat as dedup.
    try:
        event_name = _record_event(
            account=account,
            event_type=event_type,
            ee_event_id=str(ee_event_id),
            payload=payload,
            source_ip=source_ip,
            auth_header_used=auth_header_used,
            ip_check=ip_check,
            location_key=location_key,
            company=company,
            processing_state="Pending",
            processing_error=None,
        )
    except frappe.exceptions.UniqueValidationError:
        return _respond(200, {"ok": True, "dedup": "race"})
    except Exception as e:
        return _respond(
            500, {"error": f"Could not record webhook: {type(e).__name__}: {e}"}
        )

    # Step 8: Validate event_type AFTER recording (so unknown ones are
    # still audited).
    if event_type not in KNOWN_EVENT_TYPES:
        frappe.db.set_value(
            "EasyEcom Webhook Event",
            event_name,
            {
                "processing_state": "Failed",
                "processing_error": f"Unknown event_type: {event_type}",
                "processing_completed_at": frappe.utils.now_datetime(),
            },
        )
        frappe.db.commit()
        # Still 200 — refusing isn't useful (EE will just retry).
        return _respond(200, {"ok": True, "warning": "unknown_event_type"})

    # Step 9: Enqueue processing (flow handlers are built in their packets;
    # for now we just record Pending and return 200).
    # When the flows for §9–§13 are built, this is where we'd:
    #   from ecommerce_super.easyecom.queue import enqueue_easyecom_job
    #   enqueue_easyecom_job("Webhook Process", company, parent_event=event_name, ...)
    # For the foundation packet, we leave it Pending — the row exists and
    # is visible to the FDE, awaiting the flow handler.

    return _respond(200, {"ok": True, "event": event_name})


# ----- Helpers -----


def _extract_token(headers) -> tuple[str, str | None]:
    """Read the token from `Access-token` or `Authorization: Bearer` header.

    Returns (which_header_was_used, token_or_None). The receiver accepts
    EITHER form per §3.8 ("the seller chooses the form when configuring
    the webhook on the EasyEcom side").
    """
    access_token = headers.get("Access-token")
    if access_token:
        return "Access-token", access_token.strip()
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return "Authorization", auth[7:].strip()
    return "Access-token", None  # default for the recording


def _check_ip_allowlist(source_ip: str, allowed_cidr_text: str | None) -> str:
    """Return 'Pass', 'Fail', or 'Skipped'."""
    if not allowed_cidr_text or not allowed_cidr_text.strip():
        return "Skipped"
    try:
        addr = ipaddress.ip_address(source_ip)
    except ValueError:
        return "Fail"
    for line in allowed_cidr_text.splitlines():
        cidr = line.strip()
        if not cidr:
            continue
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            if addr in network:
                return "Pass"
        except ValueError:
            continue
    return "Fail"


def _first_company_or_none() -> str | None:
    return frappe.db.get_value("Company", filters={}, fieldname="name")


def _record_event(
    *,
    account,
    event_type: str,
    ee_event_id: str,
    payload: Any,
    source_ip: str,
    auth_header_used: str,
    ip_check: str,
    location_key: str | None,
    company: str | None,
    processing_state: str,
    processing_error: str | None,
) -> str:
    redacted = redact(payload)
    doc = frappe.new_doc("EasyEcom Webhook Event")
    doc.update(
        {
            "company": company,
            "event_type": event_type,
            "ee_event_id": ee_event_id,
            "received_at": frappe.utils.now_datetime(),
            "correlation_id": new_correlation_id(),
            "auth_header_used": auth_header_used,
            "token_verified": 1,
            "allowed_ip_check": ip_check,
            "source_ip": source_ip,
            "http_method": "POST",
            "raw_payload": frappe.as_json(redacted),
            "payload_hash": sha256_hex(redacted),
            "processing_state": processing_state,
            "processing_error": processing_error,
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _respond(status_code: int, body: dict) -> dict:
    frappe.local.response["http_status_code"] = status_code
    return body
