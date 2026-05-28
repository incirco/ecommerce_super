"""Shared place-of-supply / taxType resolver — §9 / §11 / §12.

EasyEcom's transactional endpoints (CreatePurchaseOrder for §9 PO push,
the §11 / §12 sales-order push) carry a `taxType` int on each line item
that EE uses to apply the right GST head:

  1 = IGST          (inter-state — supplier and warehouse in different
                     Indian states)
  2 = CGST + SGST   (intra-state — supplier and warehouse in the same
                     Indian state)
  3 = Custom        (overseas supplier — IGST + custom duty handled
                     differently on the EE side; from §9's point of
                     view this is the foreign-supplier path)

This module owns ONE function — `compute_tax_type` — that codifies
the above mapping. The §9 PO push calls it per line; §11 / §12 will
reuse when those packets ship.

This is INTENTIONALLY thin. We do NOT re-implement IC's full
place-of-supply transaction logic (`india_compliance.gst_india.utils.
get_place_of_supply`) — that function is shaped for an entire
transaction (party_details + doctype enum + multiple address-basis
fallbacks). Our case is simpler: given the two relevant states (and
the supplier's country), pick 1 / 2 / 3.

State derivation lives on the calling side — flows pass in already-
resolved `gst_state` strings from the Address records they've already
loaded. That keeps this function pure (no DB I/O), trivially testable,
and reusable across §9 / §11 / §12 without coupling to any one DocType.
"""

from __future__ import annotations

from typing import Final


# The three values EE expects on `taxType`.
TAX_TYPE_IGST: Final[int] = 1
TAX_TYPE_CGST_SGST: Final[int] = 2
TAX_TYPE_CUSTOM: Final[int] = 3


def compute_tax_type(
    supplier_state: str | None,
    warehouse_state: str | None,
    supplier_country: str | None,
) -> int:
    """Return EasyEcom's taxType int for one line item.

    Args:
      supplier_state    — supplier's GST state (e.g. "Maharashtra"). May
                          be None / empty for foreign suppliers; in
                          that case supplier_country drives the result.
      warehouse_state   — receiving warehouse's GST state. Falls under
                          the same Indian-state vocabulary IC uses.
      supplier_country  — supplier's country (e.g. "India", "United
                          States"). Anything non-India routes to the
                          Custom path (3).

    Rules:
      - Foreign supplier (supplier_country != "India" and supplier_country
        present-and-non-blank) → 3 (Custom). This wins over state
        comparison — overseas suppliers have no IC state we can usefully
        compare against.
      - Indian supplier:
          - states present and equal → 2 (CGST + SGST, intra-state)
          - states present and different → 1 (IGST, inter-state)
          - either state missing → 1 (IGST, inter-state) — fail-safe
            default because IGST is the conservative GST head (the
            recipient can claim it as ITC either way; under-charging
            CGST/SGST when it should have been IGST creates a
            reconciliation gap, the reverse does not).

    The fail-safe default is documented; flows that observe a `taxType`
    of 1 with one of the states missing should surface it as a
    Created-Flagged condition on the PO Map row (the FDE may want to
    fix the underlying Address record). This module just computes —
    flagging is the flow's job.
    """
    country = (supplier_country or "").strip()
    if country and country.lower() != "india":
        return TAX_TYPE_CUSTOM

    s_state = _normalise_state(supplier_state)
    w_state = _normalise_state(warehouse_state)

    if s_state and w_state and s_state == w_state:
        return TAX_TYPE_CGST_SGST
    return TAX_TYPE_IGST


def _normalise_state(state: str | None) -> str:
    """Case-fold + trim so 'Maharashtra' == 'maharashtra ' == ' MAHARASHTRA'.

    IC's gst_state field is typically the canonical title-case form
    (e.g. 'Maharashtra', 'Karnataka'), but addresses on multi-source
    sites sometimes carry casing variants. Normalising here is cheap and
    saves the calling flow from doing it per-line.
    """
    if not state:
        return ""
    return state.strip().lower()


__all__ = [
    "TAX_TYPE_IGST",
    "TAX_TYPE_CGST_SGST",
    "TAX_TYPE_CUSTOM",
    "compute_tax_type",
]
