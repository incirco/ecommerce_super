"""§12 — EasyEcom Marketplace Order Map controller.

The recon-engine join target for B2C marketplace SIs. One Map row per
EE shipment (Invoice ID), created at SI insert time by the §12
SI builder. Settlement Lines (arriving days/weeks later via
the marketplace's reconciliation feeds) join here via
(marketplace, ecs_marketplace_order_id) → Map → Sales Invoice.

Phase 1 of §12 only writes Maps; the recon engine consumes them.
"""
from __future__ import annotations

from frappe.model.document import Document


class EasyEcomMarketplaceOrderMap(Document):
    pass
