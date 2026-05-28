"""EasyEcom PO Map controller.

§9 Stage 1 — substrate only. The two-key bridge between an ERPNext
Purchase Order and EasyEcom's two push channels (CreatePurchaseOrder
keyed on reference_code, updatePoStatus keyed on ee_po_id).

ERPNext is the source of truth for §9 — POs are ERPNext-born and
content-immutable post-submit, so there is no content-snapshot field
on this row. The only drift surface is po_status. See §9 Stage 3
(GRN pull + status reconciliation) for the comparison logic.

Stages 2-4 wire the actual push / reconciliation / UI flows on top of
this substrate. Stage 1 ships the schema only.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {"Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"}
)


class EasyEcomPOMap(Document):
    def validate(self) -> None:
        self._validate_status_value()
        self._validate_purchase_order_exists()
        self._validate_reference_code_matches_po()

    def _validate_status_value(self) -> None:
        if self.status and self.status not in VALID_STATUS_VALUES:
            frappe.throw(
                _(
                    "EasyEcom PO Map status must be one of {0} — got {1!r}."
                ).format(
                    ", ".join(sorted(VALID_STATUS_VALUES)),
                    self.status,
                ),
                frappe.ValidationError,
            )

    def _validate_purchase_order_exists(self) -> None:
        """The reqd flag on purchase_order catches absence; this guards
        the rare API-write case where the value is set to a non-existent
        PO name (Frappe's Link fieldtype tolerates that at the JSON
        layer)."""
        if not self.purchase_order:
            return
        if not frappe.db.exists("Purchase Order", self.purchase_order):
            frappe.throw(
                _(
                    "Linked Purchase Order {0!r} does not exist."
                ).format(self.purchase_order),
                frappe.ValidationError,
            )

    def _validate_reference_code_matches_po(self) -> None:
        """The autoname formula stamps name=ECS-PO-{purchase_order}, and
        the §9 push uses reference_code as the EE-side content key. They
        must stay in lockstep — out-of-band edits that decouple them
        would silently break the §9 Stage 3 PO-resolution fallback
        (po_ref_num → ERPNext PO name)."""
        if not self.reference_code or not self.purchase_order:
            return
        if self.reference_code != self.purchase_order:
            frappe.throw(
                _(
                    "reference_code ({0!r}) must equal the linked Purchase "
                    "Order name ({1!r}). The §9 content push keys on "
                    "reference_code = PO name."
                ).format(self.reference_code, self.purchase_order),
                frappe.ValidationError,
            )
