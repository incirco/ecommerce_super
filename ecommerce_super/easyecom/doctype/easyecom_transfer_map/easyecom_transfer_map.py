"""§10 Stage 1 — Stock Transfer Map controller.

The §10 invariant (locked, packet line 11):
  When a financial pre-condition isn't met, the integration creates the
  dependent document in Draft and notifies, never auto-submits.
  SI-not-submitted → IPR-in-Draft. Submitted-DN-exists → late IPR in
  Draft. Manual-reconciliation states are surfaced via ERPNext-native
  UX, not auto-resolved.

Stage 1 substrate — DocType + validate guards ONLY. No flow logic, no
EE calls, no auto-creation of dependent documents. Stages 2-4 wire
the actual outbound / inbound / variance flows.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {
        "Mapped",
        "SI-Pending",
        "SI-Submitted",
        "EE-Pushed",
        "Partial-Received",
        "Fully-Received",
        "DN-Submitted-Locked",
        "Drift",
        "Disabled",
    }
)

VALID_EE_DOCTYPES: frozenset[str] = frozenset({"", "STN", "PO", "B2B"})


class EasyEcomTransferMap(Document):
    def validate(self) -> None:
        self._validate_status_value()
        self._validate_ee_doctype()
        self._validate_delivery_note_is_internal_customer()
        self._validate_warehouses_exist()
        self._populate_company_gstins()
        self._compute_gstin_different()
        self._validate_ee_order_id_implies_pushed()

    def _populate_company_gstins(self) -> None:
        """Resolve the GSTIN per Warehouse.

        GROUNDING CORRECTION (live Harmony smoke 2026-05-30): a single
        Company can register multiple GSTINs (one per state branch)
        and tag them to specific warehouse Addresses. Lookup order per
        Warehouse:
          1. Address linked to the Warehouse carrying a `gstin`
          2. Company.gstin (legacy / single-GSTIN sites)
        """
        for wh_field, gstin_field in (
            ("source_warehouse", "source_company_gstin"),
            ("target_warehouse", "target_company_gstin"),
        ):
            wh = self.get(wh_field)
            if not wh:
                self.set(gstin_field, "")
                continue
            addr_gstin = frappe.db.sql(
                """
                SELECT a.gstin
                FROM `tabAddress` a
                JOIN `tabDynamic Link` dl
                  ON dl.parent = a.name
                WHERE dl.parenttype = 'Address'
                  AND dl.link_doctype = 'Warehouse'
                  AND dl.link_name = %s
                  AND IFNULL(a.gstin, '') != ''
                LIMIT 1
                """,
                (wh,),
            )
            if addr_gstin and addr_gstin[0][0]:
                self.set(gstin_field, addr_gstin[0][0])
                continue
            company = frappe.db.get_value("Warehouse", wh, "company")
            if not company:
                self.set(gstin_field, "")
                continue
            self.set(
                gstin_field,
                frappe.db.get_value("Company", company, "gstin") or "",
            )

    def _validate_status_value(self) -> None:
        if self.status and self.status not in VALID_STATUS_VALUES:
            frappe.throw(
                _(
                    "EasyEcom Transfer Map status must be one of {0} — "
                    "got {1!r}."
                ).format(
                    ", ".join(sorted(VALID_STATUS_VALUES)),
                    self.status,
                )
            )

    def _validate_ee_doctype(self) -> None:
        value = self.ee_doctype or ""
        if value not in VALID_EE_DOCTYPES:
            frappe.throw(
                _(
                    "EasyEcom Transfer Map ee_doctype must be empty, "
                    "'STN', 'PO', or 'B2B' — got {0!r}."
                ).format(value)
            )

    def _validate_delivery_note_is_internal_customer(self) -> None:
        """§10 only handles Internal-Customer DNs. A regular sales-side
        DN belongs to §11/§12 (sales-order flows), not §10. Refuse the
        row up front rather than letting Stage 2's outbound hook pick
        up a wrong-typed DN and produce confused downstream state."""
        if not self.delivery_note:
            return  # reqd-check handles this
        if not frappe.db.exists("Delivery Note", self.delivery_note):
            frappe.throw(
                _(
                    "Delivery Note {0!r} does not exist. The Transfer "
                    "Map autoname keys on it; create the DN first."
                ).format(self.delivery_note)
            )
        is_internal = frappe.db.get_value(
            "Delivery Note", self.delivery_note, "is_internal_customer"
        )
        if not int(is_internal or 0):
            frappe.throw(
                _(
                    "Delivery Note {0!r} is not marked is_internal_"
                    "customer=1. §10 only handles internal-Company "
                    "transfers (Internal Customer / Internal Supplier "
                    "pair). Regular sales-side DNs belong to §11/§12."
                ).format(self.delivery_note)
            )

    def _validate_warehouses_exist(self) -> None:
        for field in ("source_warehouse", "target_warehouse"):
            value = self.get(field)
            if value and not frappe.db.exists("Warehouse", value):
                frappe.throw(
                    _(
                        "Transfer Map {0} = {1!r} does not exist."
                    ).format(field, value)
                )

    def _compute_gstin_different(self) -> None:
        """Derived flag — source vs target Company GSTIN. Self-healing:
        if fetch_from didn't populate the GSTIN fields (e.g. Company has
        no GSTIN set), the flag falls to 0 and downstream flows treat
        as same-GSTIN. The precheck guards against missing GSTIN at
        go-live."""
        src = (self.source_company_gstin or "").strip().upper()
        tgt = (self.target_company_gstin or "").strip().upper()
        # Both empty → no signal, default to same (no SI auto-draft).
        # One set, one empty → treat as different (forces FDE attention
        # via the Drift state if Stage 2 / 3 finds incoherent state).
        if not src and not tgt:
            self.gstin_different = 0
            return
        self.gstin_different = 1 if src != tgt else 0

    def _validate_ee_order_id_implies_pushed(self) -> None:
        """An ee_order_id (or ee_po_id on the PO path) implies a push
        happened — status must reflect it. Catches the case where a
        manual edit sets the EE id but leaves status=Mapped, which
        would let Stage 2's auto-push gate think the push is still
        pending and re-fire."""
        has_ee_id = bool((self.ee_order_id or "").strip()) or bool(
            self.ee_po_id
        )
        if has_ee_id and self.status == "Mapped":
            frappe.throw(
                _(
                    "Transfer Map has EE id captured "
                    "(ee_order_id={0!r}, ee_po_id={1!r}) but status is "
                    "still 'Mapped'. Either clear the EE id or "
                    "transition status (e.g. to 'EE-Pushed')."
                ).format(self.ee_order_id, self.ee_po_id)
            )


@frappe.whitelist()
def get_cumulative_receipt_summary(transfer_map: str) -> dict:
    """§10 Stage 4 — server-authoritative per-Item summary for the
    Transfer Map form's "Cumulative Receipt" dashboard chip. Reuses
    transfer_inbound._cumulative_received_per_item so the math is the
    same as the IPI/DN gap arithmetic."""
    if not transfer_map or not frappe.db.exists(
        "EasyEcom Transfer Map", transfer_map
    ):
        return {"rows": []}

    tm = frappe.get_doc("EasyEcom Transfer Map", transfer_map)
    from ecommerce_super.easyecom.flows.transfer_inbound import (
        _cumulative_received_per_item,
    )

    # Dispatched qty source — SI if present (different-GSTIN), else DN.
    if tm.sales_invoice and frappe.db.exists(
        "Sales Invoice", tm.sales_invoice
    ):
        rows = frappe.db.sql(
            """SELECT item_code, qty FROM `tabSales Invoice Item`
               WHERE parent = %s""",
            (tm.sales_invoice,),
            as_dict=True,
        )
    else:
        rows = frappe.db.sql(
            """SELECT item_code, qty FROM `tabDelivery Note Item`
               WHERE parent = %s""",
            (tm.delivery_note,),
            as_dict=True,
        )
    dispatched: dict[str, float] = {}
    for r in rows:
        dispatched[r["item_code"]] = (
            dispatched.get(r["item_code"], 0) + float(r["qty"] or 0)
        )
    cumulative = _cumulative_received_per_item(tm)
    return {
        "transfer_map": transfer_map,
        "rows": [
            {
                "item_code": code,
                "dispatched": qty,
                "received": cumulative.get(code, 0),
            }
            for code, qty in dispatched.items()
        ],
    }
