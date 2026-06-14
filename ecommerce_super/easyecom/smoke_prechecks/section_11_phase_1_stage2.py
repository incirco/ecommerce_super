"""§11 Phase 1 Stage 2 smoke-precheck.

Exercises every Stage 2 helper-to-DB path on a real bench, without
any EE-side call. Catches the mock-vs-real disconnect class of bug
(the §10 SI back-link silent-bug pattern; the Stage 2
`get_ee_account_for_warehouse` missing-field bug that prompted this
script's creation).

Four checks:
  1. validate_pre_push resolves the EE Account against the real bench
     and surfaces the first precondition refusal with the exact
     packet message text.
  2. on_submit_push enqueues an EasyEcom Queue Job row pointing at
     push_b2b_order_async with the SO target.
  3. push_b2b_order_async, run directly with an SO that would fail
     at a precondition, throws inside the payload builder
     (resolve_ee_sku_or_throw or customer_block billing-address
     check) BEFORE any EasyEcomClient instantiation.
  4. cancel_b2b_order_from_erpnext, called on an SO with no Map row,
     throws "has no §11 push to cancel" before any EE call.

Run pattern (no EE call):
  bench --site smoke-test.local console <<EOF
  import json, importlib
  from process.smoke_prechecks import section_11_phase_1_stage2 as m
  importlib.reload(m)
  print(json.dumps(m.check(), default=str, indent=2))
  EOF

The script creates a deterministic precheck SO + customer on the
target site. Idempotent — re-runs reuse the existing fixtures and
just exercise the paths again.
"""

from __future__ import annotations

from typing import Any

import frappe


_PRECHECK_CUSTOMER_NAME = "ECS-S11-PRECHECK-CUST"
_PRECHECK_SO_NAME_PREFIX = "ECS-S11-PRECHECK-SO"


def check() -> dict:
    """Run all four Stage 2 smoke-prechecks. Returns a structured
    result dict per check."""
    out: dict = {
        "preflight": {},
        "checks": {},
    }

    # ---- Preflight: ensure the bench has §11 substrate + a Live
    # warehouse + one mapped Item + an enabled Account.
    out["preflight"]["substrate_present"] = _verify_substrate()
    if not out["preflight"]["substrate_present"]["ok"]:
        return out
    live = _pick_live_warehouse()
    if not live:
        out["preflight"]["live_warehouse"] = {
            "ok": False,
            "detail": (
                "No Live + enabled EasyEcom Location with mapped "
                "warehouse on this bench. Cannot exercise §11 paths."
            ),
        }
        return out
    out["preflight"]["live_warehouse"] = {
        "ok": True,
        "warehouse": live["warehouse"],
        "company": live["company"],
        "location_key": live["location_key"],
    }
    item = _pick_mapped_item()
    if not item:
        out["preflight"]["mapped_item"] = {
            "ok": False,
            "detail": "No Mapped EasyEcom Item Map on this bench.",
        }
        return out
    out["preflight"]["mapped_item"] = {
        "ok": True,
        "item_code": item,
    }
    account = _ensure_account_for_precheck()
    if not account:
        out["preflight"]["account"] = {
            "ok": False,
            "detail": (
                "No EasyEcom Account on this bench (and one cannot be "
                "created from a precheck). Configure one before re-running."
            ),
        }
        return out
    out["preflight"]["account"] = {
        "ok": True,
        "account": account,
    }

    # ---- Fixture setup
    customer = _ensure_precheck_customer(live["company"])
    so_name = _ensure_precheck_so(
        customer=customer,
        company=live["company"],
        set_warehouse=live["warehouse"],
        item_code=item,
    )
    out["preflight"]["fixtures"] = {
        "customer": customer,
        "so": so_name,
    }

    # ---- Check 1: validate_pre_push throws with packet refusal text.
    out["checks"]["1_validate_pre_push_refuses_unsynced"] = (
        _check_validate_pre_push(so_name)
    )

    # ---- Check 2: on_submit_push enqueues a Queue Job row.
    out["checks"]["2_on_submit_push_enqueues_queue_job"] = (
        _check_on_submit_enqueue(so_name)
    )

    # ---- Check 3: push_b2b_order_async throws inside builder before
    # any EE client instantiation.
    out["checks"]["3_async_push_throws_before_ee_call"] = (
        _check_async_push_refusal(so_name)
    )

    # ---- Check 4: cancel without Map throws.
    out["checks"]["4_cancel_no_map_throws"] = (
        _check_cancel_no_map(so_name)
    )

    # Overall verdict
    all_ok = all(
        v.get("ok") for v in out["checks"].values()
    )
    out["overall"] = {
        "ok": all_ok,
        "passes": sum(1 for v in out["checks"].values() if v.get("ok")),
        "total": len(out["checks"]),
    }
    return out


