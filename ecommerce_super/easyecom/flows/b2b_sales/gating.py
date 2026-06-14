"""Stage 2 substrate — gating + precondition validation for §11.

This module is intentionally empty in Stage 1. Stage 2 populates:
  - is_section_11_gated(so): Gate 0 check via §8a Warehouse mapping.
  - validate_preconditions(so, ee_account): the nine refusals from
    the §11 packet's §11.2 table, each throwing with the exact
    documented title + message.

Stage 1 ships the substrate (DocType, Custom Fields, payload
builders, helpers, endpoint constants) but does NOT wire any hook
or call any EE endpoint.
"""
