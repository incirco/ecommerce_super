"""§10 Stage 4 — Aged GIT (lost-in-transit) detection.

Per the §10 packet's "Variance" section: GIT balance > 0 after
`lost_in_transit_threshold_days` (Account-level, default 30) → nudge
the ERP user on the draft Debit Note + the originating DN. The user
decides: submit the draft DN (accept loss, reverse ITC) or investigate
further. **Integration NEVER auto-submits the DN** — that's an ERP-user
decision per the §10 invariant.

Surface (whitelisted, daily-cron-driven):

  scan_aged_git_for_account(account_name)
    Walks Transfer Maps with status in {Partial-Received,
    SI-Pending} (the "draft DN exists" indicator) AND draft_debit_note
    set AND the originating DN's posting_date older than
    `lost_in_transit_threshold_days`. For each match:
      - Create an ERPNext ToDo on the DN's owner pointing at the
        draft DN.
      - Add a Comment on the originating DN ("GIT aged past threshold
        on this transfer").
    Idempotent: existing Open ToDo for the same Transfer Map → no
    duplicate.

  scan_all_aged_git()
    Cron handler. Walks every enabled EasyEcom Account; calls
    scan_aged_git_for_account per account. Skips paused accounts
    (pause = "no integration-driven writes" and ToDo creation IS
    a write).

Identifying "the relevant ERP user": uses **the DN's `owner`** (who
created the Delivery Note). They made the transfer; they handle the
variance. Fan-out to all destination-Company Operators is a future
configurable enhancement.

Idempotency mechanism: description-substring match (no Custom Field
back-ref on ToDo — keeps the change additive and avoids touching
the ToDo permission model). The substring is the Transfer Map name,
which is unique per transfer.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt


_TODO_DESCRIPTION_TAG = "§10 Aged GIT"
_TODO_REFERENCE_DOCTYPE = "Purchase Invoice"  # the draft Debit Note


def _is_paused() -> bool:
    """Mirror §9's pause semantics — see transfer_push._is_paused."""
    from ecommerce_super.easyecom.flows.po_push import _is_paused as po_paused

    return po_paused()


@frappe.whitelist()
def scan_aged_git_for_account(account_name: str) -> dict[str, Any]:
    """Returns {created: int, skipped: int, details: list[dict]}."""
    if not account_name or not frappe.db.exists(
        "EasyEcom Account", account_name
    ):
        return {
            "ok": False,
            "message": f"Account {account_name!r} not found.",
        }

    threshold_days = int(
        frappe.db.get_value(
            "EasyEcom Account",
            account_name,
            "lost_in_transit_threshold_days",
        )
        or 30
    )
    if threshold_days <= 0:
        threshold_days = 30

    # Candidates: Transfer Maps where:
    #   - draft_debit_note IS NOT NULL (open gap)
    #   - status indicates open variance
    #   - originating DN's posting_date is older than threshold
    candidates = frappe.db.sql(
        """
        SELECT
          tm.name AS tm_name,
          tm.delivery_note,
          tm.draft_debit_note,
          tm.target_warehouse,
          dn.owner AS dn_owner,
          dn.posting_date,
          DATEDIFF(CURDATE(), dn.posting_date) AS age_days
        FROM `tabEasyEcom Transfer Map` tm
        JOIN `tabDelivery Note` dn ON dn.name = tm.delivery_note
        WHERE tm.draft_debit_note IS NOT NULL
          AND tm.draft_debit_note != ''
          AND tm.status IN (
            'Partial-Received',
            'SI-Pending'
          )
          AND DATEDIFF(CURDATE(), dn.posting_date) >= %s
        """,
        (threshold_days,),
        as_dict=True,
    )

    created = 0
    skipped = 0
    details: list[dict[str, Any]] = []
    for row in candidates:
        if _has_open_aged_git_todo(transfer_map=row["tm_name"]):
            skipped += 1
            details.append(
                {
                    "transfer_map": row["tm_name"],
                    "action": "skipped_existing_todo",
                }
            )
            continue
        gap_qty = _compute_open_gap_qty(transfer_map_name=row["tm_name"])
        _create_aged_git_todo(
            transfer_map=row["tm_name"],
            dn_name=row["delivery_note"],
            dn_owner=row["dn_owner"],
            draft_dn=row["draft_debit_note"],
            age_days=row["age_days"],
            gap_qty=gap_qty,
            threshold_days=threshold_days,
        )
        _comment_on_originating_dn(
            dn_name=row["delivery_note"],
            age_days=row["age_days"],
            tm_name=row["tm_name"],
        )
        created += 1
        details.append(
            {
                "transfer_map": row["tm_name"],
                "action": "todo_created",
                "owner": row["dn_owner"],
                "age_days": row["age_days"],
            }
        )

    return {
        "ok": True,
        "account": account_name,
        "threshold_days": threshold_days,
        "created": created,
        "skipped": skipped,
        "details": details,
    }


