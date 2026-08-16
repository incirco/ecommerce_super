"""EasyEcom Marketplace Order Map — order-grain bridge between marketplace
orders and the ERPNext Sales Invoices they produce.

WHY THIS DOCTYPE EXISTS (2026-08-17)

  Restored per methodology decision after the Aug 2026 Puresta
  reconciliation exercise (see docs/patch_notes/spec_12_amendment_
  restore_mp_order_map.md). Previously dropped in PR #107 based on the
  premise that marketplace-order → SI is 1:1 via invoice_id dedup.
  That premise failed against production data: 6,001 distinct marketplace
  refs → 6,048 EE invoices = 47 split-shipment orders per month at
  Puresta. Reconciliation at order grain (RECON_SPEC §6.2) requires a
  single doc that lists all shipment SIs for a marketplace order.

  Additionally, adding recon writeback fields (ecs_actual_net,
  ecs_variance_*, ecs_settlement_status) directly to tabSales Invoice
  is no longer possible — the row-size budget was consumed by earlier
  PRs (address fields moved to child in PR #266, still tight). This
  DocType hosts those fields at order grain, off the SI table.

WHERE THE EE METADATA LIVES

  Per-shipment EE fields (invoice_id, invoice_number, manifest_date,
  batch_id, invoice_currency_code, sales_channel, payment_gateway_*,
  IRN cross-ref, e-way bill cross-ref, etc.) live on the child
  EasyEcom Order Map Shipment — one row per SI, so split-shipment
  orders naturally carry many child rows. tabSales Invoice picks up
  ZERO new columns from this DocType.

INTERFACE CONTRACT WITH RECON APP (ecommerce_super_recon)

  Ecommerce_super writes: everything except the recon-writeback fields.
  Recon writes: settlement_status (transitions from Forecast), actual_net,
  variance_amount, variance_pct, settlement_completed_at.
  No other writes are sanctioned in either direction — dependency stays
  one-way per RECON_SPEC §2.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EasyEcomMarketplaceOrderMap(Document):
    """Order-grain bridge; see module docstring for design rationale."""

    def validate(self) -> None:
        self._enforce_composite_uniqueness()
        self._recompute_total_shipments()

    def _enforce_composite_uniqueness(self) -> None:
        """A marketplace order id is only unique within (marketplace, company).
        The same order id can legitimately reappear on another Marketplace
        or another Company (multi-tenant EE setups). Enforce the composite
        key in Python because Frappe's `unique=1` only supports single-column.
        Skip validation on load — only fire on new/renamed rows."""
        if self.is_new() is False and self.get_doc_before_save() and (
            self.marketplace == (self.get_doc_before_save().marketplace)
            and self.marketplace_order_id
            == (self.get_doc_before_save().marketplace_order_id)
            and self.company == (self.get_doc_before_save().company)
        ):
            return
        existing = frappe.db.get_value(
            "EasyEcom Marketplace Order Map",
            {
                "marketplace": self.marketplace,
                "marketplace_order_id": self.marketplace_order_id,
                "company": self.company,
                "name": ("!=", self.name or ""),
            },
            "name",
        )
        if existing:
            frappe.throw(
                _(
                    "Marketplace Order Map already exists for ({0}, {1}, {2}): {3}. "
                    "Use the existing Map to append shipment rows rather than "
                    "creating a new one."
                ).format(
                    self.marketplace,
                    self.marketplace_order_id,
                    self.company,
                    existing,
                ),
                title=_("Duplicate Marketplace Order Map"),
            )

    def _recompute_total_shipments(self) -> None:
        """Denormalised count — cheap on write, saves an aggregate on read
        (list view + Net Receivables grouping). Truth source is the child
        table itself; this field is convenience only."""
        self.total_shipments = len(self.shipments or [])
