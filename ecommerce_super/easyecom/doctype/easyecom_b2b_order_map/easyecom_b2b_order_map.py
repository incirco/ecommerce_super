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
  Invoice Pending    — polling detected EE invoice generation; the
                       SI mirror has not yet run (or ran and raised
                       a Discrepancy). Next §11.5.2 tick retries.
  Invoice Generated  — SI mirror complete; `sales_invoice` link is
                       populated on this Map. Set by §11.5.2 Mode 2
                       polling + by /einvoice/update in Mode 1.
  Cancelled          — explicit cancel (ERPNext-initiated or
                       EE-initiated via inbound webhook).
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


# ============================================================
# Lifecycle view — "at one place, see the whole story" (quick
# foundation for the #150 dashboard). Called from JS on refresh.
# ============================================================


@frappe.whitelist()
def get_lifecycle(map_name: str) -> list[dict]:
    """Return the ordered lifecycle stages for a B2B Order Map.

    Each stage: {stage, ok, timestamp, link_doctype, link_name, detail}.
    Read-only — no side effects. Consumed by the "Lifecycle" section
    on the B2B Order Map form.

    Stages (chronological):
      1. SO Submitted
      2. Pushed to EE (Map created)
      3. EE Accepted / Invoice IDs assigned
      4. SI Mirrored (our side)
      5. IRN + Eway Mint (India Compliance)
      6. API Call summary (count + last inbound/outbound)
    """
    if not frappe.has_permission("EasyEcom B2B Order Map", "read", doc=map_name):
        frappe.throw(_("Not permitted to read {0}").format(map_name))
    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    stages: list[dict] = []

    # --- Stage 1: SO Submitted ---
    so_name = map_doc.sales_order
    so = None
    if so_name and frappe.db.exists("Sales Order", so_name):
        so = frappe.db.get_value(
            "Sales Order", so_name,
            ["creation", "docstatus", "grand_total", "currency"],
            as_dict=True,
        )
    stages.append({
        "stage": "SO Submitted",
        "ok": bool(so and so.docstatus == 1),
        "timestamp": str(so.creation) if so else None,
        "link_doctype": "Sales Order" if so else None,
        "link_name": so_name if so else None,
        "detail": (
            f"grand_total {so.currency or 'INR'} {so.grand_total} (docstatus={so.docstatus})"
            if so else "Source SO not found — Map is orphaned"
        ),
    })

    # --- Stage 2: Pushed to EE (Map created = push happened) ---
    stages.append({
        "stage": "Pushed to EE",
        "ok": bool(map_doc.status),
        "timestamp": str(map_doc.creation),
        "link_doctype": "EasyEcom B2B Order Map",
        "link_name": map_doc.name,
        "detail": (
            f"module={map_doc.module or '?'}, initial status={map_doc.status or '?'}"
        ),
    })

    # --- Stage 3: EE Accepted / IDs assigned ---
    has_order_id = bool(map_doc.ee_order_id)
    has_invoice_id = bool(map_doc.invoice_id)
    id_bits = []
    if has_order_id:
        id_bits.append(f"ee_order_id={map_doc.ee_order_id}")
    if map_doc.get("ee_suborder_id"):
        id_bits.append(f"ee_suborder_id={map_doc.ee_suborder_id}")
    if has_invoice_id:
        id_bits.append(f"invoice_id={map_doc.invoice_id}")
    stages.append({
        "stage": "EE Accepted (IDs assigned)",
        "ok": has_order_id or has_invoice_id,
        "timestamp": (
            str(map_doc.modified) if (has_order_id or has_invoice_id) else None
        ),
        "link_doctype": None,
        "link_name": None,
        "detail": (
            ", ".join(id_bits) if id_bits
            else "Not yet — New B2B queued orders backfill via polling"
        ),
    })

    # --- Stage 4: SI Mirrored ---
    si_name = map_doc.sales_invoice
    si = None
    if si_name and frappe.db.exists("Sales Invoice", si_name):
        # Read IC fields defensively — they may not exist on all sites.
        si_fields = ["creation", "modified", "docstatus", "grand_total", "currency"]
        for opt in ("irn", "ewaybill", "einvoice_status"):
            if frappe.db.has_column("Sales Invoice", opt):
                si_fields.append(opt)
        si = frappe.db.get_value(
            "Sales Invoice", si_name, si_fields, as_dict=True,
        )
    stages.append({
        "stage": "SI Mirrored",
        "ok": bool(si),
        "timestamp": str(si.creation) if si else None,
        "link_doctype": "Sales Invoice" if si else None,
        "link_name": si_name if si else None,
        "detail": (
            f"grand_total {si.currency or 'INR'} {si.grand_total} (docstatus={si.docstatus})"
            if si else "Not yet — EE invoice has not been mirrored"
        ),
    })

    # --- Stage 5: IRN + Eway Mint (India Compliance) ---
    irn = getattr(si, "irn", None) if si else None
    eway = getattr(si, "ewaybill", None) if si else None
    mint_bits = []
    if irn:
        mint_bits.append(f"IRN {irn[:16]}…" if len(irn) > 16 else f"IRN {irn}")
    if eway:
        mint_bits.append(f"Eway {eway}")
    stages.append({
        "stage": "IRN + Eway Minted",
        "ok": bool(irn or eway),
        "timestamp": str(si.modified) if si and (irn or eway) else None,
        "link_doctype": None,
        "link_name": None,
        "detail": (
            " • ".join(mint_bits) if mint_bits
            else "Not yet — depends on Account mint toggles (gsp_mint_einvoice / gsp_mint_ewaybill)"
        ),
    })

    # --- Stage 6: API Call summary for this SO ---
    api_calls = _summarise_api_calls_for_map(map_doc)
    stages.append({
        "stage": "API Calls",
        "ok": api_calls["total"] > 0,
        "timestamp": api_calls.get("last_at"),
        "link_doctype": "EasyEcom API Call" if api_calls["total"] > 0 else None,
        # No single link — the summary tells the story
        "link_name": None,
        "detail": (
            f"{api_calls['total']} total ({api_calls['outbound']} outbound, "
            f"{api_calls['inbound']} inbound)"
            + (f", last: {api_calls['last_endpoint']} → HTTP {api_calls['last_status']}"
               if api_calls.get("last_endpoint") else "")
        ),
    })

    return stages


