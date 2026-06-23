"""§11 Phase 1 substrate — B2B Order Map controller.

Mirrors §10's EasyEcom Transfer Map structure: one row per anchor
ERPNext document (Sales Order here, Delivery Note for §10), carrying
the EE-side identifiers + status lifecycle.

Stage 1 substrate — DocType + validate guards ONLY. No push, no EE
calls, no auto-creation of dependent documents. Stages 2-3 wire the
actual push / cancel / polling flows.

Status lifecycle:
  Pushed             — Old B2B successful sync push.
  Queued             — New B2B successful queue acknowledgement.
  Invoice Pending    — polling detected EE invoice generation
                       (Phase 1 marker; Phase 2 SI mirror NYI).
  Invoice Generated  — Phase 2 SI mirror complete (NOT used in
                       Phase 1; defined for forward compatibility).
  Cancelled          — explicit cancel (ERPNext-initiated in
                       Phase 1; EE-initiated webhook in Phase 2).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {
        "Pushed",
        "Queued",
        "Invoice Pending",
        "Invoice Generated",
        "Cancelled",
    }
)

VALID_MODULE_VALUES: frozenset[str] = frozenset({"Old B2B", "New B2B"})


class EasyEcomB2BOrderMap(Document):
    def validate(self) -> None:
        self._validate_status_value()
        self._validate_module_value()
        self._validate_sales_order_exists()
        self._validate_ee_order_id_implies_pushed_or_later()

    def _validate_status_value(self) -> None:
        if self.status and self.status not in VALID_STATUS_VALUES:
            frappe.throw(
                _(
                    "EasyEcom B2B Order Map status must be one of {0} "
                    "— got {1!r}."
                ).format(
                    ", ".join(sorted(VALID_STATUS_VALUES)), self.status
                )
            )

    def _validate_module_value(self) -> None:
        if self.module and self.module not in VALID_MODULE_VALUES:
            frappe.throw(
                _(
                    "EasyEcom B2B Order Map module must be 'Old B2B' "
                    "or 'New B2B' — got {0!r}."
                ).format(self.module)
            )

    def _validate_sales_order_exists(self) -> None:
        """Defensive — the Link field already enforces existence at
        validate time, but the autoname template embeds the SO name
        directly so we surface a clearer message when the Link is
        somehow set without the row existing (manual SQL etc.)."""
        if not self.sales_order:
            return
        if not frappe.db.exists("Sales Order", self.sales_order):
            frappe.throw(
                _(
                    "Sales Order {0!r} does not exist. The B2B Order "
                    "Map autoname keys on it; create the SO first."
                ).format(self.sales_order)
            )

    def _validate_ee_order_id_implies_pushed_or_later(self) -> None:
        """An ee_order_id captured implies a successful push happened
        — status cannot be left in a pre-push state. Catches the case
        where a manual edit sets the EE id but leaves status at a
        value that would make the polling reconciler think the push
        hasn't landed yet.

        New B2B's `Queued` status is allowed without ee_order_id
        because EE returns no identifiers at that stage; polling will
        backfill the ee_order_id and transition to a downstream
        status."""
        has_ee_order_id = bool((self.ee_order_id or "").strip())
        if not has_ee_order_id:
            return
        if self.status in {"Cancelled"}:
            return  # cancellation preserves the ee_order_id for audit
        # ee_order_id present implies the order is at least Pushed.
        # If status is Queued, that's incoherent — Queued means EE
        # hasn't returned an ID yet.
        if self.status == "Queued":
            frappe.throw(
                _(
                    "B2B Order Map has ee_order_id={0!r} captured but "
                    "status is still 'Queued'. Either clear the EE id "
                    "or transition status (e.g. to 'Pushed')."
                ).format(self.ee_order_id)
            )
