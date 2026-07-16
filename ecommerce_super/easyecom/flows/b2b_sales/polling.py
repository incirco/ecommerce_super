"""§11 Stage 3 — B2B Order Map polling reconciliation.

Design pivot from packet (approved 2026-06-14):
  - Packet assumed getAllOrders + cursor watermark sweep.
  - Endpoint probe surfaced EE's 7-day cap on `created_after`; a Map
    older than 7 days would be permanently abandoned.
  - getOrderDetails accepts `reference_code` with NO date constraint,
    deterministic per-Map lookup keyed on identifiers we already own.
  - Phase 1 polling is therefore: per-Map probe via
    /orders/V2/getOrderDetails?reference_code=<SO.name>. getAllOrders
    is DROPPED from Phase 1 entirely (zero value when ERPNext
    originates every B2B SO).

Cadence model:
  - Scheduler tick is FIXED at 5 minutes (hooks.py cron */5).
  - Per-Account `ecs_polling_cadence_minutes` Custom Field (default
    15) gates which Maps qualify per tick: a Map is polled iff
      last_polled_at IS NULL OR last_polled_at <= NOW() - cadence
  - Null `last_polled_at` means "never polled" — qualifies on the
    first tick after Map creation (~5 min delay to first poll).

Status derivation (LOCKED — see derive_local_status_from_ee_rows
docstring for the full rule table). Handles EE's two multi-row
semantics:
  - State-change history (new row per state change, same order_id).
  - Shipment splits (separate invoice rows per fulfillment chunk,
    same reference_code).

Discrepancy kinds raised:
  - "B2B Map orphaned at EE"
  - "B2B order cancelled by EE — polling-detected"
  - "B2B order partial cancellation detected"
  - "B2B unknown order_status_id"
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import now_datetime

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET
from ecommerce_super.easyecom.exceptions import (
    EasyEcomAPIError,
    EasyEcomError,
)
from ecommerce_super.easyecom.helpers.warehouse_mapping import (
    get_ee_location_for_warehouse,
)
from ecommerce_super.easyecom.utils.correlation import new_correlation_id


PENDING_STATUSES: frozenset[str] = frozenset(
    {"Pushed", "Queued", "Invoice Pending"}
)

# Locked enum per EE docs.
KNOWN_ACTIVE_STATUS_IDS: frozenset[int] = frozenset(
    {1, 2, 3, 4, 5, 6, 7, 30}
)
CANCELLED_STATUS_ID: int = 9

# §11.6 dispatch transitions — order_status_id → ecs_easyecom_dispatch_status
# label. Pre-shipment statuses (1-4, 30) collapse to "Pending"; shipment
# milestones (5/6/7) get distinct labels. status_id=9 is "Cancelled" but
# is also handled by the full-cancellation derivation path — we still
# stamp dispatch_status=Cancelled for SI-level visibility.
DISPATCH_STATUS_BY_ID: dict[int, str] = {
    1: "Pending", 2: "Pending", 3: "Pending", 4: "Pending", 30: "Pending",
    5: "Shipped",
    6: "Delivered",
    7: "Returned",
    9: "Cancelled",
}

# EE response keys to scan for a carrier tracking URL. EE's getOrderDetails
# response is inconsistent across older Old B2B vs newer New B2B payloads;
# the scan is defensive — first non-empty wins. If you see a payload with
# a tracking link in a key not listed here, add it (keep existing order).
TRACKING_URL_CANDIDATE_KEYS: tuple[str, ...] = (
    "tracking_link",
    "tracking_url",
    "track_link",
    "shipping_track_link",
    "courier_tracking_url",
)


# ============================================================
# Scheduler entry point.
# ============================================================


def reconcile_all_pending_b2b_orders() -> dict:
    """Cron tick handler (every 5 min). Iterates each enabled EE
    Account; for each, finds qualifying Maps (cooldown elapsed) and
    polls each.

    Returns a structured summary for the bench log / scheduler
    visibility.
    """
    summary: dict[str, Any] = {
        "accounts_processed": 0,
        "maps_polled": 0,
        "transitions": 0,
        "discrepancies_raised": 0,
        "errors": [],
    }

    accounts = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1},
        fields=[
            "name",
            "ecs_polling_cadence_minutes",
        ],
    )
    for acc in accounts:
        # gh#152 circuit breaker — skip polling for accounts whose
        # inbound side is 5xxing. Prevents amplifying pressure on EE
        # while our own outage plays out. Import inside loop so a
        # missing module (fresh install pre-patch) can't break the
        # scheduler entirely.
        try:
            from ecommerce_super.easyecom.api.gsp_circuit import (
                should_allow_poll,
            )
            if not should_allow_poll(acc["name"]):
                summary.setdefault("circuit_open_skips", []).append(acc["name"])
                summary["accounts_processed"] += 1
                continue
        except Exception:  # noqa: BLE001
            pass  # breaker fault must not itself block polling

        cadence = int(acc.get("ecs_polling_cadence_minutes") or 15)
        eligible = _find_eligible_maps(
            easyecom_account=acc["name"], cadence_minutes=cadence
        )
        for map_name in eligible:
            try:
                outcome = reconcile_one_map(map_name)
                summary["maps_polled"] += 1
                if outcome.get("transitioned"):
                    summary["transitions"] += 1
                if outcome.get("discrepancy_raised"):
                    summary["discrepancies_raised"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(
                    {
                        "map": map_name,
                        "exception": type(exc).__name__,
                        "message": str(exc)[:300],
                    }
                )
                frappe.log_error(
                    title=f"§11 polling failed for {map_name}",
                    message=f"{type(exc).__name__}: {exc}",
                )
        summary["accounts_processed"] += 1

    return summary


def _find_eligible_maps(
    *, easyecom_account: str, cadence_minutes: int
) -> list[str]:
    """Return Map names in pending statuses whose last_polled_at is
    NULL OR <= NOW() - cadence_minutes."""
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabEasyEcom B2B Order Map`
        WHERE easyecom_account = %(account)s
          AND status IN %(statuses)s
          AND (
            last_polled_at IS NULL
            OR last_polled_at <= DATE_SUB(NOW(), INTERVAL %(cadence)s MINUTE)
          )
        """,
        {
            "account": easyecom_account,
            "statuses": tuple(PENDING_STATUSES),
            "cadence": int(cadence_minutes),
        },
        as_dict=True,
    )
    return [r["name"] for r in rows]


