"""UUIDv7 generation for correlation IDs.

UUIDv7 (RFC 9562) is time-ordered: the leading 48 bits are the Unix timestamp
in milliseconds, big-endian. This makes IDs sortable by creation time and
gives database indexes locality of reference, which matters because every
log DocType (API Call, Sync Record, Queue Job, Webhook Event) carries a
correlation_id index and most queries are time-ordered.

Python's stdlib `uuid` module does not yet ship a `uuid7()` constructor, so
we build one. Format (RFC 9562 §5.7):

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                           unix_ts_ms                          |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |          unix_ts_ms           |  ver  |       rand_a          |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |var|                        rand_b                             |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                            rand_b                             |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7 (time-ordered) per RFC 9562."""
    unix_ts_ms = int(time.time() * 1000)
    # 10 random bytes for rand_a (12 bits) + rand_b (62 bits) + version/variant
    rand = os.urandom(10)
    # 16 bytes total: 6 ts bytes || 10 rand bytes
    raw = unix_ts_ms.to_bytes(6, "big") + rand
    b = bytearray(raw)
    # Set version: high nibble of byte 6 = 0b0111
    b[6] = (b[6] & 0x0F) | 0x70
    # Set variant: top 2 bits of byte 8 = 0b10
    b[8] = (b[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(b))


def new_correlation_id() -> str:
    """Return a fresh UUIDv7 as a canonical string. Use at every operation
    entry point (poll tick, webhook receipt, document-event hook, scheduled
    job, manual action). Propagate downstream — never mint a new one at an
    intermediate layer."""
    return str(uuid7())
