"""§11.5.1 Mode 1 — Custom GSP endpoints exposed to EE.

When the EE-side FDE clicks "Generate Invoice", EE calls our endpoints
as if we were a third-party Custom GSP. We:
  1. Resolve the EE order to an ERPNext Sales Invoice (find or create)
  2. Submit the SI (India Compliance requires submitted SI to mint IRN)
  3. Call India Compliance's generate_e_invoice / generate_e_waybill
  4. Return the IRN/QR/PDF in the response body — bytewise matching
     EE's contract

Three endpoints (all whitelisted with allow_guest=True since EE isn't
a Frappe user — auth is via Basic/Bearer headers, not Frappe session):

  POST /api/method/...gsp.gettoken          — Basic → Bearer mint
  POST /api/method/...gsp.einvoice_update   — Bearer + EE order JSON → IRN
  POST /api/method/...gsp.ewaybill_update   — Bearer + EE order JSON → eway

Failure modes:
  - Auth failure → HTTP 401
  - Payload validation (missing reference_code, etc.) → HTTP 422
  - SI find/create failure (missing Customer/Item Map) → HTTP 422 + clear msg
  - India Compliance NIC rejection → HTTP 422 with NIC error detail
  - NIC timeout / 5xx → HTTP 502 with retry suggestion

Idempotency:
  - Keyed on EE's `invoice_id` (always populated, the natural key)
  - Re-hit with same invoice_id → return cached IRN, NEVER re-mint
    (re-minting on NIC IRP creates a duplicate IRN that cannot be
    deleted — the ONLY remediation is calling NIC support)
"""
from __future__ import annotations

import json
from typing import Any

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.gsp_auth import (
    EasyEcomGSPAuthError,
    issue_bearer,
    validate_basic_auth,
    validate_bearer,
)


# ============================================================
# /gettoken — Basic auth → Bearer mint
# ============================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
def gettoken() -> dict[str, Any]:
    """EE's /gettoken contract:

    Headers:
        Authorization: Basic <base64-encoded user:secret>

    Returns:
        {"status": 200, "access_token": "<token>",
         "token_type": "Bearer", "expires_in": 3600}

    On auth failure:
        {"status": 401, "message": "<reason>"}
    """
    try:
        account_name = validate_basic_auth(
            frappe.get_request_header("Authorization")
        )
    except EasyEcomGSPAuthError as exc:
        frappe.response.http_status_code = 401
        return {"status": 401, "message": str(exc)}

    try:
        minted = issue_bearer(
            account_name,
            issued_to_ip=frappe.local.request_ip if frappe.local else None,
        )
    except Exception as exc:
        frappe.log_error(
            title=f"§11.5.1 /gettoken: token mint failed for {account_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        frappe.response.http_status_code = 500
        return {"status": 500, "message": "Token mint failed."}

    return {
        "status": 200,
        "access_token": minted["token"],
        "token_type": "Bearer",
        "expires_in": minted["expires_in"],
    }


# ============================================================
# /einvoice/update — Bearer + EE order JSON → IRN
# ============================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
def einvoice_update() -> dict[str, Any]:
    """EE's /einvoice/update contract:

    Headers:
        Authorization: Bearer <token from /gettoken>
        Content-Type: application/json

    Body:
        {"orders": [<EE order object — same shape as getOrderDetails>]}

    Returns on success:
        {"status": 200, "message": "Invoice fetched successfully",
         "data": {"invoice_details": {
             "invoice_id": "<EE's invoice_id>",
             "erp_invoice_num": "<our SI docname>",
             "irn": "<64-char IRN from NIC IRP>",
             "ack_number": "<NIC ack number>",
             "ack_date": "<ISO timestamp>",
             "invoice_pdf": "<URL>",
             "irn_qr": "<base64 or text>",
             "invoice_base64": "<base64 PDF or empty>"
         }}}

    On auth failure:        HTTP 401
    On payload errors:      HTTP 422
    On NIC mint failure:    HTTP 422 with NIC error in body
    """
    try:
        ee_account = validate_bearer(
            frappe.get_request_header("Authorization")
        )
    except EasyEcomGSPAuthError as exc:
        frappe.response.http_status_code = 401
        return {"status": 401, "message": str(exc)}

    try:
        body = frappe.request.get_json() or {}
    except Exception as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": f"Invalid JSON body: {exc}"}

    orders = body.get("orders") or []
    if not isinstance(orders, list) or not orders:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": "Body must have orders[] array with at least one row.",
        }

    ee_row = orders[0]
    return _einvoice_handler(ee_row=ee_row, ee_account=ee_account)