# ============================================================
# Per-Map reconciliation.
# ============================================================


def reconcile_one_map(map_name: str) -> dict:
    """Poll one B2B Order Map via getOrderDetails and reconcile its
    state. Returns a structured outcome dict.

    Always stamps `last_polled_at` (success or failure) so a misbehaving
    Map doesn't get re-polled on every single tick.
    """
    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    sales_order = map_doc.sales_order
    ee_account_name = map_doc.easyecom_account

    # Resolve location_key from the SO's set_warehouse — same gate as
    # the push side. If the warehouse is no longer EE-mapped (rare
    # but possible during reconfiguration), skip + log.
    so_warehouse = frappe.db.get_value(
        "Sales Order", sales_order, "set_warehouse"
    )
    ee_location = (
        get_ee_location_for_warehouse(so_warehouse) if so_warehouse else None
    )
    location_key = (
        str(getattr(ee_location, "location_key", "")) if ee_location else ""
    )
    if not location_key:
        _stamp_last_polled(map_name)
        return {
            "ok": False,
            "transitioned": False,
            "detail": (
                f"Skip: warehouse {so_warehouse!r} no longer maps to a "
                "Live EE Location."
            ),
        }

    correlation_id = new_correlation_id()
    client = EasyEcomClient(location_key=location_key)
    try:
        response = client.get(
            ORDER_DETAILS_GET,
            params={"reference_code": sales_order},
            correlation_id=correlation_id,
        )
    except (EasyEcomAPIError, EasyEcomError) as exc:
        _stamp_last_polled(map_name)
        return {
            "ok": False,
            "transitioned": False,
            "exception": type(exc).__name__,
            "message": str(exc)[:500],
        }

    rows = _extract_rows(response)

    # Backfill OrderID/SuborderID/InvoiceID onto the Map row when
    # they're missing locally but EE has them. Surfaced 2026-06-28
    # via Thuraya New B2B end-to-end smoke (SAL-ORD-2026-00022): a
    # New B2B push returns "Successfully Queued" with no IDs in the
    # response body, so the Map row sits with null ee_order_id /
    # ee_suborder_id / ee_invoice_id until polling fills them in.
    # The §17 worklist card "New B2B orders missing IDs (2h+)" was
    # specifically designed for this gap. Phase 1 polling derivation
    # focused on status transitions (Cancelled / Invoice Pending /
    # partial-cancel) and silently skipped ID backfill — surfaces
    # only on New B2B accounts (Old B2B captures IDs at push time).
    backfilled = _backfill_ee_ids_if_missing(map_doc, rows)

    decision, payload = derive_local_status_from_ee_rows(map_doc, rows)
    outcome = _apply_decision(
        map_doc=map_doc,
        decision=decision,
        payload=payload,
        rows=rows,
        ee_account_name=ee_account_name,
        correlation_id=correlation_id,
    )
    if backfilled:
        outcome["ids_backfilled"] = backfilled

    # §11.6 — Stamp dispatch status on the linked SI (if any). Runs on
    # every poll regardless of decision so Pending → Shipped → Delivered
    # transitions land even on quiet "no_change" ticks where the Map
    # itself doesn't move. Re-reads map_doc because _apply_decision may
    # have just set sales_invoice during a Mode 2 mirror.
    dispatch_outcome = _stamp_dispatch_status_on_si(
        map_name=map_name, rows=rows,
    )
    if dispatch_outcome:
        outcome["dispatch_stamped"] = dispatch_outcome

    _stamp_last_polled(map_name)
    return outcome


