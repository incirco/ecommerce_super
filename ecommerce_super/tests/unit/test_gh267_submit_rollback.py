"""gh#267 — /einvoice/update must NOT leave a corrupt half-submitted SI.

Root cause: `mint_irn_for_si` catches a failed `si.submit()` and re-raises
GSPHandlerError; `_einvoice_handler_impl` then RETURNS a 422 to EE. Because
a response is returned (not an unhandled exception), Frappe commits the
partial submit at request teardown — leaving an SI with docstatus=1 but only
some Stock Ledger entries posted (billed full, stock partial). The fix wraps
the submit in a named savepoint and rolls back to it on any submit failure,
so the Draft SI (already committed by find_or_create_si_for_gsp) survives but
the partial submit is discarded.

Secondary fix: `_log_inbound_gsp_failure` derived `company` from
EasyEcom Account, which has no `company` field → OperationalError aborted the
whole Sync Record write. It now derives company from the Sales Order.

These mock frappe primitives so the tests run without a bench.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
    GSPHandlerError,
    mint_irn_for_si,
)

SAVEPOINT = "ecs_gsp_si_submit"


def _fake_si(*, docstatus: int = 0, irn: str = "", submit_raises: bool = False):
    """MagicMock SI doc with the fields mint_irn_for_si reads."""
    si = MagicMock()
    si.name = "ACC-SINV-2026-00042"
    si.docstatus = docstatus
    state = {"irn": irn, "ack_no": "", "ack_dt": None, "signed_qr_code": ""}
    si.get.side_effect = lambda key, default=None: state.get(key, default)
    si.flags = MagicMock()

    def _submit():
        if submit_raises:
            # Mimic ERPNext's NegativeStockError surfacing during submit.
            raise Exception("NegativeStockError: 1.0 units needed")
        si.docstatus = 1

    si.submit.side_effect = _submit
    si.reload = MagicMock()
    return si


class TestSubmitRollback(unittest.TestCase):

    def test_failed_submit_rolls_back_to_savepoint_and_raises(self):
        """A submit failure must roll back to the named savepoint (not
        leave a committed partial submit) and surface GSPHandlerError."""
        si = _fake_si(docstatus=0, submit_raises=True)
        with (
            patch("frappe.get_doc", return_value=si),
            patch("frappe.db.savepoint") as savepoint,
            patch("frappe.db.rollback") as rollback,
        ):
            with self.assertRaises(GSPHandlerError) as ctx:
                mint_irn_for_si("ACC-SINV-2026-00042", ee_account="ACC")

        # savepoint created before submit, rolled back to on failure.
        savepoint.assert_called_once_with(SAVEPOINT)
        rollback.assert_called_once_with(save_point=SAVEPOINT)
        self.assertIn("could not be submitted", str(ctx.exception))

    def test_rollback_falls_back_to_full_rollback_if_savepoint_gone(self):
        """If rollback-to-savepoint itself fails (savepoint released by an
        inner commit), fall back to a full rollback — never let the submit
        failure surface without an undo."""
        si = _fake_si(docstatus=0, submit_raises=True)

        def _rollback(*args, **kwargs):
            if kwargs.get("save_point"):
                raise Exception("savepoint does not exist")
            return None  # full rollback succeeds

        with (
            patch("frappe.get_doc", return_value=si),
            patch("frappe.db.savepoint"),
            patch("frappe.db.rollback", side_effect=_rollback) as rollback,
        ):
            with self.assertRaises(GSPHandlerError):
                mint_irn_for_si("ACC-SINV-2026-00042", ee_account="ACC")

        # Both the targeted rollback AND the full-rollback fallback fire.
        self.assertIn(call(save_point=SAVEPOINT), rollback.mock_calls)
        self.assertIn(call(), rollback.mock_calls)

    def test_successful_submit_does_not_roll_back(self):
        """On a clean submit the savepoint is set but NEVER rolled back;
        the mint proceeds (toggle OFF path returns the assembled response)."""
        si = _fake_si(docstatus=0, submit_raises=False)
        sentinel = {"irn": "", "invoice_pdf": "http://pdf"}
        with (
            patch("frappe.get_doc", return_value=si),
            patch("frappe.db.savepoint") as savepoint,
            patch("frappe.db.rollback") as rollback,
            patch("frappe.db.get_value", return_value=0),  # mint toggle OFF
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                "_assemble_irn_response",
                return_value=sentinel,
            ),
        ):
            out = mint_irn_for_si("ACC-SINV-2026-00042", ee_account="ACC")

        savepoint.assert_called_once_with(SAVEPOINT)
        rollback.assert_not_called()
        self.assertEqual(out, sentinel)

    def test_already_submitted_si_skips_savepoint(self):
        """A retry on an already-submitted SI (docstatus=1) must not touch
        the submit block at all — no savepoint, no rollback."""
        si = _fake_si(docstatus=1, submit_raises=False)
        with (
            patch("frappe.get_doc", return_value=si),
            patch("frappe.db.savepoint") as savepoint,
            patch("frappe.db.rollback") as rollback,
            patch("frappe.db.get_value", return_value=0),  # mint toggle OFF
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler."
                "_assemble_irn_response",
                return_value={"irn": ""},
            ),
        ):
            mint_irn_for_si("ACC-SINV-2026-00042", ee_account="ACC")

        savepoint.assert_not_called()
        rollback.assert_not_called()
        si.submit.assert_not_called()


class TestLogInboundFailureCompany(unittest.TestCase):
    """The failure logger must derive company from the Sales Order and
    never touch a non-existent EasyEcom Account.company column."""

    def test_company_from_sales_order_not_easyecom_account(self):
        from ecommerce_super.easyecom.api.gsp import _log_inbound_gsp_failure

        get_value_calls = []

        def _get_value(doctype, name, field=None, *a, **k):
            get_value_calls.append((doctype, field))
            if doctype == "Sales Order" and field == "company":
                return "Acme Pvt Ltd"
            return None

        sr = MagicMock()
        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.db.get_value", side_effect=_get_value),
            patch("frappe.new_doc", return_value=sr),
            patch("frappe.db.commit"),
            patch("frappe.log_error"),
            patch("frappe.get_traceback", return_value="tb"),
        ):
            _log_inbound_gsp_failure(
                endpoint="/einvoice/update",
                ee_row={"reference_code": "SO-1", "invoice_id": "INV-1"},
                ee_account="ACC",
                reason="NegativeStockError",
                http_status=422,
            )

        # Never queries the (non-existent) EasyEcom Account company column.
        self.assertNotIn(("EasyEcom Account", "company"), get_value_calls)
        # Company came from the Sales Order and the SR row was written.
        sr.update.assert_called_once()
        self.assertEqual(sr.update.call_args[0][0]["company"], "Acme Pvt Ltd")
        sr.insert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
