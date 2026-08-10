"""Neutralised — schema work moved to
`move_ecs_ee_address_fields_to_child_table` (#266).

WHY THIS FILE STILL EXISTS

  The original version of this patch (#259) added 13 flat
  ecs_ee_billing_* / ecs_ee_shipping_* varchar Custom Fields to
  Sales Invoice. On fat benches (Puresta prod, Puresta UAT after
  many app installs) that ALTER TABLE overflowed MariaDB's 65535-
  byte in-row limit and the migration halted.

  The permanent fix (#266) re-homes those 13 fields on a new
  child DocType (EasyEcom Address, istable=1) attached to SI via
  a Table Custom Field — zero DB columns on tabSales Invoice.

  Because Frappe's Patch Log is per-patch-module, we can't
  simply delete this patch entry from patches.txt: on sites where
  this patch NEVER ran, removing the entry would leave a gap in
  the log. Keeping it as a no-op is the safe path:

    - Sites where this patch is already in Patch Log (successful
      earlier run OR successful Small Text hotfix run) → Frappe
      skips it; the follow-on move_ecs_ee_address... patch handles
      the actual work (data migration + cleanup).

    - Sites where this patch is NOT in Patch Log (failed at the
      failing ALTER TABLE, never got recorded) → this no-op runs,
      succeeds, gets logged, and the follow-on
      move_ecs_ee_address... patch runs next.

DO NOT restore the original ALTER-TABLE-adding logic here. Any new
address-related fields go on EasyEcom Address (the child DocType),
not on tabSales Invoice.
"""
from __future__ import annotations


def execute() -> None:
    # Deliberate no-op — see module docstring.
    #
    # The schema work this patch used to do (add 13 flat address
    # varchar Custom Fields to Sales Invoice) has been superseded
    # by move_ecs_ee_address_fields_to_child_table, which creates
    # EasyEcom Address (child DocType) and attaches it to SI via a
    # single Table Custom Field.
    return
