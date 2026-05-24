"""Per-operation idempotency-key builders (SPEC §6.1).

The §6.1 table is the **contract**: each outbound mutating operation has a
named key builder whose formula is reproducible — re-running the same
logical operation produces the same key, allowing both EE-side dedup
(where supported) and our retry logic to detect duplicates without
coordination.

Each function here wraps the foundation primitive
`utils.hashing.sha256_idempotency` with the formula for one operation. The
parts are passed positionally and joined with ``:`` exactly as the spec
says (formulae lifted verbatim from §6.1):

  item            sha256(f'item:{company}:{item_code}:{ee_location_key}:{change_hash}')
  customer        sha256(f'customer:{company}:{customer_name}:{ee_location_key}:{change_hash}')
  supplier        sha256(f'supplier:{company}:{supplier_name}:{ee_location_key}:{change_hash}')
  po              sha256(f'po:{company}:{po_name}:{ee_location_key}')
  so              sha256(f'so:{company}:{so_name}:{ee_location_key}')
  b2b_invoice     sha256(f'b2b_invoice:{company}:{si_name}:{ee_location_key}')

On retry, the *same* key is inherited from the original Sync Record —
never recomputed. Callers must store the key on the Sync Record at
first-attempt time and re-use it.

For internal-bookkeeping job types that are NOT in the §6.1 table
(Webhook Process, SLA Breach Compute, Schema Snapshot Compute, etc.),
use `internal_job_key` — a documented, named builder so even those go
through this module rather than an ad-hoc fallback in the facade. The
fallback in `queue/__init__.py` has been removed in this completion
packet; callers MUST pass a key built here.
"""

from __future__ import annotations

from ecommerce_super.easyecom.utils.hashing import sha256_hex, sha256_idempotency

# ----- §6.1 operation builders -----


def item_push_key(
    *, company: str, item_code: str, ee_location_key: str, change_hash: str
) -> str:
    """sha256(f'item:{company}:{item_code}:{ee_location_key}:{change_hash}')"""
    return sha256_idempotency("item", company, item_code, ee_location_key, change_hash)


def customer_push_key(
    *, company: str, customer_name: str, ee_location_key: str, change_hash: str
) -> str:
    """sha256(f'customer:{company}:{customer_name}:{ee_location_key}:{change_hash}')"""
    return sha256_idempotency(
        "customer", company, customer_name, ee_location_key, change_hash
    )


def supplier_push_key(
    *, company: str, supplier_name: str, ee_location_key: str, change_hash: str
) -> str:
    """sha256(f'supplier:{company}:{supplier_name}:{ee_location_key}:{change_hash}')"""
    return sha256_idempotency(
        "supplier", company, supplier_name, ee_location_key, change_hash
    )


def po_push_key(*, company: str, po_name: str, ee_location_key: str) -> str:
    """sha256(f'po:{company}:{po_name}:{ee_location_key}')

    PO names are immutable in ERPNext, so no change_hash is needed —
    identity of the PO suffices to identify the operation.
    """
    return sha256_idempotency("po", company, po_name, ee_location_key)


def so_push_key(*, company: str, so_name: str, ee_location_key: str) -> str:
    """sha256(f'so:{company}:{so_name}:{ee_location_key}')

    Same reasoning as PO — SO names are immutable.
    """
    return sha256_idempotency("so", company, so_name, ee_location_key)


def b2b_invoice_push_key(*, company: str, si_name: str, ee_location_key: str) -> str:
    """sha256(f'b2b_invoice:{company}:{si_name}:{ee_location_key}')

    Sales Invoice names are immutable post-submission.
    """
    return sha256_idempotency("b2b_invoice", company, si_name, ee_location_key)


# ----- Internal-bookkeeping builder (not in §6.1 table) -----


def internal_job_key(
    *,
    job_type: str,
    company: str,
    target_doctype: str | None = None,
    target_name: str | None = None,
    payload: dict | None = None,
) -> str:
    """Idempotency key for internal-bookkeeping job types that are NOT in
    the §6.1 operation table — Webhook Process, SLA Breach Compute,
    Schema Snapshot Compute, Mapping Coverage Compute, Morning Brief
    Compute, Configuration Audit Write, Field Mapping Compile.

    Spec §6.1 doesn't dictate a formula for these because they don't
    mutate EE state. The key still must be deterministic so retries dedup
    correctly. We use a clearly-namespaced shape so it's never confused
    with a §6.1 outbound key:

      sha256(f'internal:{job_type}:{company}:{target_doctype}:{target_name}:{payload_hash}')

    Callers (the cron handlers and the webhook router) must use this
    rather than ad-hoc sha256 calls so all keys go through a named
    builder (the "no silent divergence" rule, §2.7).
    """
    payload_hash = sha256_hex(payload or {})
    return sha256_idempotency(
        "internal",
        job_type,
        company,
        target_doctype or "",
        target_name or "",
        payload_hash,
    )


# ----- Change-hash helper exposed at this layer too -----


def change_hash(payload: dict | list) -> str:
    """Canonical SHA-256 of the normalised JSON payload.

    Re-exports `sha256_hex` at the §6.1 layer so flow code can write::

        from ecommerce_super.easyecom.utils import idempotency
        ch = idempotency.change_hash(payload_dict)
        key = idempotency.item_push_key(..., change_hash=ch)

    without reaching into utils.hashing.
    """
    return sha256_hex(payload)


__all__ = [
    "item_push_key",
    "customer_push_key",
    "supplier_push_key",
    "po_push_key",
    "so_push_key",
    "b2b_invoice_push_key",
    "internal_job_key",
    "change_hash",
]
