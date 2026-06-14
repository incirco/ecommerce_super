"""gh#48: audit every Custom Field the integration ships and rescue
the ones whose patches silently no-op'd.

The reproduction on `ci-test.local` 2026-06-12 confirmed that
`create_custom_fields()` can return without creating its target on a
fresh install (race between `bench install-app`'s DocType registry
warm-up and the patches' execute() calls). `tabPatch Log` records the
patch as "executed" anyway, hiding the failure from every observability
surface — until a §10 form fires the column and crashes.

This patch is the auditor. It walks the canonical
`EXPECTED_FIELDS` registry in
`ecommerce_super.easyecom.install.custom_field_verify` and ensures
each entry's column exists (re-creating via the inline-doc.insert
path if not). Logs a summary line so smoke tests and FDE observers can
assert zero-rescue on a healthy install.

Idempotent: re-runs are no-ops once all fields are healthy. Runs late
in patches.txt so individual field patches get the first crack at
materialising their targets; this patch only rescues what they
missed.
"""

from __future__ import annotations

import frappe

from ecommerce_super.easyecom.install.custom_field_verify import run_audit


def execute() -> None:
    summary = run_audit()

    needs_rescue = summary["total"] - summary["ok"] - summary["doctype_missing"]
    if needs_rescue == 0:
        # Healthy site — no patches no-op'd. Quiet path.
        return

    print(
        f"[ecommerce_super] gh#48 audit: total={summary['total']}, "
        f"ok={summary['ok']}, rescued={summary['rescued']}, "
        f"doctype_missing={summary['doctype_missing']}"
    )
    for detail in summary["details"]:
        if detail["before"] != "ok":
            print(
                f"  - {detail['dt']}.{detail['fieldname']}: "
                f"{detail['before']} → {detail['after']}"
            )

    # If we rescued anything, log it to Error Log so FDE-visible
    # observability captures the event. Title says "rescued" — message
    # carries the structured summary so an FDE can diff this site
    # against a healthy baseline.
    if summary["rescued"] > 0:
        frappe.log_error(
            title=(
                f"gh#48: rescued {summary['rescued']} Custom Field(s) "
                "via auto-audit"
            ),
            message=str(summary),
        )

    frappe.db.commit()
