"""Contract test helpers — load fixtures + boot a plausible request
context for the GSP endpoints.

Contract tests exercise the ACTUAL endpoint functions from
`ecommerce_super.easyecom.api.gsp` with real EE-captured payload
shapes. Downstream side effects (SI insert, IRN mint) are mocked at
the handler boundary — we're testing the ENDPOINT contract
(request-body parsing, response-envelope shape, auth-header handling,
error-code semantics), not the mirror machinery.

Any regression against a captured live shape → test fails → the PR
that introduced the regression can't ship without acknowledgment.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a fixture JSON by name (without the .json suffix).

    Every fixture carries a `_meta` block documenting where it came
    from and which incident it captures. That block is stripped before
    return so the fixture body matches what EE would send verbatim.
    """
    path = _FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Contract-test fixture not found: {path}. Check "
            f"tests/contract/gsp/fixtures/ for available names."
        )
    with path.open() as f:
        data = json.load(f)
    data.pop("_meta", None)
    return data


class _ResponseCapture:
    """Small container the request_context yields — tests read
    `.response` to get the dict the endpoint would have shipped to EE.

    The @_gsp_endpoint decorator writes the response to
    `frappe.local.response` via `_write_gsp_flat_response` and returns
    None to prevent Frappe's outer layer from re-wrapping. So we
    intercept `_write_gsp_flat_response` calls and stash the dict
    here for the test to assert on.
    """
    def __init__(self):
        self.response: dict | None = None
        self.http_status: int | None = None


@contextmanager
def request_context(
    body: dict,
    *,
    auth_header: str | None = "Bearer TESTTOKEN",
    ee_account: str = "TEST-EE-ACC-01",
    bypass_auth: bool = True,
    bypass_rate_limit: bool = True,
):
    """Set up a plausible request context: json body, auth header,
    account resolution. Yields a _ResponseCapture that the test can
    inspect after calling the endpoint.

    Bypasses auth (returns ee_account from validate_bearer) and rate
    limits by default so contract tests focus on request/response
    shape, not those separately-tested concerns.
    """
    from ecommerce_super.easyecom.api import gsp as gsp_mod

    capture = _ResponseCapture()

    def _capture_response(payload: dict, http_status: int):
        capture.response = payload
        capture.http_status = http_status

    patches = [
        # Make frappe.request.get_json() return the fixture body.
        patch.object(
            gsp_mod.frappe, "request",
            MagicMock(get_json=lambda: body),
        ),
        # Make _get_gsp_auth_header return the injected header.
        patch.object(
            gsp_mod, "_get_gsp_auth_header",
            return_value=auth_header,
        ),
        # Stub frappe.response so tests can inspect the http_status_code.
        patch.object(
            gsp_mod.frappe, "response",
            MagicMock(http_status_code=200),
        ),
        # THE KEY INTERCEPT — capture the response dict the endpoint
        # decorator would have written to frappe.local.response.
        patch.object(
            gsp_mod, "_write_gsp_flat_response",
            side_effect=_capture_response,
        ),
        # Stub inbound-call logging + failure logging so tests don't
        # try to write real DB rows.
        patch.object(gsp_mod, "_log_inbound_gsp_call"),
        patch.object(gsp_mod, "_log_inbound_gsp_failure"),
    ]
    if bypass_auth:
        # For einvoice/ewaybill (Bearer path) and gettoken (Basic
        # path) — return a stand-in account name.
        patches.append(patch.object(
            gsp_mod, "validate_bearer", return_value=ee_account,
        ))
        patches.append(patch.object(
            gsp_mod, "validate_basic_auth", return_value=ee_account,
        ))
    if bypass_rate_limit:
        patches.append(patch.object(gsp_mod, "_enforce_gsp_rate_limit"))

    for p in patches:
        p.start()
    try:
        yield capture
    finally:
        for p in patches:
            p.stop()
