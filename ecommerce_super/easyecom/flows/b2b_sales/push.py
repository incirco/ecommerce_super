"""Stage 2 substrate — Sales Order push dispatcher + response handlers.

This module is intentionally empty in Stage 1. Stage 2 populates:
  - validate_pre_push(so): hook handler for SO validate.
  - on_submit_push(so): hook handler for SO on_submit (Gate 0 +
    enqueue).
  - push_b2b_order_async(...): queue job entry that calls EE and
    persists the response into a B2B Order Map row.

Naming intent: the reserved slot in hooks.py (currently commented at
lines 334-337) names these handlers `validate_pre_push` and
`on_submit_push`. Stage 2 will uncomment that slot and wire these
handlers in directly.
"""
