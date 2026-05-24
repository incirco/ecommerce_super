"""Whitelisted Test Connection endpoint for the EasyEcom Account form.

Called from easyecom_account.js when the Test Connection button is clicked.
Acquires (or refreshes) the JWT for the Account's default_location_key via
EasyEcomClient.refresh_jwt() and reports inline.

SPEC §3.11 bar 1 / §3.3.2 ("test_connection_action Button — triggers a
/access/token call and reports result inline. Available before save").

The acceptance bar: "With valid credentials, a Test Connection action
acquires a JWT for the primary location via POST /access/token and reports
success inline. With invalid credentials it reports a clear failure, not
a stack trace."

Returns a plain dict so the client-side handler can decide presentation;
never raises through the whitelist (every error path returns ok=False).
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomAuthError,
    EasyEcomError,
    EasyEcomRateLimitError,
)


@frappe.whitelist()
def test_connection(account: str) -> dict[str, Any]:
    """Acquire a fresh JWT for the Account's default location and report.

    Returns:
      {
        "ok": True|False,
        "message": "...",                # short, FDE-readable summary
        "location_key": "...",           # the location the test ran against
        "error_code": "ECS_API_AUTH_ERROR" # only on failure
      }

    Permission: caller needs read on EasyEcom Account (any role) AND the
    account record must exist. We don't decrypt credentials here directly —
    EasyEcomClient does, transiently, inside refresh_jwt().
    """
    # Permission check — relies on the standard whitelist+DocPerm path. We
    # additionally guard so the response doesn't leak existence of an
    # account the caller cannot read.
    if not frappe.has_permission("EasyEcom Account", "read", doc=account):
        frappe.throw(
            frappe._("Not permitted to read EasyEcom Account {0}.").format(account),
            frappe.PermissionError,
        )

    if not frappe.db.exists("EasyEcom Account", account):
        return {"ok": False, "message": f"Account {account} not found."}

    acc = frappe.get_doc("EasyEcom Account", account)
    if not acc.default_location_key:
        return {
            "ok": False,
            "message": "No Default Location is set on this Account. "
            "Set default_location_key (typically the primary location) and retry.",
        }

    # default_location_key is a Link to EasyEcom Location (docname); resolve
    # to the actual location_key string the client uses.
    location_key = frappe.db.get_value(
        "EasyEcom Location", acc.default_location_key, "location_key"
    )
    if not location_key:
        return {
            "ok": False,
            "message": f"Cannot resolve default_location_key ({acc.default_location_key}) "
            "to a location_key. The Location may have been deleted.",
        }

    # Late import: avoid pulling the HTTP client into module-load paths that
    # don't need it (e.g. test discovery).
    from ecommerce_super.easyecom.client.client import EasyEcomClient

    try:
        client = EasyEcomClient(location_key=location_key)
        # refresh_jwt() always hits /access/token (vs get_jwt which uses cache).
        # That's what "Test Connection" should do — exercise the auth path fresh.
        jwt = client.refresh_jwt()
        return {
            "ok": True,
            "message": f"Connected. JWT acquired for location {location_key}.",
            "location_key": location_key,
            "jwt_acquired": bool(jwt),
        }
    except EasyEcomRateLimitError as e:
        # 60s lockout between token calls (§31.3.1). Distinct from auth
        # failure — credentials are likely fine; the FDE just clicked
        # Test Connection too soon after the last attempt.
        return {
            "ok": False,
            "message": (
                "Test Connection rate-limited. EasyEcom permits one /access/token "
                "call per location per 60 seconds — wait a moment and try again. "
                "Credentials are not the problem (the lockout fires before any "
                "credential check)."
            ),
            "location_key": location_key,
            "error_code": e.error_code,
            "retry_after": e.retry_after,
        }
    except EasyEcomAuthError as e:
        return {
            "ok": False,
            "message": "Authentication failed. Check the api_key, email, and password.",
            "location_key": location_key,
            "error_code": e.error_code,
            "detail": str(e),
        }
    except EasyEcomAPIError as e:
        return {
            "ok": False,
            "message": f"EasyEcom returned an error: {e}",
            "location_key": location_key,
            "error_code": e.error_code,
            "status_code": e.status_code,
        }
    except EasyEcomError as e:
        return {
            "ok": False,
            "message": str(e),
            "location_key": location_key,
            "error_code": e.error_code,
        }
    except Exception as e:
        # Any other unexpected error — return a clean message rather than
        # surfacing a stack trace through the whitelist (§3.11 bar 1).
        frappe.log_error(
            title="EasyEcom Test Connection unexpected error",
            message=f"{type(e).__name__}: {e}",
        )
        return {
            "ok": False,
            "message": f"Unexpected error: {type(e).__name__}. See Error Log for details.",
            "location_key": location_key,
        }