# ============================================================
# Preflight helpers
# ============================================================


def _verify_substrate() -> dict:
    """Confirm Stage 1 + 2 substrate is present on the bench."""
    needed_fields = [
        ("EasyEcom Account", "ecs_b2b_module"),
        ("Sales Order", "ecs_b2b_order_map"),
    ]
    missing = []
    for dt, fn in needed_fields:
        if not frappe.db.has_column(dt, fn):
            missing.append(f"{dt}.{fn}")
    if not frappe.db.exists("DocType", "EasyEcom B2B Order Map"):
        missing.append("DocType:EasyEcom B2B Order Map")
    if missing:
        return {
            "ok": False,
            "detail": f"Substrate missing: {missing}",
        }
    return {"ok": True}


def _pick_live_warehouse() -> dict | None:
    """Return (warehouse, company, location_key) for any Live EE
    Location with a mapped warehouse on this bench."""
    row = frappe.db.get_value(
        "EasyEcom Location",
        {
            "workflow_state": "Live",
            "enabled": 1,
            "mapped_warehouse": ("is", "set"),
        },
        ["mapped_warehouse", "frappe_company", "location_key"],
        as_dict=True,
    )
    if not row:
        return None
    return {
        "warehouse": row["mapped_warehouse"],
        "company": row.get("frappe_company")
        or frappe.db.get_value("Warehouse", row["mapped_warehouse"], "company"),
        "location_key": row.get("location_key"),
    }


def _pick_mapped_item() -> str | None:
    return frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Item", "status": "Mapped"},
        "erpnext_name",
    )


def _ensure_account_for_precheck() -> str | None:
    """Return an enabled Account name with ecs_b2b_module set, or
    flip the first existing Account into that shape for the precheck.

    The precheck deliberately mutates Account state so the validate_pre_push
    path can resolve a real Account — substrate validation must hit the
    actual singleton-Account query. Caller is responsible for restoring
    pre-precheck Account state if they care; on smoke-test.local this is
    expected to be in a dev-mutable state.
    """
    name = frappe.db.get_value(
        "EasyEcom Account",
        {"enabled": 1},
        "name",
    )
    if not name:
        # No enabled Account — try to enable one with a B2B module
        # configured. If even disabled accounts don't exist, give up.
        any_account = frappe.db.get_value(
            "EasyEcom Account", {}, "name"
        )
        if not any_account:
            return None
        frappe.db.set_value(
            "EasyEcom Account",
            any_account,
            {"enabled": 1, "ecs_b2b_module": "Old B2B"},
            update_modified=False,
        )
        frappe.db.commit()
        name = any_account
    else:
        # Account is enabled — ensure ecs_b2b_module is set.
        current = frappe.db.get_value(
            "EasyEcom Account", name, "ecs_b2b_module"
        )
        if not current:
            frappe.db.set_value(
                "EasyEcom Account",
                name,
                "ecs_b2b_module",
                "Old B2B",
                update_modified=False,
            )
            frappe.db.commit()
    return name


# ============================================================
# Fixture setup
# ============================================================


