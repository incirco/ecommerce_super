"""§11.5.1 Mode 1 — Authentication helpers for the Custom GSP endpoints.

EE calls our /gettoken with HTTP Basic auth (the same shared secret
they configured on their EE Account's Custom GSP setup). We:
  1. Decode the Basic header
  2. Match the password portion against any enabled EE Account's
     gsp_basic_auth_secret (Password field, decrypted)
  3. Mint a Bearer token (1hr TTL), store only the SHA-256 hash
  4. Return the plaintext token ONCE in the response

For subsequent /einvoice/update and /ewaybill/update calls, EE sends
the Bearer back. We:
  1. Hash the incoming Bearer
  2. Lookup tabEasyEcom GSP Token by hash
  3. Check expires_at > now()
  4. Update last_used_at
  5. Return the matched EasyEcom Account name (the caller-injected
     scope for downstream SI find/create + IRN mint)

Failures raise EasyEcomGSPAuthError — caller endpoints catch + return
HTTP 401 with an EE-friendly body.

Token cleanup: a daily scheduler tick removes tokens past
(expires_at + 7 days) so the table doesn't grow unbounded. Active /
recently-expired tokens stay queryable for audit.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.password import get_decrypted_password


# Token TTL per EE's contract.
TOKEN_TTL_SECONDS: int = 3600  # 1 hour

# Keep expired tokens this long for audit before cleanup.
EXPIRED_RETENTION_DAYS: int = 7


class EasyEcomGSPAuthError(Exception):
    """Raised when Basic or Bearer auth fails. Caller endpoints
    translate this to HTTP 401 with a clean error body."""


# ============================================================
# Basic auth — used by /gettoken
# ============================================================


def validate_basic_auth(auth_header: str | None) -> str:
    """Decode HTTP Basic header, find matching EE Account.

    Returns the EE Account name (i.e. account_name) on success.

    Raises EasyEcomGSPAuthError on any failure:
      - Missing/malformed Authorization header
      - Base64 decode error
      - No enabled EE Account with a matching gsp_basic_auth_secret

    Note: the username portion of the Basic header is IGNORED. Only
    the password portion is matched against secrets. This is a
    deliberate simplification — EE's contract treats the Basic
    credential as a shared secret, not a user identity.
    """
    if not auth_header:
        raise EasyEcomGSPAuthError("Missing Authorization header.")

    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        raise EasyEcomGSPAuthError(
            "Authorization header must be 'Basic <base64>'."
        )

    try:
        decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise EasyEcomGSPAuthError(
            f"Could not decode Basic auth credential: {exc}"
        ) from exc

    if ":" not in decoded:
        raise EasyEcomGSPAuthError(
            "Decoded credential must be in user:password format."
        )

    _, password = decoded.split(":", 1)

    # Match against any enabled EE Account's secret. Constant-time
    # comparison per secret to avoid timing leaks across accounts.
    enabled_accounts = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1},
        fields=["name"],
    )
    for acc in enabled_accounts:
        try:
            stored = get_decrypted_password(
                "EasyEcom Account", acc["name"], "gsp_basic_auth_secret",
                raise_exception=False,
            )
        except Exception:
            stored = None
        if not stored:
            continue
        if secrets.compare_digest(stored, password):
            return acc["name"]

    raise EasyEcomGSPAuthError(
        "No enabled EE Account matched the provided Basic auth secret."
    )


# ============================================================
# Bearer minting + validation — used by /gettoken (mint) + all
# downstream endpoints (validate)
# ============================================================


def issue_bearer(
    account_name: str,
    *,
    issued_to_ip: str | None = None,
) -> dict[str, Any]:
    """Mint a new Bearer token for the given EE Account.

    Returns {token, expires_in, expires_at_iso} — the plaintext token
    is returned ONCE here and never persisted. The hash is stored in
    tabEasyEcom GSP Token for later validation.

    Token format: 64-char hex (32 random bytes). Sufficient entropy
    to make brute-force impractical within the 1-hour TTL.
    """
    plaintext = secrets.token_hex(32)
    token_hash = _hash_token(plaintext)

    now = now_datetime()
    expires = add_to_date(now, seconds=TOKEN_TTL_SECONDS)

    doc = frappe.new_doc("EasyEcom GSP Token")
    doc.token_hash = token_hash
    doc.easyecom_account = account_name
    doc.issued_at = now
    doc.expires_at = expires
    doc.issued_to_ip = (issued_to_ip or "")[:140]
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()

    return {
        "token": plaintext,
        "expires_in": TOKEN_TTL_SECONDS,
        "expires_at_iso": expires.isoformat(),
    }


def validate_bearer(auth_header: str | None) -> str:
    """Decode `Authorization: Bearer <token>`, look up by hash, check
    expiry, update last_used_at. Returns the EE Account name on
    success.

    Raises EasyEcomGSPAuthError on any failure:
      - Missing/malformed Authorization header
      - Token hash not found (expired-and-cleaned-up, or never issued)
      - Token expired
    """
    if not auth_header:
        raise EasyEcomGSPAuthError("Missing Authorization header.")

    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise EasyEcomGSPAuthError(
            "Authorization header must be 'Bearer <token>'."
        )

    token_hash = _hash_token(parts[1])

    row = frappe.db.get_value(
        "EasyEcom GSP Token",
        {"token_hash": token_hash},
        ["name", "easyecom_account", "expires_at"],
        as_dict=True,
    )
    if not row:
        raise EasyEcomGSPAuthError("Bearer token is invalid or unknown.")

    if get_datetime(row["expires_at"]) < now_datetime():
        raise EasyEcomGSPAuthError("Bearer token has expired.")

    # Bump last_used_at (best-effort — doesn't fail auth if it errors).
    try:
        frappe.db.set_value(
            "EasyEcom GSP Token", row["name"],
            "last_used_at", now_datetime(),
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        pass

    return row["easyecom_account"]


# ============================================================
# Cleanup — scheduled task
# ============================================================


def cleanup_expired_tokens() -> dict[str, Any]:
    """Delete GSP Token rows past (expires_at + EXPIRED_RETENTION_DAYS).

    Runs daily via scheduler_events in hooks.py.
    """
    cutoff = add_to_date(now_datetime(), days=-EXPIRED_RETENTION_DAYS)
    expired = frappe.db.sql(
        """
        SELECT name FROM `tabEasyEcom GSP Token`
        WHERE expires_at < %s
        """,
        (cutoff,),
        as_dict=True,
    )
    for row in expired:
        frappe.delete_doc(
            "EasyEcom GSP Token", row["name"],
            ignore_permissions=True, force=True,
        )
    frappe.db.commit()
    return {"deleted": len(expired), "cutoff": cutoff.isoformat()}


# ============================================================
# Internals
# ============================================================


def _hash_token(plaintext: str) -> str:
    """SHA-256 hex of the token. Used both at issue time (to persist)
    and at validate time (to look up)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
