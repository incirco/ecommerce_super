"""§11.5.1 gh#149 — dry-run diagnostic endpoints for /einvoice/update
and /ewaybill/update.

Purpose: let the FDE simulate what WOULD happen if EE fired the real
Custom GSP endpoint for a given SO, without any persistent side
effects. Returns a per-step checklist so misconfigurations (missing
Customer Map, missing Item Map, missing tax template, etc.) surface
immediately instead of after a live EE fire.

Two entry points (both @frappe.whitelist gated to System Manager /
EasyEcom FDE):

  dry_run_einvoice(reference_code) -> checklist
    Simulates /einvoice/update. Fabricates a plausible EE row from
    the SO, runs the real code path via `mirror_si_from_ee_response`
    inside a savepoint, rolls back before returning. No SI is
    persisted; no EE call is made.

  dry_run_ewaybill(reference_code) -> checklist
    Simulates /ewaybill/update. Same shape as einvoice but exercises
    the eway-mint code path (which depends on an already-minted IRN,
    so also simulates that upstream step).

Both consumed by the "Dry-Run Einvoice" / "Dry-Run Ewaybill" buttons
on the B2B Order Map form. The dashboard in gh#150 will call these
to render inline diagnostics on each Map row.

Design constraint: NEVER persist. Every code path that would insert /
update / commit must be wrapped in a savepoint + rollback. If a
savepoint rollback would fail (e.g. an outer transaction), we log a
warning and return the checklist with a "rollback failed" note —
never leaving a partial SI in the DB.
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe import _


_ALLOWED_ROLES = {"System Manager", "EasyEcom FDE"}


def _require_dry_run_permission() -> None:
    """Gate: only System Manager / EasyEcom FDE. Bearer users cannot
    invoke — this is a desk / cURL diagnostic, not a GSP endpoint."""
    if frappe.session.user == "Administrator":
        return
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not (_ALLOWED_ROLES & user_roles):
        frappe.throw(
            _(
                "Dry-run endpoints require System Manager or EasyEcom "
                "FDE role. Your roles: {0}"
            ).format(sorted(user_roles) or "(none)"),
            title=_("Not Permitted"),
        )


@frappe.whitelist(methods=["POST", "GET"])
def dry_run_einvoice(reference_code: str) -> dict:
    """Simulate /einvoice/update for the given SO. Returns a
    per-step checklist; never persists anything."""
    _require_dry_run_permission()
    if not reference_code:
        frappe.throw(_("reference_code is required"))
    return _dry_run(reference_code=reference_code, include_eway=False)


@frappe.whitelist(methods=["POST", "GET"])
def dry_run_ewaybill(reference_code: str) -> dict:
    """Simulate /ewaybill/update for the given SO. Includes the
    einvoice steps first (eway depends on IRN), then the eway-mint
    step. Never persists anything."""
    _require_dry_run_permission()
    if not reference_code:
        frappe.throw(_("reference_code is required"))
    return _dry_run(reference_code=reference_code, include_eway=True)


def _dry_run(*, reference_code: str, include_eway: bool) -> dict:
    """Shared engine — walks the same code path both real endpoints
    take, collecting per-step results into a checklist. All actual
    inserts run inside a savepoint and roll back before return.
    """
    checks: list[dict] = []

    # Step 1 — SO exists
    if not frappe.db.exists("Sales Order", reference_code):
        checks.append(_fail("so_exists", f"Sales Order {reference_code!r} not found"))
        return _summarise(reference_code, checks)
    so = frappe.get_doc("Sales Order", reference_code)
    checks.append(_ok("so_exists", so_name=so.name, docstatus=so.docstatus))

    # Step 2 — B2B Order Map exists
    map_name = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        {"sales_order": reference_code},
        "name",
    )
    if not map_name:
        checks.append(_fail(
            "b2b_order_map",
            f"No B2B Order Map for SO {reference_code!r}. The SO must "
            "have been pushed via §11 before EE can request an invoice.",
        ))
        return _summarise(reference_code, checks)
    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    checks.append(_ok(
        "b2b_order_map",
        map_name=map_name,
        status=map_doc.status,
        ee_order_id=getattr(map_doc, "ee_order_id", None),
    ))

    # Step 3 — Idempotency: already-mirrored SI?
    if map_doc.get("sales_invoice"):
        checks.append(_ok(
            "existing_si",
            note=(
                f"Real /einvoice/update would return the existing SI "
                f"({map_doc.sales_invoice}) via idempotency lookup — "
                "no new SI would be created."
            ),
            sales_invoice=map_doc.sales_invoice,
        ))
        return _summarise(reference_code, checks)

    # Step 4 — Fabricate the EE row the real endpoint would receive
    ee_row = _simulate_ee_row_from_so(so, map_doc)
    checks.append(_ok(
        "ee_row_simulation",
        invoice_id=ee_row["invoice_id"],
        total_amount=ee_row["total_amount"],
        line_count=len(ee_row["order_items"]),
    ))

    # Step 5 — Customer resolution (mirror uses merchant_c_id)
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        _resolve_customer,
    )
    customer = _resolve_customer(ee_row)
    if not customer:
        checks.append(_fail(
            "buyer_resolution",
            f"No EasyEcom Customer Map for ee_c_id {ee_row.get('merchant_c_id')!r}. "
            "Run §8e Customer Pull or fix the Map row before pushing.",
        ))
    else:
        checks.append(_ok("buyer_resolution", resolved_to=customer))

    # Step 6 — Item resolution per SO line
    item_checks = []
    all_items_ok = True
    for so_item in so.items:
        ee_sku = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_name": so_item.item_code},
            "ee_sku",
        )
        entry = {
            "row": so_item.idx,
            "item_code": so_item.item_code,
            "resolved": ee_sku is not None,
            "ee_sku": ee_sku,
        }
        if ee_sku is None:
            all_items_ok = False
        item_checks.append(entry)
    checks.append({
        "step": "item_map",
        "ok": all_items_ok,
        "items": item_checks,
        **({"reason": "One or more SO lines have no EasyEcom Item Map row"}
           if not all_items_ok else {}),
    })

    # Step 7 — Tax template on source SO
    if not getattr(so, "taxes_and_charges", None):
        checks.append(_ok(
            "tax_template",
            note=(
                "SO has no taxes_and_charges template. Mirror will leave "
                "SI.taxes empty — variance check will catch any drift."
            ),
            template=None,
        ))
    else:
        checks.append(_ok(
            "tax_template",
            template=so.taxes_and_charges,
        ))

    # Step 8 — Per-line item_tax_template presence
    lines_missing = [
        it.item_code for it in so.items
        if not getattr(it, "item_tax_template", None)
    ]
    if lines_missing:
        checks.append(_ok(  # not a hard fail — ERPNext falls back to defaults
            "item_tax_template",
            note=(
                f"{len(lines_missing)} line(s) have no item_tax_template. "
                "ERPNext will use the template + item defaults. If tax "
                "computation ends up wrong, set item_tax_template per line."
            ),
            missing_item_codes=lines_missing[:10],  # cap for display
        ))
    else:
        count = len({it.item_code for it in so.items if it.item_tax_template})
        checks.append(_ok("item_tax_template", count=count))

    # If any hard blocker up to this point, skip the SI insert simulation.
    hard_failed = any(
        not c["ok"] for c in checks
        if c["step"] in {"buyer_resolution", "item_map"}
    )
    if hard_failed:
        checks.append({
            "step": "mirror_si_insert",
            "ok": False,
            "reason": (
                "Skipped — upstream buyer / item resolution failed. Fix "
                "those first; re-run dry-run to exercise the SI insert."
            ),
        })
        return _summarise(reference_code, checks)

    # Step 9 — Simulate SI insert inside a savepoint; roll back before returning
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        mirror_si_from_ee_response,
    )
    sp_name = f"dry_run_{frappe.generate_hash(length=8)}"
    si_name: str | None = None
    mirror_error: str | None = None
    grand_total: float | None = None
    variance_pct: float | None = None
    try:
        frappe.db.savepoint(sp_name)
        result = mirror_si_from_ee_response(map_doc=map_doc, ee_row=ee_row)
        si_name = result.get("sales_invoice")
        grand_total = result.get("si_total")
        variance_pct = result.get("variance_pct")
    except Exception as exc:  # noqa: BLE001 — dry-run catches everything intentionally
        mirror_error = f"{type(exc).__name__}: {exc}"
    finally:
        # Roll back the savepoint regardless of success. Wrap in
        # its own try so a rollback failure doesn't mask the real
        # error and doesn't leak partial state to the checklist.
        try:
            frappe.db.rollback(save_point=sp_name)
        except Exception as rb_exc:  # noqa: BLE001
            checks.append(_fail(
                "mirror_si_rollback",
                f"CRITICAL — savepoint rollback failed: "
                f"{type(rb_exc).__name__}: {rb_exc}. Any SI created by "
                "this dry-run may persist. Investigate immediately.",
            ))

    if mirror_error:
        checks.append(_fail(
            "mirror_si_insert",
            f"Mirror would fail with: {mirror_error}. Fix the underlying "
            "issue before /einvoice/update fires for real.",
        ))
        return _summarise(reference_code, checks)

    checks.append(_ok(
        "mirror_si_insert",
        note=(
            f"Mirror would create SI {si_name!r} with grand_total "
            f"₹{grand_total} (variance vs simulated EE total: "
            f"{variance_pct:+.2f}%). Rolled back cleanly."
        ),
        simulated_si_name=si_name,
        grand_total=grand_total,
        variance_pct=variance_pct,
    ))

    # Step 10 — Eway path (only if requested)
    if include_eway:
        # Eway depends on an already-minted IRN. Since we rolled back
        # the SI, we can't actually run mint_eway_for_si — we can only
        # check preconditions: (a) e-waybill is enabled on the site,
        # (b) IC has an e-waybill service configured, (c) the SO has
        # transport details (transporter, vehicle number, distance).
        eway_precheck = _dry_run_eway_prechecks(so)
        checks.append(eway_precheck)

    return _summarise(reference_code, checks)


def _dry_run_eway_prechecks(so: Any) -> dict:
    """Read-only checks for eway prerequisites. Cannot actually simulate
    the eway mint because it requires a persisted, IRN-stamped SI which
    we deliberately don't create in dry-run."""
    details = {}
    missing: list[str] = []

    # India Compliance's E-Waybill Log DocType must exist on the site
    if not frappe.db.exists("DocType", "e-Waybill Log"):
        return _fail(
            "eway_precheck",
            "India Compliance's 'e-Waybill Log' DocType not found — install "
            "india_compliance and enable e-waybill service before eway can fire.",
        )
    details["ic_ewaybill_log_present"] = True

    # SO must have transport-relevant fields for a real eway mint
    for field in ("transporter", "vehicle_no", "distance"):
        val = getattr(so, field, None)
        if not val:
            missing.append(field)
        details[field] = val
    if missing:
        return {
            "step": "eway_precheck",
            "ok": False,
            "reason": (
                f"SO missing transport fields: {missing}. Eway mint will "
                "fail unless India Compliance's fields are populated on "
                "the SO or auto-derivable from the source Delivery Note."
            ),
            **details,
        }
    return _ok("eway_precheck", **details)


