"""§9 Stage 4 — Pre-Go-Live readiness check for the Buying flow.

Before flipping `auto_push_pos_on_save=1` on an Account, the FDE needs
a single source of truth for "everything Stage 2 + 3 will need to find
configured." This module is that check.

Surface (whitelisted, FDE-facing):
  - precheck_buying_go_live(account) → structured findings dict

Findings come in three buckets:
  - blockers : the flow will refuse to run / fail loudly. Go-Live MUST
    NOT proceed.
  - warnings : the flow will run but with reduced safety. FDE decides.
  - ok       : checked-and-passed items, surfaced so the FDE sees
    coverage (i.e. "we DID look at this").

Checks (mapped to packet line 95 + Stage 3 live-findings):

  1. Stock Settings.allow_negative_stock = 0
     Rationale: §9 Stage 3 PR submit calls update stock; with
     allow_negative_stock=1, a receipt against an over-issued PR can
     leave stock figures inconsistent. The packet doesn't mandate
     this explicitly but the §22 alert routing will flag the silent
     negative-stock branches as data-integrity issues. WARNING (not
     blocker) — some clients legitimately run allow_negative_stock=1.

  2. EasyEcom Account.default_rejected_warehouse is set
     Rationale: §9 Stage 3 builds the PR's rejected_warehouse from
     this. Without it, qc-fail / non-acceptance GRN paths fail at
     PR-submit ('Rejected Warehouse is missing'). BLOCKER.

  3. Each Live EasyEcom Location's mapped_warehouse has a resolvable
     Address (with address_line1 or city)
     Rationale: §9 Stage 2's _run_preconditions refuses to push a PO
     whose warehouse address can't be resolved (EE's
     CreatePurchaseOrder requires a non-empty address). BLOCKER per
     location.

  4. Each Live + enabled Location's mapped_warehouse has a resolvable
     Address (with address_line1 or city). The single-Account
     invariant (§8.1) means all Locations on the site belong to the
     single enabled Account; no per-Location FK exists.

  5. grn_receipt_trigger_status is one of {'1 CREATED', '2 QC Pending',
     '3 QC Complete'}
     Rationale: malformed / blank value breaks the §9 Stage 3 status
     gate. Default ('3 QC Complete') is safe. BLOCKER if blank.

  6. grn_pull_high_watermark is set (the onboarding cutoff)
     Rationale: the user-clarified contract (2026-05-28) — "during
     onboarding we need to define time from when we have to pull the
     already received material; system should not create POs against
     them; any GRN pulled without a linked PO created through ERPNext
     is grn drift." A NULL watermark would fall back to EE's last-7-
     days backstop, dragging in historical GRNs against POs that
     may already be manually receipted in books. The FDE must prime
     this datetime to the cutoff (typically 'now' at go-live) before
     the GRN pull is safe to run. BLOCKER if NULL.

This is read-only. No state mutation. Safe to call repeatedly.
"""

from __future__ import annotations

from typing import Any

import frappe


