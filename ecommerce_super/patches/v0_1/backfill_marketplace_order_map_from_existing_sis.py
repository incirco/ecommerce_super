"""Backfill Marketplace Order Map + Shipment child rows for existing
B2C Sales Invoices that predate PR #273.

CONTEXT

  PR #272 restored the EasyEcom Marketplace Order Map DocType.
  PR #273 wired invoice_builder to populate it on every new SI.
  This patch closes the gap: SIs that already exist in prod (created
  under the flat charge_type="Actual" / no-Map regime) don't have
  Map + Shipment child rows yet, so recon can't join to them at the
  order grain (RECON_SPEC §6.2).

  Without this backfill:
    - Recon's Settlement Line joins on marketplace_order_id →
      Marketplace Order Map find no Map row for pre-PR-#273 SIs →
      settlement entries fall out as "unmatched"
    - Split-shipment orders (one marketplace_order_id → many SIs)
      can't be grouped for order-grain reconciliation

  With this backfill:
    - Every existing SI with ecs_marketplace_order_id populated gets
      a matching Marketplace Order Map + one Shipment child row
    - Split-shipment orders converge on the same Map (thanks to the
      composite-uniqueness controller from PR #272)
    - Every metadata field we can populate from the SI itself is
      captured on the Shipment child (invoice_id, invoice_number,
      awb_number, currency, conversion_rate)
    - Missing fields (manifest_date, batch_id, sales_channel, IRN,
      etc.) stay blank — a future patch can enrich from Sync Record's
      last_response_payload if needed

IDEMPOTENCY

  Per-SI: skip if a Shipment child row already exists with
  sales_invoice = this SI's name. Safe to re-run.

  Per-Map: composite-uniqueness on (marketplace, marketplace_order_id,
  company) — split-shipment SIs share a Map (second SI finds the Map
  created by the first, appends a Shipment row).

FAILURE ISOLATION

  Per-SI try/except. One bad SI never fails the whole batch. Errors
  logged and counted; patch completes with summary counts.

RUN CONTROL

  Registered in patches.txt — runs once via `bench migrate`, then
  Frappe's Patch Log records completion and skips on future migrates.

  Also safe to invoke manually via:
    bench --site <site> execute \\
      ecommerce_super.patches.v0_1.backfill_marketplace_order_map_from_existing_sis.execute

  Manual invocation is idempotent (per-SI Shipment-exists check).
"""
from __future__ import annotations

from typing import Any

import frappe


# Currencies that are always safe to auto-confirm — INR only. Any other
# currency requires ops confirmation per PR #276's FX gate design.
_AUTO_CONFIRM_CURRENCIES = {"INR"}


