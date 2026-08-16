"""Hotfix — add 'Backfill from existing SI' to
`EasyEcom Order Map Shipment.conversion_rate_source` Select options.

CONTEXT

  PR #278 (backfill patch) sets `conversion_rate_source =
  "Backfill from existing SI"` on every Shipment child it creates.
  But that value wasn't in the Select options shipped by PR #276
  (only 4 options: ERPNext Currency Exchange / EE Tax Export
  (manual) / EE API (native) / Manual override).

  Frappe's Select-field validation rejects on `mp_map.save()` —
  which the per-SI try/except in the backfill catches, causing
  ZERO Shipment rows to be created on prod (all counted as
  "errors").

  Unit tests didn't catch it because they assert on the dict
  shape and never actually call `.save()` (mock objects).

FIX

  Add the missing option to the Select in the DocType JSON, then
  `reload_doc` to update the DocType record on already-migrated
  sites so the new option is recognised. Frappe's normal
  `bench migrate` picks up JSON changes to DocTypes without a
  patch, but adding this patch guarantees the reload happens
  even in edge cases (partial migrates, cache staleness).

  Also clear the doctype cache so `frappe.get_meta` returns the
  fresh options list.

IDEMPOTENT — reload_doc is safe to re-run.
"""
from __future__ import annotations

import frappe


def execute() -> None:
    frappe.reload_doc(
        "easyecom",
        "doctype",
        "easyecom_order_map_shipment",
        force=True,
    )
    frappe.clear_cache(doctype="EasyEcom Order Map Shipment")
    print(
        "[add_backfill_option_to_shipment_conversion_rate_source] "
        "Refreshed EasyEcom Order Map Shipment DocType — "
        "'Backfill from existing SI' now valid for "
        "conversion_rate_source Select."
    )