def _backfill_ee_ids_if_missing(
    map_doc: Any, rows: list[dict]
) -> dict | None:
    """If the Map row is missing OrderID / SuborderID / InvoiceID but
    the EE response carries them, write them back. Returns a small
    dict naming which fields were backfilled (or None when nothing
    changed) — useful for the FDE trace in the outcome.

    Only acts on businessorder rows. Picks the row with the latest
    `last_update_date` so multi-shipment splits resolve to the most
    recent invoice — but for fresh New B2B pushes there's only one
    row, so this degenerate case is the common path.

    Doesn't transition status — that's the derivation function's job.
    This function only fills empty identifier columns.
    """
    b2b_rows = [
        r for r in rows if r.get("order_type_key") == "businessorder"
    ]
    if not b2b_rows:
        return None

    latest = max(b2b_rows, key=lambda r: r.get("last_update_date") or "")

    updates: dict[str, Any] = {}
    if not map_doc.ee_order_id and latest.get("order_id"):
        updates["ee_order_id"] = str(latest["order_id"])

    # SuborderID lives one level down in order_items[].suborder_id;
    # pick the first suborder's id as the Map's anchor (multi-suborder
    # tracking is shipment-split territory, Phase 2).
    if not map_doc.ee_suborder_id:
        items = latest.get("order_items") or []
        if items:
            sub_id = items[0].get("suborder_id")
            if sub_id:
                updates["ee_suborder_id"] = str(sub_id)

    if not map_doc.ee_invoice_id and latest.get("invoice_id"):
        updates["ee_invoice_id"] = str(latest["invoice_id"])

    if not updates:
        return None

    frappe.db.set_value(
        "EasyEcom B2B Order Map", map_doc.name, updates,
        update_modified=False,
    )
    frappe.db.commit()
    # Refresh the in-memory map_doc so downstream derivation sees the
    # new IDs.
    for field, value in updates.items():
        setattr(map_doc, field, value)
    return updates


def _extract_rows(response: Any) -> list[dict]:
    """getOrderDetails returns `data: [...]` (list of order rows).
    Defensive — accept both list and the {orders: [...]} variant
    just in case EE's response shape diverges between endpoints."""
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        orders = data.get("orders")
        if isinstance(orders, list):
            return [r for r in orders if isinstance(r, dict)]
    return []


# ============================================================
# Locked status derivation function.
# ============================================================