def _check_role(action_label: str) -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    allowed = {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    if not roles.intersection(allowed):
        frappe.throw(
            frappe._("{0} requires EasyEcom FDE or System Manager.").format(
                action_label
            ),
            frappe.PermissionError,
        )


@frappe.whitelist()
def precheck_buying_go_live(account: str) -> dict[str, Any]:
    """Read-only readiness check. Returns:
      {
        "ok": True | False,             # False if any blockers
        "account": "...",
        "blockers": [str, ...],
        "warnings": [str, ...],
        "checked": [str, ...],
      }
    """
    _check_role("Buying Pre-Go-Live Check")

    if not account or not frappe.db.exists("EasyEcom Account", account):
        return {
            "ok": False,
            "account": account,
            "blockers": [f"Account {account!r} not found."],
            "warnings": [],
            "checked": [],
        }

    blockers: list[str] = []
    warnings: list[str] = []
    checked: list[str] = []

    _check_stock_settings(warnings, checked)
    _check_rejected_warehouse(account, blockers, checked)
    _check_grn_receipt_trigger_status(account, blockers, checked)
    _check_location_addresses(account, blockers, checked)
    _check_grn_pull_high_watermark(account, blockers, checked)

    return {
        "ok": not blockers,
        "account": account,
        "blockers": blockers,
        "warnings": warnings,
        "checked": checked,
    }


def _check_stock_settings(
    warnings: list[str], checked: list[str]
) -> None:
    """Stock Settings.allow_negative_stock — soft warning, not blocker."""
    allow_neg = int(
        frappe.db.get_single_value(
            "Stock Settings", "allow_negative_stock"
        )
        or 0
    )
    if allow_neg:
        warnings.append(
            "Stock Settings → allow_negative_stock is enabled. §9 GRN "
            "receipts will still submit, but qty divergences won't be "
            "caught at PR-submit — they'll surface later as recon "
            "drift. Disable unless your accounting policy explicitly "
            "requires it."
        )
    else:
        checked.append("Stock Settings → allow_negative_stock = 0.")


def _check_rejected_warehouse(
    account: str, blockers: list[str], checked: list[str]
) -> None:
    """Account.default_rejected_warehouse must be set."""
    rejected = frappe.db.get_value(
        "EasyEcom Account", account, "default_rejected_warehouse"
    )
    if not rejected:
        blockers.append(
            "EasyEcom Account → default_rejected_warehouse is not set. "
            "§9 GRN receipts that hit the qc-fail / non-acceptance "
            "branches will fail at PR-submit. Pick a Warehouse on the "
            "Account form before going live."
        )
    elif not frappe.db.exists("Warehouse", rejected):
        blockers.append(
            f"EasyEcom Account → default_rejected_warehouse = "
            f"{rejected!r}, but no Warehouse with that name exists. "
            "Pick a valid Warehouse."
        )
    else:
        checked.append(
            f"EasyEcom Account → default_rejected_warehouse = "
            f"{rejected!r} ✓"
        )


def _check_grn_pull_high_watermark(
    account: str, blockers: list[str], checked: list[str]
) -> None:
    """The onboarding cutoff datetime. Per the 2026-05-28 user
    clarification, GRN pull is only safe to fire when the watermark
    is set — a NULL value would fall back to EE's last-7-days backstop
    and pull historical GRNs against POs that may already be manually
    receipted. BLOCKER."""
    watermark = frappe.db.get_value(
        "EasyEcom Account", account, "grn_pull_high_watermark"
    )
    if not watermark:
        blockers.append(
            "EasyEcom Account → grn_pull_high_watermark is not set. "
            "This is the ONBOARDING CUTOFF — material received before "
            "this datetime is considered already in books and won't be "
            "pulled as a new GRN. Without it, the first GRN pull would "
            "fall back to EE's last-7-days backstop and create PRs for "
            "historical GRNs. Prime this field to the cutoff datetime "
            "(typically 'now' at go-live) before enabling GRN pull. "
            "Contract: any GRN pulled without a linked PO created "
            "through ERPNext is grn drift (Discrepancy, not PR)."
        )
    else:
        checked.append(
            f"EasyEcom Account → grn_pull_high_watermark = {watermark} ✓"
        )


def _check_grn_receipt_trigger_status(
    account: str, blockers: list[str], checked: list[str]
) -> None:
    valid = {"1 CREATED", "2 QC Pending", "3 QC Complete"}
    value = frappe.db.get_value(
        "EasyEcom Account", account, "grn_receipt_trigger_status"
    )
    if not value:
        blockers.append(
            "EasyEcom Account → grn_receipt_trigger_status is blank. "
            "§9 Stage 3's status gate can't function. Default '3 QC "
            "Complete' is safe; pick lifecycle-earlier values only if "
            "your client receipts on the EE-side QC Pending state."
        )
    elif value not in valid:
        blockers.append(
            f"EasyEcom Account → grn_receipt_trigger_status = "
            f"{value!r} is not one of {sorted(valid)}."
        )
    else:
        checked.append(
            f"EasyEcom Account → grn_receipt_trigger_status = "
            f"{value!r} ✓"
        )


def _check_location_addresses(
    account: str, blockers: list[str], checked: list[str]
) -> None:
    """Every Live + Enabled Location's mapped_warehouse needs a
    resolvable Address with address_line1 or city. Mirrors the
    Stage 4 carry-in precondition in po_push._run_preconditions."""
    # §8.1 single-Account: all Locations on the site belong to the
    # single enabled Account. No Location.account FK exists (the
    # belonging is implicit via the single-Account invariant).
    locations = frappe.db.get_all(
        "EasyEcom Location",
        filters={
            "workflow_state": "Live",
            "enabled": 1,
        },
        fields=["name", "location_key", "mapped_warehouse"],
    )
    if not locations:
        blockers.append(
            "No Live + Enabled EasyEcom Locations on this Account. "
            "Run Location Discovery and mark each Location Live before "
            "going live on Buying."
        )
        return

    n_ok = 0
    for loc in locations:
        wh = loc.get("mapped_warehouse")
        if not wh:
            blockers.append(
                f"Location {loc.location_key!r}: no mapped_warehouse. "
                "Link a Warehouse to this Location."
            )
            continue
        addr = _resolve_warehouse_address(wh)
        addr_value = ((addr or {}).get("address_line1") or "") + (
            (addr or {}).get("city") or ""
        )
        if not addr_value.strip():
            blockers.append(
                f"Location {loc.location_key!r} → Warehouse {wh!r}: "
                "no resolvable Address (need address_line1 or city). "
                "Link an Address to the Warehouse via Address.links."
            )
            continue
        n_ok += 1

    if n_ok:
        checked.append(
            f"{n_ok}/{len(locations)} Location warehouses have "
            "resolvable addresses ✓"
        )


def _resolve_warehouse_address(warehouse: str) -> dict | None:
    """Same query as grn_pull._resolve_warehouse_address — kept inline
    to avoid an import cycle if grn_pull starts depending on this
    module in the future."""
    rows = frappe.db.sql(
        """
        SELECT a.address_line1, a.city
        FROM `tabAddress` a
        JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.parenttype = 'Address'
          AND dl.link_doctype = 'Warehouse'
          AND dl.link_name = %s
        ORDER BY a.creation ASC
        LIMIT 1
        """,
        (warehouse,),
        as_dict=True,
    )
    return rows[0] if rows else None
