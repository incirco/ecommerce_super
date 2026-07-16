"""§11.5.1 gh#150 (part) — manual Re-fire endpoint for /einvoice/update.

The problem this solves (concrete, from live incident pattern):
  Every time we ship a code fix that affects the mirror or mint path
  (gh#201, gh#206, gh#214, gh#218 all in the last 48 hours), MMPL ops
  has to remediate the Draft SIs that got stuck under the broken code.
  Options prior to this: (a) ask MMPL to regenerate invoice from EE UI,
  (b) wait for EE auto-retry, (c) bench console. All slow/awkward.

  This endpoint lets an EasyEcom FDE click a button on the B2B Order
  Map form to re-run the same handler chain EE's /einvoice/update
  would run. Fresh EE data pulled via getOrderDetails; same handler
  chain (find_or_create_si_for_gsp + mint_irn_for_si); same elevated
  session (matches production behavior). Comment logged on the Map
  for the audit trail.

Not a dry-run — this ACTUALLY creates/submits SIs and mints IRN/eway.
For "would it succeed?" without side effects, use gsp_dry_run.

Two entry points, both @frappe.whitelist gated to System Manager /
EasyEcom FDE:

  refire_einvoice(map_name) -> outcome dict
    Fetches EE row → runs einvoice handler chain → returns
    {ok, si_name, irn, message, code_path_executed}. Logs Comment.

Design constraints:
  - Never bypass validation. Re-fire uses the same code path the real
    endpoint uses (idempotency, GST context copy, variance check).
  - Idempotent. If SI already exists for this invoice_id, the handler
    returns it (via find_or_create_si_for_gsp path-1 idempotency);
    the Re-fire won't create duplicates.
  - Permission-gated. FDE role only.
  - Audit trail. Every click logs a Comment on the Map with attempt +
    outcome (success SI name + IRN, or failure reason).
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe import _


_ALLOWED_ROLES = {"System Manager", "EasyEcom FDE"}


def _require_refire_permission() -> None:
    """Gate: only System Manager / EasyEcom FDE. Bearer users cannot
    invoke — this is a desk/cURL tool, not a GSP endpoint."""
    if frappe.session.user == "Administrator":
        return
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not (_ALLOWED_ROLES & user_roles):
        frappe.throw(
            _(
                "Re-fire endpoints require System Manager or EasyEcom "
                "FDE role. Your roles: {0}"
            ).format(sorted(user_roles) or "(none)"),
            title=_("Not Permitted"),
        )


@frappe.whitelist(methods=["POST"])
def refire_einvoice(map_name: str) -> dict:
    """Re-run the /einvoice/update handler chain for the given Map.

    Fetches fresh EE data via getOrderDetails, then runs
    find_or_create_si_for_gsp + mint_irn_for_si (gated on Account's
    gsp_mint_einvoice toggle) under an elevated session. Logs a
    Comment on the Map row with the attempt + outcome.

    Returns:
        {
            "ok": bool,
            "map_name": str,
            "sales_invoice": str | None,
            "irn": str | None,
            "message": str,   # success detail or error reason
        }
    """
    _require_refire_permission()
    if not map_name:
        frappe.throw(_("map_name is required"))

    # Load Map row up-front so we can log Comments against it later
    # even on failure paths.
    if not frappe.db.exists("EasyEcom B2B Order Map", map_name):
        frappe.throw(_(
            "EasyEcom B2B Order Map {0!r} not found."
        ).format(map_name))

    outcome = _do_refire(map_name)
    _log_refire_comment(map_name, outcome)
    return outcome


def _do_refire(map_name: str) -> dict:
    """The engine. Fetches EE row → runs handler chain → structured
    outcome. All exceptions caught and reported as ok=False so the
    JS button always gets a clean response to render."""
    from ecommerce_super.easyecom.api.gsp import _elevated_session
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_or_create_si_for_gsp,
        mint_irn_for_si,
        GSPHandlerError,
    )

    # Step 1: fetch fresh EE row using the same code path polling uses
    try:
        ee_row, ee_account = _fetch_fresh_ee_row(map_name)
    except _RefireEarlyError as exc:
        return _fail(map_name, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(
            map_name,
            f"Could not fetch EE row via getOrderDetails: "
            f"{type(exc).__name__}: {exc}",
        )

    # Step 2: run the handler chain under the same elevated session
    # the real endpoint uses (matches production behavior).
    with _elevated_session():
        try:
            si_name = find_or_create_si_for_gsp(
                ee_row=ee_row, ee_account=ee_account,
            )
        except GSPHandlerError as exc:
            return _fail(map_name, f"SI create/find failed: {exc}", ee_row=ee_row)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                map_name,
                f"SI create raised unexpected: "
                f"{type(exc).__name__}: {exc}",
                ee_row=ee_row,
            )

        try:
            irn_data = mint_irn_for_si(si_name, ee_account=ee_account)
        except GSPHandlerError as exc:
            # SI exists but mint failed — partial success. Report both.
            return {
                "ok": False,
                "map_name": map_name,
                "sales_invoice": si_name,
                "irn": None,
                "message": (
                    f"SI {si_name} created but IRN mint failed: {exc}"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "map_name": map_name,
                "sales_invoice": si_name,
                "irn": None,
                "message": (
                    f"SI {si_name} created but IRN mint raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

    irn = (irn_data or {}).get("data", {}).get(
        "invoice_details", {}
    ).get("irn_details", {}).get("irn")
    return {
        "ok": True,
        "map_name": map_name,
        "sales_invoice": si_name,
        "irn": irn,
        "message": (
            f"Re-fire success — SI {si_name} created/updated"
            + (f", IRN {irn}" if irn else " (IRN mint skipped — Account toggle off)")
        ),
    }


class _RefireEarlyError(Exception):
    """Internal: raised by _fetch_fresh_ee_row when the fetch can't
    proceed for a KNOWN reason (missing SO, missing warehouse mapping,
    empty EE response). Caught by _do_refire and surfaced as ok=False
    with the exact message. Distinguished from unexpected exceptions
    so we can differentiate 'won't work' from 'crashed'."""


