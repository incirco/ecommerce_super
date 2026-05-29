"""EasyEcom GRN Map controller.

§9 Stage 1 — substrate only. One row per EE grn_id observed via
/Grn/V2/getGrnDetails. EE-born; ee_grn_id is the natural primary key
and the idempotency hinge for the Stage 3 pull.

Stage 1 ships the schema only. Stage 3 wires the actual pull / PR
creation / status reconciliation / STN routing flows.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {
        "Pending",
        "Receipted",
        "Held-Pre-QC",
        "STN-Routed",
        "Failed",
        "Discrepancy",
        "Deleted-Post-Receipt",
        # Corrective commit 2026-05-29: unknown-PO drift dismissed by
        # FDE (the GRN should not be received — noise / duplicate).
        "Dismissed",
    }
)

# EE GRN lifecycle codes — live finding 2026-05-28: in addition to the
# documented 1-4, real Harmony returns `5` on GRNs that have moved past
# QC Complete into a settled / closed state. We tolerate the documented
# 1-4 + observed 5 + a small safety margin (6-10) for any other
# downstream statuses EE might add. Rejection is reserved for clearly
# bogus values (0, negative, or > 10).
VALID_GRN_STATUS_IDS: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10})


class EasyEcomGRNMap(Document):
    def validate(self) -> None:
        self._validate_status_value()
        self._validate_grn_status_id()
        self._validate_stn_routing_consistency()

    def _validate_status_value(self) -> None:
        if self.status and self.status not in VALID_STATUS_VALUES:
            frappe.throw(
                _(
                    "EasyEcom GRN Map status must be one of {0} — got {1!r}."
                ).format(
                    ", ".join(sorted(VALID_STATUS_VALUES)),
                    self.status,
                ),
                frappe.ValidationError,
            )

    def _validate_grn_status_id(self) -> None:
        """EE only ships 1/2/3/4 on grn_status_id. Tolerate NULL (Stage 1
        substrate may insert rows before the pull fills the header) but
        reject anything outside the documented range."""
        if self.grn_status_id is None:
            return
        try:
            value = int(self.grn_status_id)
        except (TypeError, ValueError):
            frappe.throw(
                _("grn_status_id must be an integer — got {0!r}.").format(
                    self.grn_status_id
                ),
                frappe.ValidationError,
            )
        if value not in VALID_GRN_STATUS_IDS:
            frappe.throw(
                _(
                    "grn_status_id must be one of {0} (EE lifecycle 1 CREATED / "
                    "2 QC Pending / 3 QC Complete / 4 Deleted) — got {1}."
                ).format(sorted(VALID_GRN_STATUS_IDS), value),
                frappe.ValidationError,
            )

    def _validate_stn_routing_consistency(self) -> None:
        """STN-Routed rows must have routed_to_stn=1 and no PR; the
        inverse pair (routed_to_stn=1 but status != STN-Routed) is also
        nonsense. Substrate-layer guard so flow code in Stage 3 doesn't
        have to defend against bad rows."""
        if self.status == "STN-Routed":
            if not int(self.routed_to_stn or 0):
                frappe.throw(
                    _(
                        "GRN Map status=STN-Routed requires routed_to_stn=1 "
                        "(§9 ↔ §10 boundary marker)."
                    ),
                    frappe.ValidationError,
                )
            if self.purchase_receipt:
                frappe.throw(
                    _(
                        "STN-Routed GRNs cannot have a Purchase Receipt — §10 "
                        "STN-inward handles these; §9 creates NO PR."
                    ),
                    frappe.ValidationError,
                )
        elif int(self.routed_to_stn or 0):
            frappe.throw(
                _(
                    "routed_to_stn=1 requires status=STN-Routed — got {0!r}."
                ).format(self.status),
                frappe.ValidationError,
            )