# ============================================================
# /ewaybill/update — Bearer + EE order JSON → eway
# ============================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
def ewaybill_update() -> dict[str, Any]:
    """EE's /ewaybill/update contract:

    Headers:
        Authorization: Bearer <token from /gettoken>
        Content-Type: application/json

    Body:
        {"orders": {<single EE order object with transport_* fields>}}

    Note: unlike /einvoice/update, EE sends `orders` as a SINGLE OBJECT
    (not an array). The shape diff is per EE's contract — we accept
    both shapes defensively.

    Returns on success:
        {"status": 200, "message": "E-Way Bill fetched successfully",
         "data": {"invoice_details": {
             "invoice_id": "<EE's invoice_id>",
             "erp_invoice_num": "<our SI docname>",
             "eway_bill_number": "<12-digit>",
             "eway_bill_date": "<ISO>",
             "eway_bill_pdf": "<URL>",
             "transport_mode": "Road",
             "vehicle_number": "...",
             "vehicle_type": "...",
             "transporter_gst": "...",
             "transporter_name": "...",
             "eway_bill_base64": "<base64 PDF or empty>"
         }}}
    """
    try:
        ee_account = validate_bearer(
            frappe.get_request_header("Authorization")
        )
    except EasyEcomGSPAuthError as exc:
        frappe.response.http_status_code = 401
        return {"status": 401, "message": str(exc)}

    try:
        body = frappe.request.get_json() or {}
    except Exception as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": f"Invalid JSON body: {exc}"}

    orders = body.get("orders")
    if isinstance(orders, list) and orders:
        ee_row = orders[0]
    elif isinstance(orders, dict):
        ee_row = orders
    else:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": "Body must have orders as dict or non-empty array.",
        }

    return _ewaybill_handler(ee_row=ee_row, ee_account=ee_account)


# ============================================================
# Handler internals
# ============================================================


def _einvoice_handler(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Find/create SI, submit + mint IRN, return EE-shape response."""
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_or_create_si_for_gsp,
        GSPHandlerError,
        mint_irn_for_si,
    )

    try:
        si_name = find_or_create_si_for_gsp(
            ee_row=ee_row, ee_account=ee_account,
        )
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": str(exc)}

    try:
        irn_data = mint_irn_for_si(si_name, ee_account=ee_account)
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 422
        return {
            "status": 422,
            "message": str(exc),
            "data": {"invoice_details": {
                "invoice_id": str(ee_row.get("invoice_id") or ""),
                "erp_invoice_num": si_name,
            }},
        }
    except Exception as exc:
        frappe.log_error(
            title=f"§11.5.1 /einvoice mint failed for {si_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        frappe.response.http_status_code = 502
        return {
            "status": 502,
            "message": f"NIC IRP mint failed: {type(exc).__name__}",
        }

    return {
        "status": 200,
        "message": "Invoice fetched successfully",
        "data": {"invoice_details": irn_data},
    }


def _ewaybill_handler(*, ee_row: dict, ee_account: str) -> dict[str, Any]:
    """Find SI (must already exist from einvoice call), mint eway."""
    from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
        find_si_by_invoice_id,
        GSPHandlerError,
        mint_eway_for_si,
    )

    ee_invoice_id = str(ee_row.get("invoice_id") or "").strip()
    if not ee_invoice_id:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": "Body missing invoice_id."}

    try:
        si_name = find_si_by_invoice_id(ee_invoice_id)
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 409
        return {"status": 409, "message": str(exc)}

    transport_values = {
        "transporter_gst_no": ee_row.get("transporter_gst"),
        "transporter_name": ee_row.get("transporter_name"),
        "vehicle_no": ee_row.get("vehicle_number"),
        "vehicle_type": ee_row.get("vehicle_type"),
        "mode_of_transport": _resolve_transport_mode(ee_row),
        "lr_no": ee_row.get("transport_document_number"),
    }

    try:
        eway_data = mint_eway_for_si(
            si_name,
            transport_values=transport_values,
            ee_account=ee_account,
        )
    except GSPHandlerError as exc:
        frappe.response.http_status_code = 422
        return {"status": 422, "message": str(exc)}
    except Exception as exc:
        frappe.log_error(
            title=f"§11.5.1 /ewaybill mint failed for {si_name}",
            message=f"{type(exc).__name__}: {exc}",
        )
        frappe.response.http_status_code = 502
        return {
            "status": 502,
            "message": f"NIC EWB mint failed: {type(exc).__name__}",
        }

    return {
        "status": 200,
        "message": "E-Way Bill fetched successfully",
        "data": {"invoice_details": eway_data},
    }


def _resolve_transport_mode(ee_row: dict) -> str:
    """EE sends transport_mode as string ("1"=Road, "2"=Rail, "3"=Air,
    "4"=Ship) per their contract. India Compliance expects the named
    enum. Map per NIC EWB convention."""
    raw = str(ee_row.get("transport_mode") or "1").strip()
    return {
        "1": "Road", "2": "Rail", "3": "Air", "4": "Ship",
        "Road": "Road", "Rail": "Rail", "Air": "Air", "Ship": "Ship",
    }.get(raw, "Road")
