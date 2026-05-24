"""SHA-256 hashing helpers with deterministic JSON normalisation.

Used by:
  - Idempotency-key formulae (§6.1) — change_hash is sha256 of normalised JSON
  - API Call.request_payload_hash and response_payload_hash (§31.2.4)
  - Sync Record.push_payload_hash and pull_payload_hash (§31.2.3)
  - Webhook Event.payload_hash (§31.2.5)
  - Schema Snapshot (§20)

`normalise_json` produces a stable byte string from a Python dict/list by
sorting keys recursively and stripping whitespace, so equivalent payloads
hash identically across runs and across machines.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def normalise_json(payload: Any) -> bytes:
    """Return canonical bytes for `payload`: keys sorted recursively, no
    whitespace, UTF-8 encoded. Equivalent inputs return identical bytes."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def sha256_hex(payload: Any) -> str:
    """Return the SHA-256 hex digest of `payload`, normalising first if it's
    a dict or list. Raw bytes and str are hashed as given."""
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = normalise_json(payload)
    return hashlib.sha256(data).hexdigest()


def sha256_idempotency(*parts: str) -> str:
    """Build an idempotency key from the parts in §6.1's formulae.

    Example:
        sha256_idempotency("item", company, item_code, location_key, change_hash)
    """
    payload = ":".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