def scan_all_aged_git() -> dict[str, Any]:
    """Daily cron handler. Iterates enabled Accounts; skips paused."""
    if _is_paused():
        return {
            "ok": False,
            "message": "Pause active — aged GIT scan deferred. "
            "Re-runs on next daily tick after un-pause.",
        }
    accounts = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1},
        pluck="name",
    )
    summaries: list[dict[str, Any]] = []
    for acct in accounts:
        try:
            summaries.append(scan_aged_git_for_account(acct))
        except Exception as exc:
            frappe.log_error(
                title=f"§10 aged GIT scan failed for {acct}",
                message=f"{type(exc).__name__}: {exc}",
            )
            summaries.append(
                {
                    "ok": False,
                    "account": acct,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"ok": True, "summaries": summaries}


# ============================================================
# Helpers
# ============================================================


def _has_open_aged_git_todo(*, transfer_map: str) -> bool:
    """Idempotency check — existing Open ToDo whose description names
    this Transfer Map. Avoids the ToDo Custom Field permission detour."""
    return bool(
        frappe.db.sql(
            """
            SELECT 1
            FROM `tabToDo`
            WHERE status = 'Open'
              AND description LIKE %s
              AND description LIKE %s
            LIMIT 1
            """,
            (
                f"%{_TODO_DESCRIPTION_TAG}%",
                f"%{transfer_map}%",
            ),
        )
    )


def _compute_open_gap_qty(*, transfer_map_name: str) -> float:
    """Sum of absolute line qtys on the draft Debit Note — the
    canonical "still missing" total."""
    draft_dn = frappe.db.get_value(
        "EasyEcom Transfer Map", transfer_map_name, "draft_debit_note"
    )
    if not draft_dn:
        return 0.0
    rows = frappe.db.sql(
        """
        SELECT qty FROM `tabPurchase Invoice Item`
        WHERE parent = %s
        """,
        (draft_dn,),
        as_dict=True,
    )
    return sum(abs(flt(r["qty"])) for r in rows)


def _create_aged_git_todo(
    *,
    transfer_map: str,
    dn_name: str,
    dn_owner: str,
    draft_dn: str,
    age_days: int,
    gap_qty: float,
    threshold_days: int,
) -> str | None:
    """Insert an Open ToDo on the DN owner pointing at the draft DN."""
    if not dn_owner:
        return None
    description = (
        f"{_TODO_DESCRIPTION_TAG} — Transfer Map {transfer_map} has "
        f"{gap_qty:g} units in GIT for {age_days} day(s) (> "
        f"{threshold_days} threshold) on DN {dn_name}. "
        f"Submit draft DN {draft_dn} to accept loss, or investigate."
    )
    try:
        todo = frappe.get_doc(
            {
                "doctype": "ToDo",
                "owner": dn_owner,
                "allocated_to": dn_owner,
                "reference_type": _TODO_REFERENCE_DOCTYPE,
                "reference_name": draft_dn,
                "description": description,
                "priority": "Medium",
                "status": "Open",
            }
        )
        # ToDo is informational, not a workflow gate. If the draft DN
        # was renamed, deleted, or replaced between candidate-select
        # and ToDo-insert, we still want the FDE to see the aged-GIT
        # signal — the description carries enough context.
        todo.flags.ignore_links = True
        todo.insert(ignore_permissions=True)
        return todo.name
    except Exception as exc:
        frappe.log_error(
            title=f"§10 aged-GIT ToDo create failed for {transfer_map}",
            message=f"{type(exc).__name__}: {exc}\n\n{description[:500]}",
        )
        return None


def _comment_on_originating_dn(
    *, dn_name: str, age_days: int, tm_name: str
) -> None:
    """Audit Comment on the originating DN so the ERP user sees it
    when they open the DN form."""
    try:
        doc = frappe.get_doc("Delivery Note", dn_name)
        doc.add_comment(
            comment_type="Info",
            text=(
                f"<b>§10 GIT aged past threshold</b> on this transfer "
                f"({age_days} day(s) since DN submit). See Transfer "
                f"Map <code>{tm_name}</code> for the open gap and "
                "draft Debit Note. Decision: submit DN to accept loss "
                "or investigate further."
            ),
        )
    except Exception as exc:
        frappe.log_error(
            title=f"§10 aged-GIT Comment failed on DN {dn_name}",
            message=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "scan_aged_git_for_account",
    "scan_all_aged_git",
]
