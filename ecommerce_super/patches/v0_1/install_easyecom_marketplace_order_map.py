"""Install / refresh EasyEcom Marketplace Order Map + child DocType.

RESTORES the DocType dropped in PR #107 (SPEC_12 §5). See the
`docs/patch_notes/spec_12_amendment_restore_mp_order_map.md`
methodology amendment for the full rationale — TL;DR:

  1. The Aug 2026 Puresta reconciliation exercise falsified SPEC_12's
     "marketplace order → SI is 1:1 via invoice_id dedup" premise.
     Real data: 6,001 distinct marketplace refs → 6,048 EE invoices →
     47 split-shipment orders / month. Recon at order grain
     (RECON_SPEC §6.2) requires a single doc listing all shipment SIs.

  2. Recon writeback fields (settlement_status, actual_net, variance_*)
     can no longer live on Sales Invoice — the row-size budget was
     consumed by earlier PRs. Ecommerce_super_recon needs a home for
     these that isn't SI. Marketplace Order Map + Order Map Shipment
     child = zero new columns on tabSales Invoice.

WHAT THIS PATCH DOES

  Idempotent reload of both DocTypes via frappe.reload_doc. Safe on:
    - Fresh sites (creates the DocTypes from JSON)
    - Sites that had the old (retired) DocType — the empty folder from
      PR #107 will be replaced with the new schema
    - Re-runs (reload_doc is idempotent)

  No data migration — this patch establishes the empty tables. Real
  data population happens in PR B (invoice_builder integration) and
  PR F (retroactive backfill for existing SIs).
"""
from __future__ import annotations

import frappe


def execute() -> None:
    # Reload both DocTypes so they materialise on existing sites even
    # when Frappe's auto-detection missed them. `force=True` re-syncs
    # the JSON schema on every migrate — safe because both DocTypes
    # are new / previously-empty (no data to preserve).
    frappe.reload_doc(
        "easyecom",
        "doctype",
        "easyecom_marketplace_order_map",
        force=True,
    )
    frappe.reload_doc(
        "easyecom",
        "doctype",
        "easyecom_order_map_shipment",
        force=True,
    )
    frappe.clear_cache(doctype="EasyEcom Marketplace Order Map")
    frappe.clear_cache(doctype="EasyEcom Order Map Shipment")
    print(
        "[install_easyecom_marketplace_order_map] Installed / refreshed "
        "EasyEcom Marketplace Order Map + child DocType. Zero new "
        "columns on tabSales Invoice."
    )