def derive_local_status_from_ee_rows(
    local_map: Any, rows: list[dict]
) -> tuple[str, Any]:
    """Conservative Phase 1 status derivation.

    Handles EE's multi-row response semantic (state-change history
    + shipment splits) and per-suborder partial cancellation via
    cancelled_quantity (FAQ #23).

    Returns one of:
      ("orphan", None)
      ("transition_to", "Cancelled")
      ("transition_to", "Invoice Pending")
      ("partial_cancel", {"total_item_qty", "cancelled_qty",
                          "cancelled_pct"})
      ("no_change", None)
      ("unknown", {"status_id", "latest_row_invoice_id"})
    """
    # Defensive filter: B2B rows only. EE's getOrderDetails returns
    # all order types matching the reference_code; we only act on
    # rows where order_type_key == "businessorder".
    b2b_rows = [
        r for r in rows if r.get("order_type_key") == "businessorder"
    ]
    if not b2b_rows:
        return ("orphan", None)

    # Aggregate item-level quantities across ALL rows (handles
    # shipment splits — same reference_code, multiple invoice rows
    # per fulfillment chunk).
    #
    # GROUNDING CORRECTION (paste 7 live finding on Harmony): EE's
    # getOrderDetails response returns the per-line array under the
    # key `order_items`, NOT `suborders` as the Phase 1 packet
    # assumed. The field rename preserves the same shape
    # (item_quantity / cancelled_quantity per row) — only the parent
    # key changed.
    total_item_qty = 0
    total_cancelled_qty = 0
    for r in b2b_rows:
        for sub in (r.get("order_items") or []):
            iq = sub.get("item_quantity") or 0
            cq = sub.get("cancelled_quantity") or 0
            total_item_qty += iq
            total_cancelled_qty += cq

    # Phase 1 full cancellation: ALL rows show order_status_id=9 AND
    # all quantity cancelled. The qty gate guards against a half-
    # cancelled shipment-split case where the row-level status flips
    # to 9 but an order_item still has uncancelled qty.
    rows_all_cancelled = all(
        r.get("order_status_id") == CANCELLED_STATUS_ID
        for r in b2b_rows
    )
    qty_all_cancelled = (
        total_item_qty > 0 and total_cancelled_qty == total_item_qty
    )

    if rows_all_cancelled and qty_all_cancelled:
        return ("transition_to", "Cancelled")

    # Partial cancellation: some qty cancelled but not all. Phase 1
    # surfaces this as a Discrepancy (credit notes, qty adjustments
    # are Phase 2 territory) — no local transition.
    if 0 < total_cancelled_qty < total_item_qty:
        return (
            "partial_cancel",
            {
                "total_item_qty": total_item_qty,
                "cancelled_qty": total_cancelled_qty,
                "cancelled_pct": round(
                    100 * total_cancelled_qty / total_item_qty, 1
                ),
            },
        )

    # Phase 1 invoice generation: ANY row has invoice_number populated.
    # EE adds a new row when an invoice is generated for a shipment
    # chunk, so the presence of invoice_number on at least one row is
    # the trigger.
    if any(r.get("invoice_number") for r in b2b_rows):
        return ("transition_to", "Invoice Pending")

    # Otherwise: defer to latest row for residual signals.
    latest = max(b2b_rows, key=lambda r: r.get("last_update_date") or "")
    status_id = latest.get("order_status_id")

    if status_id in KNOWN_ACTIVE_STATUS_IDS:
        return ("no_change", None)

    return (
        "unknown",
        {
            "status_id": status_id,
            "latest_row_invoice_id": latest.get("invoice_id"),
        },
    )


# ============================================================
# Decision dispatch — applies the derivation outcome.
# ============================================================