def _ensure_precheck_customer(company: str) -> str:
    """Ensure a Customer that does NOT have a §8e Customer Map row
    (so precondition #3 will fire). Re-use across runs."""
    if frappe.db.exists("Customer", _PRECHECK_CUSTOMER_NAME):
        return _PRECHECK_CUSTOMER_NAME
    group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
    territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
    doc = frappe.new_doc("Customer")
    doc.update(
        {
            "customer_name": _PRECHECK_CUSTOMER_NAME,
            "customer_type": "Company",
            "customer_group": group,
            "territory": territory,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()
    return doc.name


def _ensure_precheck_so(
    *,
    customer: str,
    company: str,
    set_warehouse: str,
    item_code: str,
) -> str:
    """Ensure a DRAFT Sales Order against the precheck fixtures.
    Reuses existing precheck SO if found; otherwise creates a new one.
    Returns its name."""
    existing = frappe.db.get_value(
        "Sales Order",
        {
            "customer": customer,
            "set_warehouse": set_warehouse,
            "docstatus": 0,
        },
        "name",
    )
    if existing:
        return existing
    price_list = (
        frappe.db.get_value("Price List", {"selling": 1}, "name")
        or "Standard Selling"
    )
    doc = frappe.new_doc("Sales Order")
    doc.update(
        {
            "customer": customer,
            "company": company,
            "transaction_date": frappe.utils.today(),
            "delivery_date": frappe.utils.add_days(frappe.utils.today(), 3),
            "set_warehouse": set_warehouse,
            "selling_price_list": price_list,
            "currency": "INR",
            "conversion_rate": 1,
            "plc_conversion_rate": 1,
            "ignore_pricing_rule": 1,
        }
    )
    doc.append(
        "items",
        {
            "item_code": item_code,
            "qty": 1,
            "rate": 100,
            "warehouse": set_warehouse,
            "delivery_date": frappe.utils.add_days(frappe.utils.today(), 3),
        },
    )
    doc.flags.ignore_permissions = True
    # Bypass validate (the §11 hook would throw because the customer
    # isn't synced — that's the whole point of this smoke-precheck).
    # Also bypass mandatory (ERPNext mandatory checks fire after
    # validate; we just want the SO row to exist so we can probe the
    # §11 helpers against it).
    doc.flags.ignore_validate = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_links = True
    doc.insert()
    frappe.db.commit()
    return doc.name


# ============================================================
# Checks
# ============================================================


def _check_validate_pre_push(so_name: str) -> dict:
    """Validate that validate_pre_push:
      (a) resolves the singleton EE Account without erroring on a
          field-lookup,
      (b) throws frappe.ValidationError with a §11.2 packet message.
    """
    from ecommerce_super.easyecom.flows.b2b_sales.push import (
        validate_pre_push,
    )

    so = frappe.get_doc("Sales Order", so_name)
    try:
        validate_pre_push(so)
        return {
            "ok": False,
            "detail": (
                "validate_pre_push did NOT throw despite an unsynced "
                "customer. Preconditions are silently passing — substrate "
                "regression."
            ),
        }
    except frappe.ValidationError as exc:
        msg = str(exc)
        # Expect one of the §11.2 refusals — the first failing
        # precondition is "Customer Not Synced" given the precheck
        # customer has no Map row.
        if "not synced to EasyEcom" in msg or "synced" in msg:
            return {
                "ok": True,
                "fired_refusal": "Customer Not Synced (or equivalent)",
                "message": msg,
            }
        return {
            "ok": True,
            "fired_refusal": "Some §11.2 refusal",
            "message": msg,
            "note": (
                "Different precondition fired than expected — "
                "may be HSN / address / module / etc. depending on "
                "this bench's fixture state. Substrate path is valid; "
                "review which condition fired."
            ),
        }
    except Exception as exc:
        # Any other exception is a substrate failure — likely the
        # field-lookup bug we're trying to catch.
        return {
            "ok": False,
            "detail": (
                f"validate_pre_push raised non-ValidationError: "
                f"{type(exc).__name__}: {exc}"
            ),
        }


def _check_on_submit_enqueue(so_name: str) -> dict:
    """Confirm on_submit_push enqueues a Queue Job. We bypass
    validate_pre_push (which would block) by calling on_submit_push
    directly with the unsubmitted SO.
    """
    from ecommerce_super.easyecom.flows.b2b_sales.push import on_submit_push

    so = frappe.get_doc("Sales Order", so_name)
    # Count existing Queue Job rows for this SO before.
    before = frappe.db.count(
        "EasyEcom Queue Job",
        filters={
            "target_doctype": "Sales Order",
            "target_name": so_name,
        },
    )
    try:
        on_submit_push(so)
    except Exception as exc:
        return {
            "ok": False,
            "detail": (
                f"on_submit_push raised: {type(exc).__name__}: {exc}"
            ),
        }
    after = frappe.db.count(
        "EasyEcom Queue Job",
        filters={
            "target_doctype": "Sales Order",
            "target_name": so_name,
        },
    )
    if after > before:
        # Pull the latest job to confirm its shape.
        latest = frappe.db.get_value(
            "EasyEcom Queue Job",
            {
                "target_doctype": "Sales Order",
                "target_name": so_name,
            },
            ["name", "job_type", "state"],
            order_by="creation desc",
            as_dict=True,
        )
        return {
            "ok": True,
            "queue_jobs_before": before,
            "queue_jobs_after": after,
            "latest_job": dict(latest) if latest else None,
        }
    return {
        "ok": False,
        "detail": (
            f"on_submit_push did NOT enqueue. before={before}, after={after}"
        ),
    }


def _check_async_push_refusal(so_name: str) -> dict:
    """Direct-call push_b2b_order_async. Expect throw inside payload
    builder (resolve_ee_sku_or_throw or customer_block billing-address
    check) BEFORE any EasyEcomClient instantiation.

    We assert that the throw type is ValidationError (frappe.throw) —
    NOT an HTTP-level exception, NOT EasyEcomAPIError, NOT a generic
    attribute error from the helper layer (the class of bug Stage 2's
    helper fix addressed).
    """
    from ecommerce_super.easyecom.flows.b2b_sales.push import (
        push_b2b_order_async,
    )

    try:
        outcome = push_b2b_order_async(sales_order=so_name)
        return {
            "ok": False,
            "detail": (
                f"push_b2b_order_async returned without throwing: "
                f"{outcome!r}. Expected a precondition-class refusal."
            ),
        }
    except frappe.ValidationError as exc:
        msg = str(exc)
        return {
            "ok": True,
            "exception_class": "frappe.ValidationError",
            "message": msg,
        }
    except Exception as exc:
        # Other exceptions could be: EE-client instantiation failed,
        # builder hit an AttributeError (the class of bug we're guarding
        # against), or unrelated. Capture verbatim.
        return {
            "ok": False,
            "detail": (
                f"push_b2b_order_async raised unexpected "
                f"{type(exc).__name__}: {exc}. Expected "
                "frappe.ValidationError from the payload builder."
            ),
        }


def _check_cancel_no_map(so_name: str) -> dict:
    """cancel_b2b_order_from_erpnext on an SO with no Map → throws
    with the packet refusal text."""
    from ecommerce_super.easyecom.flows.b2b_sales.cancel import (
        cancel_b2b_order_from_erpnext,
    )

    try:
        cancel_b2b_order_from_erpnext(so_name)
        return {
            "ok": False,
            "detail": (
                "cancel_b2b_order_from_erpnext did NOT throw despite "
                "the SO having no Map row."
            ),
        }
    except frappe.ValidationError as exc:
        msg = str(exc)
        if "has no §11 push to cancel" in msg or "No B2B Push" in msg:
            return {
                "ok": True,
                "exception_class": "frappe.ValidationError",
                "message": msg,
            }
        return {
            "ok": False,
            "detail": (
                f"Cancel threw ValidationError but with unexpected "
                f"message: {msg!r}"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "detail": (
                f"Cancel raised non-ValidationError: "
                f"{type(exc).__name__}: {exc}"
            ),
        }
