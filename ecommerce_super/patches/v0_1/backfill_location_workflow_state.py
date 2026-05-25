"""Back-fill workflow_state on existing EasyEcom Location rows (§8a item 6).

Before §8a, EasyEcom Locations were created manually by the FDE (no
workflow). With §8a the EasyEcom Location Workflow attaches and rows
need a workflow_state set; otherwise the workflow can't render its
action buttons and the controller's `_derive_is_operational_from_workflow_state`
short-circuits (no state = leave is_operational alone).

Mapping per the packet:
  - is_operational = 1 AND frappe_company set → Live
  - frappe_company set (but not yet operational)  → Mapped but not Live
  - else (no Company)                             → To Map

We do not back-fill into Skipped — that's a deliberate FDE decision, never
a default. Any row a human marked "out of scope" before workflow existed
was just deleted; we don't try to recover that intent.

Idempotent: only touches rows whose workflow_state is currently empty.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    # frappe.db.table_exists takes the DocType name without the `tab`
    # prefix — it prepends `tab` internally. Passing "tabX" makes Frappe
    # look for table `tabtabX` and silently return False, which would
    # turn the entire patch into a no-op.
    if not frappe.db.table_exists("EasyEcom Location"):
        return

    # Only touch rows missing a workflow_state — re-running the patch is a no-op.
    # SQL's IN doesn't match NULL, so we go direct rather than using Frappe's
    # `("in", ("", None))` shorthand which silently misses NULL rows.
    rows = frappe.db.sql(
        """SELECT name, frappe_company, is_operational
           FROM `tabEasyEcom Location`
           WHERE workflow_state IS NULL OR workflow_state = ''""",
        as_dict=True,
    )
    if not rows:
        return

    counts = {"Live": 0, "Mapped but not Live": 0, "To Map": 0}
    for row in rows:
        if row.is_operational and row.frappe_company:
            state = "Live"
        elif row.frappe_company:
            state = "Mapped but not Live"
        else:
            state = "To Map"
        frappe.db.set_value(
            "EasyEcom Location",
            row.name,
            "workflow_state",
            state,
            update_modified=False,
        )
        counts[state] += 1

    frappe.db.commit()

    print(
        "[ecommerce_super] back-filled EasyEcom Location workflow_state: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
    )