def _apply_decision(
    *,
    map_doc: Any,
    decision: str,
    payload: Any,
    rows: list[dict],
    ee_account_name: str,
    correlation_id: str,
) -> dict:
    """Apply the derivation function's decision: transition the Map
    state, raise a Discrepancy, or both."""
    from ecommerce_super.easyecom.flows.grn_pull import _raise_discrepancy

    company = frappe.db.get_value(
        "Sales Order", map_doc.sales_order, "company"
    )

    if decision == "orphan":
        _raise_discrepancy(
            kind="B2B Map orphaned at EE",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE has no rows for reference_code="
                f"{map_doc.sales_order!r} (EE Account {ee_account_name}, "
                f"correlation_id={correlation_id}). Map exists locally "
                "in status={local_status} but EE does not recognize the "
                "reference_code — likely push failure or EE-side data "
                "loss. FDE action required.".format(
                    local_status=map_doc.status,
                )
            ),
        )
        return {
            "ok": True,
            "transitioned": False,
            "discrepancy_raised": True,
            "decision": "orphan",
        }

    if decision == "transition_to" and payload == "Cancelled":
        frappe.db.set_value(
            "EasyEcom B2B Order Map",
            map_doc.name,
            {
                "status": "Cancelled",
                "cancelled_at": now_datetime(),
            },
            update_modified=False,
        )
        _raise_discrepancy(
            kind="B2B order cancelled by EE — polling-detected",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE reports order_status_id=9 (Cancelled) "
                f"with all quantities cancelled for reference_code="
                f"{map_doc.sales_order!r} (EE Account {ee_account_name}, "
                f"correlation_id={correlation_id}). Local Map transitioned "
                "to Cancelled. The Cancel Order webhook receiver lands in "
                "Phase 2; until then, polling is the recovery path."
            ),
        )
        frappe.db.commit()
        return {
            "ok": True,
            "transitioned": True,
            "discrepancy_raised": True,
            "decision": "cancelled",
        }

    if decision == "transition_to" and payload == "Invoice Pending":
        # §11.5.2 Mode 2 — EE generated the invoice on its own side.
        # Mirror it to an ERPNext Sales Invoice (Draft) and link via
        # Map.sales_invoice. The variance check (0.01% threshold —
        # see invoice_mirror.VARIANCE_THRESHOLD_PCT) raises a
        # Discrepancy if our SI total diverges from EE's total.
        # Capture EE's invoice_number on the Map too.
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            InvoiceMirrorError,
            InvoiceMirrorVariance,
            mirror_si_from_ee_response,
        )

        # Pick the row that carries the invoice (latest with
        # invoice_number populated; multi-shipment splits would
        # produce multiple invoices over time — Phase 1 mirrors
        # the latest seen on each polling tick).
        b2b_rows = [r for r in rows if r.get("order_type_key") == "businessorder"]
        invoice_row = max(
            (r for r in b2b_rows if r.get("invoice_number")),
            key=lambda r: r.get("last_update_date") or "",
            default=None,
        )

        updates: dict[str, Any] = {"status": "Invoice Pending"}
        if invoice_row:
            ee_invoice_number = (
                invoice_row.get("invoice_number") or ""
            ).strip()
            if ee_invoice_number and not map_doc.get("ee_invoice_number"):
                updates["ee_invoice_number"] = ee_invoice_number

        # Mirror SI iff we haven't already (idempotent via Map.sales_invoice).
        mirror_outcome: dict | None = None
        variance_warning: str | None = None
        if invoice_row and not map_doc.get("sales_invoice"):
            try:
                mirror_outcome = mirror_si_from_ee_response(
                    map_doc=map_doc, ee_row=invoice_row,
                )
                updates["sales_invoice"] = mirror_outcome["sales_invoice"]
                updates["sales_invoice_mirrored_at"] = now_datetime()
                updates["status"] = "Invoice Generated"
            except InvoiceMirrorVariance as exc:
                # SI WAS created — it's in Draft. Variance exceeded
                # VARIANCE_THRESHOLD_PCT (0.01%); raise Discrepancy
                # for FDE review but persist the link.
                # Re-derive the SI name from the exception's inner
                # state: the mirror function already wrote the SI before
                # the raise. Pull from the Map's existing sales_invoice
                # link if it got set (transactional) — else fall back
                # to a Sales Invoice lookup by invoice_id.
                if not map_doc.get("sales_invoice"):
                    si_name = frappe.db.get_value(
                        "Sales Invoice",
                        {"ecs_easyecom_invoice_id": str(invoice_row.get("invoice_id"))},
                        "name",
                    )
                    if si_name:
                        updates["sales_invoice"] = si_name
                        updates["sales_invoice_mirrored_at"] = now_datetime()
                updates["status"] = "Invoice Generated"
                variance_warning = str(exc)
            except InvoiceMirrorError as exc:
                # Mirror failed before SI was inserted (missing Customer
                # Map, missing Item Map, etc.). Record on Map.last_error
                # + raise Discrepancy. Status stays Invoice Pending so
                # the next polling tick retries.
                updates["last_error"] = str(exc)[:5000]
                _raise_discrepancy(
                    kind="B2B Mode 2 SI mirror failed — missing prerequisite",
                    reference_doctype="EasyEcom B2B Order Map",
                    reference_name=map_doc.name,
                    company=company or "",
                    reason=(
                        f"§11.5.2 Mode 2 SI mirror failed for "
                        f"reference_code={map_doc.sales_order!r} "
                        f"(EE Account {ee_account_name}, correlation_id="
                        f"{correlation_id}). EE invoice_number="
                        f"{invoice_row.get('invoice_number')!r}, "
                        f"invoice_id={invoice_row.get('invoice_id')!r}. "
                        f"Error: {exc!s}. Resolve the missing prerequisite "
                        "(Customer Map, Item Map, etc.) — next polling tick "
                        "retries automatically."
                    ),
                )

        frappe.db.set_value(
            "EasyEcom B2B Order Map", map_doc.name, updates,
            update_modified=False,
        )

        if variance_warning:
            _raise_discrepancy(
                kind="B2B Mode 2 SI mirror — variance exceeds threshold",
                reference_doctype="EasyEcom B2B Order Map",
                reference_name=map_doc.name,
                company=company or "",
                reason=(
                    f"§11.5.2 Mode 2 SI mirror created Draft SI for "
                    f"reference_code={map_doc.sales_order!r} but the "
                    f"computed totals diverge beyond "
                    f"VARIANCE_THRESHOLD_PCT (0.01%). "
                    f"{variance_warning} "
                    "FDE: SO built the SI natively via ERPNext's "
                    "make_sales_invoice; EE's total disagrees. Review "
                    "the SO's line rates + tax template vs EE's "
                    "invoice payload, then either amend or accept "
                    "the Draft SI."
                ),
            )

        frappe.db.commit()
        return {
            "ok": True,
            "transitioned": True,
            "discrepancy_raised": bool(variance_warning) or (
                mirror_outcome is None and invoice_row is not None
            ),
            "decision": "invoice_pending",
            "mirror_outcome": mirror_outcome,
            "variance_warning": variance_warning,
        }

    if decision == "partial_cancel":
        _raise_discrepancy(
            kind="B2B order partial cancellation detected",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE reports partial cancellation on "
                f"reference_code={map_doc.sales_order!r} "
                f"(EE Account {ee_account_name}, correlation_id="
                f"{correlation_id}). total_item_qty="
                f"{payload['total_item_qty']}, cancelled_qty="
                f"{payload['cancelled_qty']} ({payload['cancelled_pct']}%). "
                "No local Map transition — credit notes / qty "
                "adjustments are Phase 2 territory. FDE action required."
            ),
        )
        frappe.db.commit()
        return {
            "ok": True,
            "transitioned": False,
            "discrepancy_raised": True,
            "decision": "partial_cancel",
            "qty_payload": payload,
        }

    if decision == "unknown":
        # Pickle the entire payload to the reason for forensic visibility.
        try:
            payload_json = json.dumps(rows, default=str)[:4000]
        except Exception:
            payload_json = "(payload not JSON-serialisable)"
        _raise_discrepancy(
            kind="B2B unknown order_status_id",
            reference_doctype="EasyEcom B2B Order Map",
            reference_name=map_doc.name,
            company=company or "",
            reason=(
                f"§11 polling: EE returned an order_status_id "
                f"{payload['status_id']!r} that's outside the known "
                f"enum {{1,2,3,4,5,6,7,9,30}} for reference_code="
                f"{map_doc.sales_order!r} (EE Account {ee_account_name}, "
                f"correlation_id={correlation_id}). latest_row "
                f"invoice_id={payload['latest_row_invoice_id']}. "
                f"No local transition — FDE action required to "
                f"interpret the unknown status. Full payload: "
                f"{payload_json}"
            ),
        )
        return {
            "ok": True,
            "transitioned": False,
            "discrepancy_raised": True,
            "decision": "unknown",
        }

    # decision == "no_change" — quiet path, just refresh last_polled_at.
    return {
        "ok": True,
        "transitioned": False,
        "discrepancy_raised": False,
        "decision": "no_change",
    }


