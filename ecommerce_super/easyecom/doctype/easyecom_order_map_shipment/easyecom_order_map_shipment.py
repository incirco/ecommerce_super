"""EasyEcom Order Map Shipment — child of EasyEcom Marketplace Order Map.

One row per Sales Invoice mirroring an EE shipment. Split-shipment
orders (one marketplace_order_id → many EE invoice_ids → many SIs)
carry many child rows on the parent Map.

No validation logic here — the caller (b2c invoice_builder /
credit_note builder) is responsible for populating fields consistently.
Trusting the caller keeps the child cheap on inserts and matches the
minimal-child pattern established by EasyEcom Address (PR #266).
"""
from __future__ import annotations

from frappe.model.document import Document


class EasyEcomOrderMapShipment(Document):
    pass