def _simulate_ee_row_from_so(so: Any, map_doc: Any) -> dict:
    """Fabricate a plausible EE getOrderDetails-shaped row from the SO.
    Real EE responses come from `/orders/V2/getOrderDetails`; we
    construct one from local data so dry-run doesn't hit the wire.

    Fields set to match what `mirror_si_from_ee_response` reads:
      - invoice_id (fabricated)
      - invoice_currency_code (SO.currency)
      - total_amount (SO.grand_total)
      - merchant_c_id (reverse-lookup via Customer Map)
      - order_items[] — one per SO line with sku + item_quantity +
        taxable_value = so_item.amount (tax-exclusive per-line net)
    """
    merchant_c_id = frappe.db.get_value(
        "EasyEcom Customer Map",
        {"erpnext_customer": so.customer},
        "ee_c_id",
    )
    return {
        "invoice_id": f"DRY-RUN-{frappe.generate_hash(length=10)}",
        "invoice_number": None,
        "invoice_date": str(so.transaction_date),
        "invoice_currency_code": so.currency or "INR",
        "total_amount": float(so.grand_total or 0),
        "merchant_c_id": merchant_c_id,
        "reference_code": so.name,
        "warehouse_id": None,
        "order_items": [
            {
                "sku": frappe.db.get_value(
                    "EasyEcom Item Map",
                    {"erpnext_name": it.item_code},
                    "ee_sku",
                ) or it.item_code,
                "item_quantity": int(it.qty or 0),
                "taxable_value": float(it.amount or 0),
            }
            for it in so.items
        ],
    }


# --- Result-format helpers ---


def _ok(step: str, **kwargs) -> dict:
    return {"step": step, "ok": True, **kwargs}


def _fail(step: str, reason: str, **kwargs) -> dict:
    return {"step": step, "ok": False, "reason": reason, **kwargs}


def _summarise(reference_code: str, checks: list[dict]) -> dict:
    return {
        "ok": all(c["ok"] for c in checks),
        "reference_code": reference_code,
        "checks": checks,
    }