def _summarise_api_calls_for_map(map_doc) -> dict:
    """Compact API Call summary for a Map: totals per direction + the
    latest call's endpoint + status. Uses SO name / EE ids as search
    anchors on the API Call log."""
    filters_or = []
    if map_doc.sales_order:
        filters_or.append(["endpoint", "like", f"%{map_doc.sales_order}%"])
    if map_doc.ee_order_id:
        filters_or.append(["response_body", "like", f"%{map_doc.ee_order_id}%"])
    if map_doc.invoice_id:
        filters_or.append(["response_body", "like", f"%{map_doc.invoice_id}%"])
    if not filters_or:
        return {"total": 0, "outbound": 0, "inbound": 0}

    # Rough count — we'll count everything matching any of the anchors
    # via OR. Not exact (could double-count if multiple anchors match
    # the same row) but good enough for the summary widget.
    try:
        rows = frappe.db.get_all(
            "EasyEcom API Call",
            or_filters=filters_or,
            fields=["name", "direction", "endpoint", "http_status", "creation"],
            order_by="creation desc",
            limit=100,
        )
    except Exception:
        # Column shape mismatch on some sites — degrade gracefully.
        return {"total": 0, "outbound": 0, "inbound": 0}

    outbound = sum(1 for r in rows if r.direction == "Outbound")
    inbound = sum(1 for r in rows if r.direction == "Inbound")
    result = {
        "total": len(rows),
        "outbound": outbound,
        "inbound": inbound,
    }
    if rows:
        latest = rows[0]
        result["last_at"] = str(latest.creation)
        result["last_endpoint"] = latest.endpoint or "?"
        result["last_status"] = latest.http_status or "?"
    return result
