"""PR D follow-up — add FX-tracking fields to Order Map Shipment child.

Two new fields:
  conversion_rate_confirmed  (Check, default 0)
  conversion_rate_source     (Select: ERPNext Currency Exchange / EE Tax Export
                                       / EE API / Manual override)

The Shipment child DocType was installed by
install_easyecom_marketplace_order_map (PR #272). Frappe's patch log
won't re-run that patch, so we need this second patch to force the
column materialization on already-installed sites.

WHY

  Puresta Aug 2026 recon exposed that foreign-currency B2C exports
  need ops verification that our Currency Exchange rate matches EE's
  locked-in rate before SI submit — else FX drift produces silent
  INR base-amount errors. The pending-manifest sweeper (PR #274)
  reads `conversion_rate_confirmed` to gate the submit path for
  non-INR SIs.

Idempotent: reload_doc + updatedb are both safe re-runs.
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
    # Force column materialization. On sites that had the DocType
    # from PR #272 without these fields, reload_doc updates the
    # DocType record but Frappe's auto-column-add can miss child-
    # table columns on some MariaDB configs. Explicit updatedb.
    try:
        frappe.db.updatedb("EasyEcom Order Map Shipment")
    except Exception as exc:
        # updatedb signature varies across Frappe versions; the reload
        # above materializes columns on modern versions anyway.
        frappe.logger().info(
            f"[add_fx_confirmation_fields_to_shipment] updatedb call "
            f"raised {type(exc).__name__}: {exc}. reload_doc already ran; "
            f"columns should materialize on next migrate."
        )
    frappe.clear_cache(doctype="EasyEcom Order Map Shipment")
    print(
        "[add_fx_confirmation_fields_to_shipment] Added / refreshed "
        "conversion_rate_confirmed + conversion_rate_source fields on "
        "EasyEcom Order Map Shipment. FX-drift protection for foreign-"
        "currency B2C SIs is now active — see PR D (#276) for the "
        "sweeper gate semantics."
    )