def execute() -> None:
    counts = {
        "total_sis": 0,
        "created_maps": 0,
        "created_shipments": 0,
        "skipped_already_backfilled": 0,
        "skipped_no_reference_code": 0,
        "skipped_no_marketplace_account": 0,
        "errors": 0,
    }

    for si_data in _iter_backfillable_sis():
        counts["total_sis"] += 1
        try:
            outcome = _process_one_si(si_data)
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as exc:
            counts["errors"] += 1
            frappe.logger().warning(
                f"[backfill_mp_order_map] SI {si_data.get('name')} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    frappe.db.commit()
    print(f"[backfill_mp_order_map] Summary: {counts}")


# ============================================================
# Query — find candidates
# ============================================================


def _iter_backfillable_sis():
    """Yield SI rows with ecs_marketplace_order_id populated. Includes
    both Draft and Submitted SIs — Draft SIs need the Map too so the
    pending-manifest sweeper can find them (PR #274).

    Batched via frappe.db.sql because the Puresta prod set is likely
    thousands of rows. as_dict=True for the small field list keeps
    Python-side memory bounded."""
    # is_return = 0 excludes Credit Notes / Sales Returns.
    #
    # CNs share the tabSales Invoice table with SIs but are the REVERSAL
    # side of the pair. The backfill must not create Shipment rows for
    # them because:
    #
    #   1. ecs_easyecom_invoice_id on CNs is stored as "CN-<original>"
    #      (namespace prefix from _build_credit_note_for_reversed_order
    #      in invoice_builder.py). The sweeper's per-invoice re-check
    #      does int(ee_invoice_id) → ValueError on "CN-...", caught,
    #      returned as still_pending forever.
    #
    #   2. Live-flow CNs already get a Shipment row inserted via
    #      _upsert_marketplace_order_map at CN creation time with
    #      original_sales_invoice back-reference — that's the correct
    #      row. Retro-creating another row here would duplicate.
    #
    #   3. Draft CNs from prior backfill_mode runs need their original
    #      SI submitted first before CN submit can succeed — a Shipment
    #      pointing sweeper at the CN causes indefinite retries.
    return frappe.db.sql(
        """
        SELECT
            name,
            company,
            currency,
            conversion_rate,
            ecs_marketplace,
            ecs_marketplace_order_id,
            ecs_easyecom_invoice_id,
            ecs_easyecom_invoice_number,
            ecs_awb_number,
            ecs_courier,
            ecs_payment_mode
        FROM `tabSales Invoice`
        WHERE ecs_marketplace_order_id IS NOT NULL
          AND ecs_marketplace_order_id != ''
          AND is_return = 0
        """,
        as_dict=True,
    )


# ============================================================
# Per-SI processing
# ============================================================


def _process_one_si(si_data: dict) -> str:
    """For one SI, ensure a Marketplace Order Map exists and has a
    Shipment child row for this SI. Returns one of the counts keys."""
    si_name = si_data["name"]
    marketplace_order_id = (si_data.get("ecs_marketplace_order_id") or "").strip()
    marketplace = (si_data.get("ecs_marketplace") or "").strip()
    company = si_data["company"]

    if not marketplace_order_id:
        return "skipped_no_reference_code"

    # Resolve Marketplace Account — required for the Map.
    marketplace_account = _resolve_marketplace_account(
        marketplace=marketplace,
        company=company,
    )
    if not marketplace_account:
        frappe.logger().warning(
            f"[backfill_mp_order_map] SI {si_name}: no Marketplace Account "
            f"for (marketplace={marketplace!r}, company={company!r}). "
            f"Skipping — Map creation requires a valid MA link."
        )
        return "skipped_no_marketplace_account"

    # Idempotency: skip if a Shipment child already exists for this SI.
    # Cheap direct lookup by unique sales_invoice on the child.
    if _shipment_exists_for_si(si_name):
        return "skipped_already_backfilled"

    # Look up or create the Map (parent). Composite uniqueness ensures
    # split-shipment SIs converge on the same Map.
    map_created, mp_map = _get_or_create_map(
        marketplace_order_id=marketplace_order_id,
        marketplace=marketplace,
        marketplace_account=marketplace_account,
        company=company,
        ee_order_id=None,  # SI didn't carry this — leave blank
    )

    # Append the Shipment child row.
    mp_map.append("shipments", _build_shipment_from_si(si_data))
    mp_map.save(ignore_permissions=True)

    return "created_maps" if map_created else "created_shipments"


def _resolve_marketplace_account(*, marketplace: str, company: str) -> str | None:
    """SI custom fields don't include ecs_marketplace_account on older
    rows (added later in the schema evolution). Derive by unique
    (marketplace, company) lookup. If multiple candidates, warn and
    pick the first — ops can correct via manual edit on the Map."""
    if not marketplace:
        return None
    candidates = frappe.db.get_all(
        "EasyEcom Marketplace Account",
        filters={"marketplace": marketplace, "company": company},
        fields=["name"],
        limit_page_length=5,
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        frappe.logger().warning(
            f"[backfill_mp_order_map] multiple Marketplace Accounts for "
            f"(marketplace={marketplace!r}, company={company!r}): "
            f"{[c['name'] for c in candidates]}. Picking first. Ops can "
            f"correct via manual edit on the resulting Map."
        )
    return candidates[0]["name"]


def _shipment_exists_for_si(si_name: str) -> bool:
    """Idempotency guard — a Shipment row already exists for this SI."""
    return bool(frappe.db.exists(
        "EasyEcom Order Map Shipment", {"sales_invoice": si_name}
    ))


def _get_or_create_map(
    *,
    marketplace_order_id: str,
    marketplace: str,
    marketplace_account: str,
    company: str,
    ee_order_id: str | None,
) -> tuple[bool, Any]:
    """Returns (was_created, doc). Handles the split-shipment case by
    looking up on the composite key first."""
    existing_name = frappe.db.get_value(
        "EasyEcom Marketplace Order Map",
        {
            "marketplace": marketplace,
            "marketplace_order_id": marketplace_order_id,
            "company": company,
        },
        "name",
    )
    if existing_name:
        return (False, frappe.get_doc("EasyEcom Marketplace Order Map", existing_name))

    new_map = frappe.get_doc({
        "doctype": "EasyEcom Marketplace Order Map",
        "marketplace_order_id": marketplace_order_id,
        "marketplace": marketplace,
        "marketplace_account": marketplace_account,
        "company": company,
        "easyecom_order_id": ee_order_id or None,
        # settlement_status auto-defaults to "Forecast" from JSON default
    })
    new_map.insert(ignore_permissions=True)
    return (True, new_map)


def _build_shipment_from_si(si_data: dict) -> dict:
    """Build a minimal Shipment child dict from an SI's own fields.
    Only populates what we can derive from the SI — manifest metadata,
    IRN, e-way bill, etc. stay blank because they weren't captured on
    the SI's schema. A future patch could enrich from Sync Record's
    last_response_payload if needed.

    Currency handling: for INR SIs, auto-confirm the conversion rate
    (no drift possible). For non-INR SIs (extremely rare in the pre-
    PR-#276 era; usually a legacy foreign-currency SI that was already
    submitted at whatever rate), leave confirmed=0 so the sweeper's
    FX gate catches any Draft ones and flags them for ops review."""
    currency = (si_data.get("currency") or "INR").strip().upper()
    conversion_rate = float(si_data.get("conversion_rate") or 1)

    return {
        "doctype": "EasyEcom Order Map Shipment",
        "sales_invoice": si_data["name"],
        # ecs_easyecom_event_type intentionally blank — we don't know
        # the event type without EE's current order_status. Recon can
        # derive it later; leaving blank avoids setting a wrong value.
        "ecs_easyecom_event_type": None,
        "original_sales_invoice": None,
        # EE identifiers from SI fields
        "invoice_id": (
            str(si_data.get("ecs_easyecom_invoice_id"))
            if si_data.get("ecs_easyecom_invoice_id") else None
        ),
        "invoice_number": si_data.get("ecs_easyecom_invoice_number") or None,
        "easyecom_invoice_pdf_url": None,
        # Manifest / dispatch — awb + courier we have; manifest_date etc. we don't
        "manifest_date": None,
        "manifest_no": None,
        "batch_id": None,
        "batch_created_at": None,
        "sales_channel": None,
        "awb_number": si_data.get("ecs_awb_number") or None,
        "courier": si_data.get("ecs_courier") or None,
        # Currency
        "invoice_currency_code": currency,
        "conversion_rate": conversion_rate,
        "conversion_rate_confirmed": (
            1 if currency in _AUTO_CONFIRM_CURRENCIES else 0
        ),
        "conversion_rate_source": "Backfill from existing SI",
        "total_amount_native": 0.0,  # not tracked pre-PR #276
        "total_tax_native": 0.0,
        # Payment
        "payment_gateway_name": None,
        "payment_gateway_transaction_number": None,
        # Compliance — blank; enrich later from Sync Record if needed
        "irn": None,
        "ack_no": None,
        "ack_date": None,
        "eway_bill_number": None,
        "eway_bill_date": None,
    }