def _fetch_fresh_ee_row(map_name: str) -> tuple[dict, str]:
    """Fetch the fresh EE row for this Map via getOrderDetails.
    Returns (ee_row, ee_account). Raises _RefireEarlyError with a
    clear message on any known-fail condition."""
    from ecommerce_super.easyecom.client.client import EasyEcomClient
    from ecommerce_super.easyecom.client.endpoints import ORDER_DETAILS_GET
    from ecommerce_super.easyecom.helpers.warehouse_mapping import (
        get_ee_location_for_warehouse,
    )

    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
    sales_order = map_doc.sales_order
    ee_account = getattr(map_doc, "easyecom_account", None)
    if not sales_order:
        raise _RefireEarlyError(
            f"Map {map_name!r} has no sales_order — cannot re-fire."
        )

    so_warehouse = frappe.db.get_value(
        "Sales Order", sales_order, "set_warehouse",
    )
    if not so_warehouse:
        raise _RefireEarlyError(
            f"Sales Order {sales_order!r} has no set_warehouse — "
            "cannot resolve EE location for the getOrderDetails call."
        )

    ee_location = get_ee_location_for_warehouse(so_warehouse)
    location_key = (
        str(getattr(ee_location, "location_key", "")) if ee_location else ""
    )
    if not location_key:
        raise _RefireEarlyError(
            f"Warehouse {so_warehouse!r} no longer maps to a Live EE "
            "Location — reconfigure before re-firing."
        )

    client = EasyEcomClient(location_key=location_key)
    response = client.get(
        ORDER_DETAILS_GET, params={"reference_code": sales_order},
    )
    rows = _extract_rows(response)
    if not rows:
        raise _RefireEarlyError(
            f"EE returned no order rows for reference_code={sales_order!r}. "
            "EE may not have generated an invoice for this SO yet."
        )

    # Prefer the row that matches our stored invoice_id (if any) so
    # multi-invoice edge cases pick the right one. Otherwise take the
    # first row.
    invoice_id = str(map_doc.invoice_id or "").strip()
    if invoice_id:
        matching = [r for r in rows if str(r.get("invoice_id") or "") == invoice_id]
        if matching:
            return matching[0], ee_account
    return rows[0], ee_account


def _extract_rows(response: Any) -> list[dict]:
    """Mirror the polling module's row extraction — EE's getOrderDetails
    returns `data: [...]` (list of order rows). Defensive against
    variance in the response envelope."""
    if not response:
        return []
    if isinstance(response, list):
        return [r for r in response if isinstance(r, dict)]
    data = (response or {}).get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _fail(map_name: str, message: str, ee_row: dict | None = None) -> dict:
    return {
        "ok": False,
        "map_name": map_name,
        "sales_invoice": None,
        "irn": None,
        "message": message,
    }


def _log_refire_comment(map_name: str, outcome: dict) -> None:
    """Append a Comment on the Map form with attempt + outcome. Never
    raises — a comment failure must not muffle the outcome response."""
    try:
        map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_name)
        status_bit = "✓ success" if outcome.get("ok") else "✗ failed"
        map_doc.add_comment(
            "Comment",
            text=_(
                "<b>Re-fire /einvoice/update ({0}):</b><br>"
                "By: {1}<br>"
                "Outcome: {2}<br>"
                "SI: {3}<br>"
                "IRN: {4}"
            ).format(
                status_bit,
                frappe.utils.escape_html(frappe.session.user or "(unknown)"),
                frappe.utils.escape_html(outcome.get("message") or ""),
                frappe.utils.escape_html(outcome.get("sales_invoice") or "—"),
                frappe.utils.escape_html(outcome.get("irn") or "—"),
            ),
        )
    except Exception:  # noqa: BLE001
        # Comment is best-effort; the outcome dict still carries the
        # real information.
        pass
