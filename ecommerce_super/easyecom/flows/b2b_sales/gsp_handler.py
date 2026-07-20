"""§11.5.1 Mode 1 — SI find/create + India Compliance mint helpers.

Called by api/gsp.py's /einvoice/update and /ewaybill/update endpoint
handlers. The lifecycle:

  1. EE calls /einvoice/update with full order payload
  2. find_or_create_si_for_gsp:
     - Look up SI by ecs_easyecom_invoice_id (idempotency)
     - Else call `invoice_mirror.mirror_si_from_ee_response`, which
       delegates to ERPNext's native `make_sales_invoice(so.name)`
       (see PR #226 — the mirror no longer hand-copies fields).
       The mirror handles its own idempotency by invoice_id, plus a
       legacy-adoption shim for pre-fix SIs whose invoice_id was
       never stamped (gh#227). Multiple invoices per SO produce
       multiple SIs — the Map-lookup middle step that used to collapse
       them was removed in gh#227.
  3. Submit the SI if Draft (IC requires submitted SI for generate_e_invoice)
  4. mint_irn_for_si:
     - If SI.irn already populated → return cached (idempotent)
     - Else call IC's generate_e_invoice → IC writes irn/ack_no/ack_dt
       on SI, creates e-Invoice Log row
  5. Assemble response per EE's contract

For /ewaybill/update:
  - find_si_by_invoice_id (SI MUST already exist from prior einvoice call)
  - update transport fields on SI (vehicle, transporter, etc.)
  - mint_eway_for_si calls IC's generate_e_waybill
  - assemble response

This module deliberately keeps endpoint logic in api/gsp.py and
business logic here. Caller injects EE row + EE account; this module
doesn't touch HTTP.
"""

from __future__ import annotations

import base64
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime


class GSPHandlerError(Exception):
    """Raised when SI find/create or IC mint cannot proceed.

    Callers translate to HTTP 422 with the message in the response.
    """


# ============================================================
# Find / create SI
# ============================================================