def _stamp_dispatch_status_on_si(
    *,
    map_name: str,
    rows: list[dict],
) -> dict | None:
    """§11.6 — Mirror EE-side fulfilment status onto the linked Sales
    Invoice's ecs_easyecom_* dispatch fields.

    Reads the latest businessorder row's `order_status_id`, maps it via
    `DISPATCH_STATUS_BY_ID`, and stamps:
      - ecs_easyecom_dispatch_status (always, when status_id is known)
      - ecs_easyecom_dispatched_at   (first time we see status 5)
      - ecs_easyecom_delivered_at    (first time we see status 6)
      - ecs_easyecom_tracking_url    (whenever EE provides one)

    Returns a small dict describing what changed (or None when nothing
    changed / nothing to stamp). update_modified=False on the write so
    the SI's modified timestamp isn't churned on every */5 tick.

    Idempotent — re-running on the same payload writes the same values.
    Never raises; failures are silent (the polling tick must not be
    broken by an SI without the §11.6 fields, e.g. fresh installs that
    haven't migrated yet).
    """
    try:
        map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    except Exception:
        return None

    si_name = map_doc.get("sales_invoice")
    if not si_name:
        return None  # No SI linked yet (Mode 2 pre-mirror or Mode 1 pre-call)

    b2b_rows = [r for r in rows if r.get("order_type_key") == "businessorder"]
    if not b2b_rows:
        return None

    latest = max(b2b_rows, key=lambda r: r.get("last_update_date") or "")
    status_id = latest.get("order_status_id")
    if status_id not in DISPATCH_STATUS_BY_ID:
        return None  # Unknown status — let the derivation function handle it

    new_status = DISPATCH_STATUS_BY_ID[status_id]
    tracking_url = ""
    for key in TRACKING_URL_CANDIDATE_KEYS:
        candidate = latest.get(key)
        if candidate:
            tracking_url = str(candidate).strip()
            break

    try:
        current = frappe.db.get_value(
            "Sales Invoice", si_name,
            [
                "ecs_easyecom_dispatch_status",
                "ecs_easyecom_dispatched_at",
                "ecs_easyecom_delivered_at",
                "ecs_easyecom_tracking_url",
            ],
            as_dict=True,
        ) or {}
    except Exception:
        # Custom fields not yet migrated; bail silently.
        return None

    updates: dict[str, Any] = {}
    if current.get("ecs_easyecom_dispatch_status") != new_status:
        updates["ecs_easyecom_dispatch_status"] = new_status

    # dispatched_at: first time we observe Shipped (or Delivered, since
    # Delivered implies dispatch happened — backfill if we missed Shipped).
    if status_id in (5, 6) and not current.get("ecs_easyecom_dispatched_at"):
        updates["ecs_easyecom_dispatched_at"] = now_datetime()

    # delivered_at: first time we observe Delivered.
    if status_id == 6 and not current.get("ecs_easyecom_delivered_at"):
        updates["ecs_easyecom_delivered_at"] = now_datetime()

    # tracking URL: overwrite when EE supplies one (couriers can change
    # mid-flight; trust EE as the source of truth).
    if tracking_url and current.get("ecs_easyecom_tracking_url") != tracking_url:
        updates["ecs_easyecom_tracking_url"] = tracking_url

    if not updates:
        return None

    try:
        frappe.db.set_value(
            "Sales Invoice", si_name, updates,
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        return None

    return {
        "sales_invoice": si_name,
        "status_id": status_id,
        "new_status": new_status,
        "fields_written": list(updates.keys()),
    }


def _stamp_last_polled(map_name: str) -> None:
    """Touch last_polled_at — fires on every poll, success or fail.
    Without this, a misbehaving Map (HTTP errors, orphan, etc.) would
    re-poll on every single 5-min tick, hammering EE quota."""
    try:
        frappe.db.set_value(
            "EasyEcom B2B Order Map",
            map_name,
            "last_polled_at",
            now_datetime(),
            update_modified=False,
        )
        frappe.db.commit()
    except Exception:
        # Don't let last_polled_at failures block reconciliation.
        pass
