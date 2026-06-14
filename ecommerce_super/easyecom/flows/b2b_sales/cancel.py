"""Stage 2 substrate — ERPNext-initiated cancellation for §11.

This module is intentionally empty in Stage 1. Stage 2 populates:
  - cancel_b2b_order_from_erpnext(sales_order): whitelisted endpoint
    that posts to EE's /orders/cancelOrder with the SO name as
    reference_code, updates the Map row to Cancelled on success.

EE cancellation endpoint: POST {{BaseURL}}/orders/cancelOrder.
Payload: {"reference_code": "<SO name>"}.
Headers: x-api-key + Authorization: Bearer <Jwt>.
Identifier: reference_code = SO name = orderNumber at createOrder
(works uniformly for Old B2B and New B2B).
"""
