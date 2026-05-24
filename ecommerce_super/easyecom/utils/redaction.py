"""Credential-aware redaction for log payloads.

Every API Call and Webhook Event payload passes through `redact()` before
persistence. SPEC §3.7.4 lists the field names that must be redacted, plus
a Bearer-token value pattern.

Redaction is centralised here — no flow may reach for raw payloads and bypass
this. A redaction failure is itself an audit event (caller must catch and
record); silent failure is forbidden.
"""

from __future__ import annotations

import re
from typing import Any

# Field names whose values are always redacted, regardless of payload location.
# Comparison is case-insensitive; matching is exact on the (lowercased)
# fieldname. Both header-style (`x-api-key`) and snake_case (`x_api_key`)
# forms are covered.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {
        "x_api_key",
        "x-api-key",
        "xapikey",
        "authorization",
        "password",
        "token",
        "secret",
        "jwt",
        "jwt_token",
        "webhook_token",
        "email",
        "slack_webhook_url",
        "access_token",
        "access-token",
        "api_key",
        "apikey",
    }
)

# Values matching this pattern are also redacted (covers cases where a
# Bearer token is stuffed into a non-standard field name).
_BEARER_RE = re.compile(r"^Bearer\s+[A-Za-z0-9._\-+/=]+$", re.IGNORECASE)
_JWT_RE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")

REDACTED_PLACEHOLDER: str = "***REDACTED***"


def _should_redact_field(name: str) -> bool:
    return name.lower().replace(" ", "") in REDACTED_FIELDS


def _value_looks_like_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if _BEARER_RE.match(value):
        return True
    if _JWT_RE.match(value) and len(value) > 50:
        # A 3-part dot-separated string longer than ~50 chars is almost
        # certainly a JWT, not a regular dotted identifier.
        return True
    return False


def redact(payload: Any, *, extra_fields: set[str] | None = None) -> Any:
    """Return a deep copy of `payload` with credentials redacted.

    - dict: every key is checked against REDACTED_FIELDS (case-insensitive,
      whitespace-stripped); matching values are replaced with the placeholder.
      Non-matching values are recursed into.
    - list/tuple: each element is recursed into.
    - str: if the value matches a Bearer/JWT pattern, replaced.
    - other: returned as-is.

    `extra_fields` lets the caller add per-call fieldnames to redact (e.g.
    a payload that uses a custom auth header).
    """
    extras = {f.lower().replace(" ", "") for f in (extra_fields or set())}

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict = {}
            for k, v in node.items():
                key_str = str(k)
                lk = key_str.lower().replace(" ", "")
                if lk in REDACTED_FIELDS or lk in extras:
                    out[key_str] = REDACTED_PLACEHOLDER
                else:
                    out[key_str] = _walk(v)
            return out
        if isinstance(node, (list, tuple)):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            if _value_looks_like_secret(node):
                return REDACTED_PLACEHOLDER
            return node
        return node

    return _walk(payload)


def redact_url(url: str) -> str:
    """Strip credential-looking query parameters from a URL string.

    Common cases: `?api_key=xxx`, `?token=xxx`, `?password=xxx`.
    Other parameters are preserved.
    """
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts: list[str] = []
    for pair in query.split("&"):
        if "=" not in pair:
            parts.append(pair)
            continue
        name, _, value = pair.partition("=")
        if _should_redact_field(name):
            parts.append(f"{name}={REDACTED_PLACEHOLDER}")
        else:
            parts.append(f"{name}={value}")
    return base + "?" + "&".join(parts)