def find_or_create_si_for_gsp(
    *,
    ee_row: dict,
    ee_account: str,
) -> str:
    """Locate the SI that should serve this EE order, creating it
    from the payload if missing. Returns SI docname.

    Lookup priority:
      1. ecs_easyecom_invoice_id (idempotency — re-hit returns cached)
      2. reference_code → Map → SO → mirror creates a new SI (delegated
         to invoice_mirror.mirror_si_from_ee_response, which also
         handles legacy-adoption for pre-fix unstamped SIs on the Map)

    Multiple EE invoices for the same SO (partial fulfilment, split
    shipments) produce multiple SIs — one per unique invoice_id.
    The Map's `sales_invoice` pointer becomes "latest SI"; the full
    list of SIs for the SO is on the SO's Connections tab (native
    ERPNext wiring, courtesy of PR #226).

    Raises GSPHandlerError on:
      - Missing invoice_id in payload (nothing to anchor idempotency)
      - reference_code resolves but no SO / no Map (can't create SI)
      - SI create fails due to missing Customer Map / Item Map (mirror
        function surfaces the specific error)
    """
    ee_invoice_id = str(ee_row.get("invoice_id") or "").strip()
    if not ee_invoice_id:
        raise GSPHandlerError(
            "EE payload missing invoice_id — cannot anchor SI lookup."
        )

    # 1. Idempotency — SI already minted for this invoice_id?
    # The mirror's own step 1 does this same check, so this early exit
    # is a small optimisation (avoids loading the Map) rather than the
    # true idempotency gate.
    existing = frappe.db.get_value(
        "Sales Invoice",
        {
            "ecs_easyecom_invoice_id": ee_invoice_id,
            "docstatus": ["!=", 2],  # any not-cancelled doc
        },
        "name",
    )
    if existing:
        return existing

    # 2. Create new SI from the source SO via invoice_mirror
    #    (which delegates to ERPNext's make_sales_invoice).
    #
    # gh#227: the previous version had a middle step here that looked
    # up Map.sales_invoice and returned it when set. That collapsed
    # every second/third EE invoice for the same SO into the first SI
    # (split shipments were under-recorded). Removed — the mirror's
    # own invoice_id idempotency handles the "already mirrored" case,
    # and the mirror's legacy-adoption shim (introduced in the same
    # PR) handles the "Map points at a pre-fix unstamped SI" case.
    reference_code = (ee_row.get("reference_code") or "").strip()
    if not reference_code:
        raise GSPHandlerError(
            "EE payload missing reference_code — cannot create new SI "
            "without an anchor to the originating SO."
        )

    map_doc_name = frappe.db.get_value(
        "EasyEcom B2B Order Map",
        {"sales_order": reference_code},
        "name",
    )
    if not map_doc_name:
        raise GSPHandlerError(
            f"No EasyEcom B2B Order Map found for reference_code "
            f"{reference_code!r}. The SO must have been pushed via §11 "
            "before EE can request an invoice for it."
        )

    map_doc = frappe.get_doc("EasyEcom B2B Order Map", map_doc_name)

    # Reuse the Mode 2 mirror — same SI creation logic, just we'll
    # submit + mint afterwards.
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        InvoiceMirrorError,
        InvoiceMirrorVariance,
        mirror_si_from_ee_response,
    )

    try:
        mirror_result = mirror_si_from_ee_response(
            map_doc=map_doc, ee_row=ee_row,
        )
    except InvoiceMirrorError as exc:
        raise GSPHandlerError(
            f"SI create from EE payload failed: {exc}"
        ) from exc
    except InvoiceMirrorVariance as exc:
        # gh#214-followup — the variance-swallow fix.
        #
        # Previously: this except clause looked up the just-created SI
        # (which the mirror inserted before raising) and RETURNED it as
        # if success. That hid the variance signal: EE received a
        # normal response and the wrong-totalled SI shipped as an
        # invoice. Live incident SO-2610405 (2026-07-16) shipped an
        # under-billed invoice through this exact path — the variance
        # check fired, but this swallow muffled it.
        #
        # Now: post a visible Comment on the Draft SI (so FDE sees it
        # on the form) and propagate as GSPHandlerError. EE will
        # receive an error response and the SI stays in Draft awaiting
        # review — better than silently shipping wrong money.
        #
        # Small-variance case (≤ VARIANCE_THRESHOLD_PCT, currently
        # 0.01%) still returns silently: the mirror never raises for
        # those, so this except doesn't fire. Only >0.01% drift lands
        # here, and that's exactly the case we want to surface.
        si_name = frappe.db.get_value(
            "Sales Invoice",
            {"ecs_easyecom_invoice_id": ee_invoice_id},
            "name",
        )
        if si_name:
            _post_variance_comment_on_si(si_name, ee_invoice_id, str(exc))
        raise GSPHandlerError(
            f"SI variance exceeded threshold — SI left in Draft for FDE "
            f"review, EE call refused to prevent shipping an under-billed "
            f"invoice. Details: {exc}"
        ) from exc

    si_name = mirror_result["sales_invoice"]

    # Link the Map ← SI so future calls hit path 2.
    frappe.db.set_value(
        "EasyEcom B2B Order Map", map_doc_name,
        {
            "sales_invoice": si_name,
            "sales_invoice_mirrored_at": now_datetime(),
        },
        update_modified=False,
    )
    frappe.db.commit()

    return si_name


def find_si_by_invoice_id(ee_invoice_id: str) -> str:
    """Look up SI by ee invoice_id. Raises if not found —
    used by /ewaybill/update which expects the SI to already exist
    from a prior /einvoice/update call."""
    si_name = frappe.db.get_value(
        "Sales Invoice",
        {"ecs_easyecom_invoice_id": str(ee_invoice_id)},
        "name",
    )
    if not si_name:
        raise GSPHandlerError(
            f"No ERPNext Sales Invoice found for EE invoice_id "
            f"{ee_invoice_id!r}. The /einvoice/update endpoint must "
            "be called first to create + mint the SI before /ewaybill/update."
        )
    return si_name


# ============================================================
# India Compliance — IRN mint
# ============================================================


