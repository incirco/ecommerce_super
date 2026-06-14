"""Stage 3 substrate — Get All Orders polling reconciliation for §11.

This module is intentionally empty in Stage 1. Stage 3 populates:
  - reconcile_all_pending_b2b_orders(): scheduled entry point.
  - Per-row reconciliation for New B2B identifier correlation,
    EE-side cancellation detection, EE-side invoice generation
    detection.

Endpoint: GET /orders/V2/getAllOrders (constant ORDERS_GET_ALL,
shared across §11/§12).
"""
