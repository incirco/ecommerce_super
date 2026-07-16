"""gh#205 part 2 — heal any pre-fix Draft SIs on the site so the
runtime `_reassert_si_dates_for_submit` healer can be safely deleted.

Background:
  gh#161 v2 (shipped 2026-07-13) added `set_posting_time = 1` to the
  mirror-insert path so the SI freezes its posting_date. Draft SIs
  created BEFORE that landed have `set_posting_time = 0`, and on
  submit ERPNext's `set_posting_time_and_date()` resets posting_date
  to today — landing due_date < posting_date if the SI was drafted
  on a prior day.

  A runtime healer (`_reassert_si_dates_for_submit`) was added in the
  same shipment. It fires on every submit through the GSP handler
  and mutates Draft SIs on the fly. This has been fine, but it's a
  workaround for a data problem — not a real ongoing need. Once the
  pre-fix Drafts clear, the healer is dead code.

  This migration patch runs the same heal logic in one shot at
  migrate time. After it runs on any deployed site, no pre-fix Drafts
  remain, and the runtime healer can be deleted (which this same PR
  also does — see gsp_handler.py delta).

Idempotent: runs on every migrate but is a no-op once healed. Any
site that never had pre-fix Drafts (fresh installs) sees a 0-count
log entry, nothing more.
"""
from __future__ import annotations

import frappe


def execute() -> None:
    # Only Draft SIs that (a) came from our mirror (have the EE
    # invoice_id back-ref) AND (b) have set_posting_time=0 are the
    # target. Fresh SIs from post-gh#161-v2 mirrors have set_posting_time=1
    # so this list is bounded to the pre-fix cohort.
    if not frappe.db.has_column("Sales Invoice", "ecs_easyecom_invoice_id"):
        # App not yet installed on this site — nothing to do.
        return

    candidates = frappe.get_all(
        "Sales Invoice",
        filters={
            "docstatus": 0,
            "set_posting_time": 0,
            "ecs_easyecom_invoice_id": ["!=", ""],
        },
        fields=["name", "posting_date", "due_date", "payment_terms_template"],
        limit=1000,  # cap to prevent unbounded scans; a site with
        # 1000+ pre-fix Drafts has a bigger problem than this patch
    )

    if not candidates:
        frappe.log_error(
            title="gh#205 heal: no pre-fix Draft SIs found",
            message=(
                "Site has zero Draft SIs with set_posting_time=0 and an "
                "EE invoice_id back-ref. The runtime healer is safely "
                "deleted."
            ),
        )
        return

    healed: list[str] = []
    for si in candidates:
        # `si` is a frappe._dict (from get_all) which supports both attr
        # and dict access. Use dict access to also stay compatible with
        # plain-dict test fixtures.
        si_name = si["name"] if "name" in si else si.get("name")
        try:
            _heal_one_si(si)
            healed.append(si_name)
        except Exception as exc:  # noqa: BLE001 — patch continues past per-SI failures
            frappe.log_error(
                title=f"gh#205 heal: failed on {si_name}",
                message=f"{type(exc).__name__}: {exc}",
            )

    frappe.db.commit()
    frappe.log_error(
        title=f"gh#205 heal: healed {len(healed)} of {len(candidates)} pre-fix Draft SIs",
        message=(
            f"Healed SIs (set_posting_time=1, due_date=posting_date, "
            f"payment_terms_template cleared): {healed!r}"
        ),
    )


def _heal_one_si(si: dict) -> None:
    """Apply the same heal the runtime `_reassert_si_dates_for_submit`
    applied per-SI: set posting_time flag, align due_date to
    posting_date if it's earlier, clear any payment_terms_template
    that would re-derive schedule."""
    from frappe.utils import getdate

    updates = {"set_posting_time": 1}

    # Align due_date if it's before posting_date (defensive against
    # non-parseable values — the guard mirrors the runtime healer).
    try:
        if (
            si.get("due_date")
            and si.get("posting_date")
            and getdate(si["due_date"]) < getdate(si["posting_date"])
        ):
            updates["due_date"] = si["posting_date"]
    except (TypeError, ValueError, AttributeError):
        pass  # skip the compare on malformed data

    if si.get("payment_terms_template"):
        updates["payment_terms_template"] = ""

    frappe.db.set_value(
        "Sales Invoice", si["name"], updates, update_modified=False,
    )
    # Clear payment_schedule child rows too (matches runtime healer).
    if si.get("payment_terms_template"):
        frappe.db.delete(
            "Payment Schedule",
            {"parent": si["name"], "parenttype": "Sales Invoice"},
        )