def mint_irn_for_si(si_name: str, *, ee_account: str | None = None) -> dict[str, Any]:
    """Submit SI if Draft, then call IC's generate_e_invoice
    (gated on the EE Account's gsp_mint_einvoice toggle).

    Returns the response shape EE expects:
      {invoice_id, erp_invoice_num, irn, ack_number, ack_date,
       invoice_pdf, irn_qr, invoice_base64}

    Behaviour:
      - gsp_mint_einvoice ON (default): mint IRN on NIC IRP via IC.
        Response carries populated IRN/QR/ack fields.
      - gsp_mint_einvoice OFF: SI is created + submitted (GL impact
        happens), but NO NIC IRP call. Response carries empty
        irn/ack_number/ack_date/irn_qr fields and only the PDF URL.

    Idempotent — if SI already has IRN, returns cached without
    calling NIC IRP again (critical: re-minting creates duplicate
    IRNs which cannot be deleted).

    Raises GSPHandlerError on validation errors. Other exceptions
    (NIC timeouts, IC infra issues) propagate to caller for HTTP 502.
    """
    si = frappe.get_doc("Sales Invoice", si_name)

    # Idempotency — IRN already minted (regardless of toggle, return
    # cached if present; never re-mint).
    if si.get("irn"):
        return _assemble_irn_response(si, ee_account=ee_account)

    # IC requires submitted SI. Submit if Draft (whether or not we
    # then mint IRN — the GL impact happens either way).
    #
    # gh#205 part 2 removed the pre-submit `_reassert_si_dates_for_submit`
    # heal path. It only existed to fix pre-gh#161-v2 Draft SIs
    # (created before 2026-07-13). The one-shot migration patch
    # `heal_gh205_pre_fix_draft_si_dates` (v0_1) runs the same heal on
    # migrate; after that runs, no pre-fix Drafts remain and the runtime
    # healer is dead code. Fresh SIs from the current mirror insert path
    # already have set_posting_time=1 baked in, so the ERPNext-native
    # date freeze works without our intervention.
    if si.docstatus == 0:
        try:
            si.flags.ignore_permissions = True
            si.submit()
        except Exception as exc:
            raise GSPHandlerError(
                f"SI {si_name} could not be submitted: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # Toggle gate — skip IC mint if the EE Account has it off.
    if not _should_mint_einvoice(ee_account):
        si.reload()
        # Return response with empty IRN fields but populated PDF URL.
        return _assemble_irn_response(si, ee_account=ee_account)

    # Call India Compliance.
    from india_compliance.gst_india.utils.e_invoice import (
        generate_e_invoice,
    )
    try:
        generate_e_invoice(docname=si_name, throw=True, force=False)
    except Exception as exc:
        # AlreadyGeneratedError, GSPServerError, ValidationError, etc.
        # Surface back to caller — generate_e_invoice has already
        # written its error to Error Log + e-Invoice Log if applicable.
        # The AlreadyGenerated case is technically OK (idempotency
        # safety net) — re-read the doc to grab the existing IRN.
        si.reload()
        if si.get("irn"):
            return _assemble_irn_response(si, ee_account=ee_account)
        raise GSPHandlerError(
            f"India Compliance IRN mint failed for {si_name}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    si.reload()
    if not si.get("irn"):
        raise GSPHandlerError(
            f"India Compliance returned without populating IRN on "
            f"{si_name}. Check e-Invoice Log for the NIC response detail."
        )

    return _assemble_irn_response(si)


def _post_variance_comment_on_si(
    si_name: str,
    ee_invoice_id: str,
    variance_msg: str,
) -> None:
    """gh#214-followup — add a visible Comment on the Draft SI when the
    variance check fired. Called from the /einvoice/update handler
    right before it raises GSPHandlerError, so the FDE opening the SI
    later sees WHY it's in Draft (instead of digging through Error
    Log or API Call logs).

    Never raises — a comment failure must not block the outer throw.
    The variance is already going to surface as an HTTP error to EE;
    the comment is just a courtesy for the FDE."""
    try:
        si = frappe.get_doc("Sales Invoice", si_name)
        si.add_comment(
            "Comment",
            text=_(
                "<b>Variance alert (gh#214-followup):</b><br><br>"
                "This SI was left in Draft because its total differs from "
                "EE's total by more than the threshold. The /einvoice/update "
                "call from EE was refused so an under-billed invoice was "
                "not shipped.<br><br>"
                "<b>Details:</b> {0}<br>"
                "<b>EE invoice_id:</b> {1}<br><br>"
                "Review the tax lines, correct if needed, then either "
                "submit this SI manually or ask EE to regenerate the "
                "invoice (which will fire /einvoice/update again)."
            ).format(
                frappe.utils.escape_html(variance_msg),
                frappe.utils.escape_html(ee_invoice_id),
            ),
        )
    except Exception:
        # Deliberate broad catch — the comment is best-effort. The
        # variance itself surfaces via the raised GSPHandlerError; a
        # comment failure must not muffle that signal.
        pass


# ============================================================
# India Compliance — E-way bill mint
# ============================================================
#
# NOTE (gh#205 part 2, 2026-07-16): the pre-submit `_reassert_si_dates_for_submit`
# healer that lived here was deleted. It only existed to fix pre-gh#161-v2
# Draft SIs (set_posting_time=0 with EE invoice_id back-ref). The one-shot
# migration patch `heal_gh205_pre_fix_draft_si_dates` (v0_1) runs the same
# heal at migrate time; after that runs, no pre-fix Drafts remain and the
# runtime healer is dead code. Fresh SIs from the current mirror insert
# path (invoice_mirror.py — see `si.set_posting_time = 1` in
# `mirror_si_from_ee_response`) already carry the flag, so the
# ERPNext-native date freeze works without our intervention.


def mint_eway_for_si(
    si_name: str,
    *,
    transport_values: dict[str, Any],
    ee_account: str | None = None,
) -> dict[str, Any]:
    """Call IC's generate_e_waybill with transport values from EE
    (gated on the EE Account's gsp_mint_ewaybill toggle).

    Returns the response shape EE expects:
      {invoice_id, erp_invoice_num, eway_bill_number, eway_bill_date,
       eway_bill_pdf, transport_mode, vehicle_number, vehicle_type,
       transporter_gst, transporter_name, eway_bill_base64}

    Behaviour:
      - gsp_mint_ewaybill ON (default): mint e-way bill on NIC EWB via IC.
        Response carries populated eway_bill_number/date.
      - gsp_mint_ewaybill OFF: NO NIC EWB call. Response carries
        empty eway_bill_number/date/pdf — but transport fields echo
        back so EE has a record.

    Idempotent — if SI.ewaybill already populated, returns cached.
    """
    si = frappe.get_doc("Sales Invoice", si_name)

    if si.get("ewaybill"):
        return _assemble_eway_response(
            si, transport_values=transport_values, ee_account=ee_account,
        )

    if si.docstatus != 1:
        raise GSPHandlerError(
            f"SI {si_name} must be submitted before e-way bill mint. "
            f"Call /einvoice/update first to submit (+ optionally mint IRN)."
        )

    # Toggle gate — skip IC mint if the EE Account has it off.
    if not _should_mint_ewaybill(ee_account):
        # Return response with empty eway fields but echo transport
        # values so EE has a paper trail of what was attempted.
        return _assemble_eway_response(
            si, transport_values=transport_values, ee_account=ee_account,
        )

    import json as _json
    from india_compliance.gst_india.utils.e_waybill import (
        generate_e_waybill,
    )
    try:
        generate_e_waybill(
            doctype="Sales Invoice",
            docname=si_name,
            values=_json.dumps(transport_values),
            force=False,
        )
    except Exception as exc:
        si.reload()
        if si.get("ewaybill"):
            return _assemble_eway_response(
            si, transport_values=transport_values, ee_account=ee_account,
        )
        raise GSPHandlerError(
            f"India Compliance e-way bill mint failed for {si_name}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    si.reload()
    if not si.get("ewaybill"):
        raise GSPHandlerError(
            f"India Compliance returned without populating ewaybill on "
            f"{si_name}. Check e-Waybill Log for the NIC response detail."
        )

    return _assemble_eway_response(si, ee_account=ee_account)


# ============================================================
# Response assembly helpers
# ============================================================


def _assemble_irn_response(
    si: Any,
    *,
    ee_account: str | None = None,
) -> dict[str, Any]:
    """Build the data.invoice_details payload per EE's /einvoice contract."""
    # gh#134: render the PDF as base64 alongside the URL. Base64 is the
    # primary delivery mechanism (self-contained in the response, immune
    # to the auth-middleware trap on the URL side). URL is kept as
    # belt-and-suspenders for EE clients that prefer it.
    invoice_format = _resolve_print_format(
        ee_account, "gsp_print_format", default="Standard",
    )
    return {
        "invoice_id": str(si.get("ecs_easyecom_invoice_id") or ""),
        "erp_invoice_num": si.name,
        "irn": si.get("irn") or "",
        "ack_number": si.get("ack_no") or "",
        "ack_date": si.get("ack_dt").isoformat() if si.get("ack_dt") else "",
        "invoice_pdf": _resolve_invoice_pdf_url(si, ee_account=ee_account),
        "irn_qr": si.get("signed_qr_code") or "",
        "invoice_base64": _render_si_pdf_base64(
            si, format_name=invoice_format,
        ),
    }


def _assemble_eway_response(
    si: Any,
    *,
    transport_values: dict[str, Any] | None = None,
    ee_account: str | None = None,
) -> dict[str, Any]:
    """Build the data.invoice_details payload per EE's /ewaybill contract.

    When `transport_values` is provided (the request payload from EE), we
    echo back the transport fields so the EE record reflects what was
    requested even when the IC mint was skipped (toggle off) or when the
    SI hasn't yet had those fields set by the IC flow.
    """
    tv = transport_values or {}
    # gh#134: render base64 only when an e-way bill actually exists on
    # the SI. Rendering a "no eway" print-format PDF would be empty and
    # confusing to EE.
    eway_format = _resolve_print_format(
        ee_account, "gsp_ewaybill_print_format", default="e-Waybill",
    )
    return {
        "invoice_id": str(si.get("ecs_easyecom_invoice_id") or ""),
        "erp_invoice_num": si.name,
        "eway_bill_number": si.get("ewaybill") or "",
        "eway_bill_date": (
            si.get("e_waybill_validity").isoformat()
            if si.get("e_waybill_validity") else ""
        ),
        "eway_bill_pdf": (
            _resolve_eway_pdf_url(si, ee_account=ee_account)
            if si.get("ewaybill") else ""
        ),
        "transport_mode": (
            si.get("mode_of_transport")
            or tv.get("mode_of_transport")
            or ""
        ),
        "vehicle_number": (
            si.get("vehicle_no") or tv.get("vehicle_no") or ""
        ),
        "vehicle_type": (
            si.get("vehicle_type") or tv.get("vehicle_type") or ""
        ),
        "transporter_gst": (
            si.get("transporter_gst_no")
            or tv.get("transporter_gst_no")
            or ""
        ),
        "transporter_name": (
            si.get("transporter_name") or tv.get("transporter_name") or ""
        ),
        "eway_bill_base64": (
            _render_si_pdf_base64(si, format_name=eway_format)
            if si.get("ewaybill") else ""
        ),
    }


# ============================================================
# Toggle helpers
# ============================================================


def _should_mint_einvoice(ee_account: str | None) -> bool:
    """Read gsp_mint_einvoice toggle on the EE Account.

    Defaults to True (mint) when:
      - ee_account is None (no scoping context — pre-toggle behaviour)
      - the field doesn't exist (patch not yet applied)
      - the account row can't be found

    The toggle field is added by patches/v0_1/add_gsp_basic_auth_secret_field.py
    with default=1, so existing accounts inherit the mint-on behaviour
    after patch rerun.
    """
    if not ee_account:
        return True
    try:
        value = frappe.db.get_value(
            "EasyEcom Account", ee_account, "gsp_mint_einvoice"
        )
    except Exception:
        return True
    if value is None:
        return True
    return bool(int(value))


def _should_mint_ewaybill(ee_account: str | None) -> bool:
    """Read gsp_mint_ewaybill toggle on the EE Account. Same defaulting
    semantics as `_should_mint_einvoice`."""
    if not ee_account:
        return True
    try:
        value = frappe.db.get_value(
            "EasyEcom Account", ee_account, "gsp_mint_ewaybill"
        )
    except Exception:
        return True
    if value is None:
        return True
    return bool(int(value))


def _render_si_pdf_base64(
    si: Any,
    *,
    format_name: str,
    letterhead: bool = True,
) -> str:
    """Render the SI print-format as PDF bytes, base64-encode, return
    as ASCII string ready to inline in the JSON response.

    gh#134: EE's Custom-GSP client (Mode 1) reads `invoice_base64` /
    `eway_bill_base64` directly from the JSON — no follow-up HTTP fetch.
    This eliminates the "can EE reach our URL?" reliability problem the
    URL-only delivery had (the URL endpoint requires session auth and
    hits the same validate_auth trap gh#123 / gh#130 fixed for the
    request side — but there's no equivalent fix on the response-URL
    side EE would GET).

    Empty string on any render failure — never raises. The URL fields
    stay populated too as belt-and-suspenders.

    format_name — precedence caller decides (see _resolve_invoice_pdf_url
    / _resolve_eway_pdf_url for the standard precedence rules).
    """
    try:
        pdf_bytes = frappe.get_print(
            doctype="Sales Invoice",
            name=si.name,
            print_format=format_name,
            as_pdf=True,
            no_letterhead=0 if letterhead else 1,
        )
        if not pdf_bytes:
            _mark_pdf_render_failure(
                si,
                reason="render returned empty result",
                format_name=format_name,
            )
            return ""
        # Frappe returns bytes for PDF renders; guard for str fallback.
        if isinstance(pdf_bytes, str):
            pdf_bytes = pdf_bytes.encode("utf-8")
        return base64.b64encode(pdf_bytes).decode("ascii")
    except Exception as exc:
        _mark_pdf_render_failure(
            si,
            reason=f"{type(exc).__name__}: {exc}",
            format_name=format_name,
        )
        return ""


def _mark_pdf_render_failure(
    si: Any,
    *,
    reason: str,
    format_name: str,
) -> None:
    """gh#137: surface a PDF-render failure on the SI's timeline so an
    FDE looking at the SI immediately sees why EE got IRN+ack but no
    invoice_base64. Keeps the Error Log entry (audit trail) AND adds a
    Comment on the SI docname so the failure isn't invisible.

    Failure mode design (from gh#137):
      - DO NOT fail /einvoice/update — the IRN mint succeeded, that's
        the load-bearing side effect. Response still ships 200 with
        IRN + ack + URL.
      - DO NOT create a Failed Sync Record — the SI push succeeded;
        Failed Sync Record would misleadingly imply the push failed.
      - DO log to Error Log (audit trail).
      - DO add a Comment to the SI so the FDE sees it in the SI timeline.

    Never raises — best-effort observability layer on top of the
    existing best-effort render.
    """
    # Existing behaviour: Error Log entry (kept — audit trail).
    try:
        frappe.log_error(
            title=f"gh#134 GSP base64 PDF render failed for {si.name}",
            message=f"format={format_name!r}: {reason}",
        )
    except Exception:
        pass

    # gh#137: also drop a Comment on the SI's timeline.
    try:
        comment_text = (
            f"gh#137: GSP PDF render failed on Custom-GSP response "
            f"assembly. EE received IRN + ack but no invoice_base64. "
            f"Reason: {reason}. Print Format: {format_name!r}. "
            "Investigate wkhtmltopdf / print-format on this bench."
        )
        frappe.get_doc({
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": "Sales Invoice",
            "reference_name": si.name,
            "content": comment_text,
        }).insert(ignore_permissions=True)
    except Exception:
        # If Comment insert fails for any reason (permissions, DB, etc.),
        # keep going — Error Log entry above is still the audit trail.
        pass


def _resolve_invoice_pdf_url(
    si: Any,
    *,
    ee_account: str | None = None,
) -> str:
    """Return a public URL to download the SI print as PDF.

    Builds on Frappe's standard print URL convention. EE downloads
    on demand. No file is persisted — PDF rendered on each request.

    NOTE (gh#134): the URL is populated as a belt-and-suspenders
    fallback, but the primary delivery mechanism is `invoice_base64`
    on the response payload. Frappe's `download_pdf` endpoint requires
    session auth and EE's anonymous GET would hit the same
    validate_auth trap gh#123 / gh#130 fixed for the request side.

    Print format precedence:
      1. EasyEcom Account.gsp_print_format (per-Account override)
      2. "Standard" (Frappe default Sales Invoice format)
    """
    site_url = (frappe.utils.get_url() or "").rstrip("/")
    format_name = _resolve_print_format(
        ee_account, "gsp_print_format", default="Standard",
    )
    return (
        f"{site_url}/api/method/frappe.utils.print_format.download_pdf"
        f"?doctype=Sales+Invoice&name={si.name}"
        f"&format={frappe.utils.quoted(format_name)}&no_letterhead=0"
    )


def _resolve_eway_pdf_url(
    si: Any,
    *,
    ee_account: str | None = None,
) -> str:
    """Return URL for the e-way bill print format.

    Print format precedence:
      1. EasyEcom Account.gsp_ewaybill_print_format (per-Account override)
      2. "e-Waybill" (India Compliance's default format)
    """
    site_url = (frappe.utils.get_url() or "").rstrip("/")
    format_name = _resolve_print_format(
        ee_account, "gsp_ewaybill_print_format", default="e-Waybill",
    )
    return (
        f"{site_url}/api/method/frappe.utils.print_format.download_pdf"
        f"?doctype=Sales+Invoice&name={si.name}"
        f"&format={frappe.utils.quoted(format_name)}&no_letterhead=0"
    )


def _resolve_print_format(
    ee_account: str | None,
    fieldname: str,
    *,
    default: str,
) -> str:
    """Read the per-Account print format override, fall back to default.

    Defaults to `default` when:
      - ee_account is None
      - field doesn't exist (patch not yet applied)
      - field is blank
      - lookup raises
    """
    if not ee_account:
        return default
    try:
        value = frappe.db.get_value("EasyEcom Account", ee_account, fieldname)
    except Exception:
        return default
    return str(value).strip() if value else default
