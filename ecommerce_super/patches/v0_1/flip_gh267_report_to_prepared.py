"""gh#267 — flip Corrupt Submitted Sales Invoices report to Prepared.

Frappe's report JSON reload doesn't overwrite `prepared_report` on
already-installed Report records; the flag is one-shot at first
install. This patch flips the flag on existing sites so long-running
scans run as background jobs (email when ready) instead of blocking
the desk for minutes.

Idempotent. No-op if the report doesn't exist yet (fresh installs
already get prepared_report=1 from the JSON).
"""
from __future__ import annotations

import frappe


REPORT_NAME = "Corrupt Submitted Sales Invoices"


def execute() -> None:
    if not frappe.db.exists("Report", REPORT_NAME):
        return
    current = frappe.db.get_value("Report", REPORT_NAME, "prepared_report")
    if current == 1:
        return
    frappe.db.set_value(
        "Report", REPORT_NAME, "prepared_report", 1, update_modified=False
    )
    print(
        f"[flip_gh267_report_to_prepared] {REPORT_NAME}: "
        f"prepared_report {current} → 1"
    )
