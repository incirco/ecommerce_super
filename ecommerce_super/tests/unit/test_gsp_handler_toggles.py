"""§11.5.1 Mode 1 — gsp_mint_einvoice / gsp_mint_ewaybill toggle tests.

The two Check fields on EasyEcom Account let an FDE disable NIC IRP /
NIC EWB calls per-account, while still using Custom GSP for SI creation
and PDF download. These tests cover the gating logic in gsp_handler's
mint_irn_for_si and mint_eway_for_si.

We mock frappe primitives so tests run without a bench. The handlers
read the toggle via frappe.db.get_value("EasyEcom Account", name, field).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2b_sales.gsp_handler import (
    _resolve_eway_pdf_url,
    _resolve_invoice_pdf_url,
    _resolve_print_format,
    _should_mint_einvoice,
    _should_mint_ewaybill,
    mint_eway_for_si,
    mint_irn_for_si,
)


# ============================================================
# Helpers
# ============================================================


def _fake_si(
    *,
    name: str = "ACC-SINV-2026-00001",
    irn: str = "",
    ewaybill: str = "",
    docstatus: int = 0,
    ecs_easyecom_invoice_id: str = "EE-INV-123",
) -> MagicMock:
    """Build a MagicMock SI doc with the fields the handler reads."""
    si = MagicMock()
    si.name = name
    si.docstatus = docstatus
    state = {
        "irn": irn,
        "ewaybill": ewaybill,
        "ack_no": "",
        "ack_dt": None,
        "signed_qr_code": "",
        "ecs_easyecom_invoice_id": ecs_easyecom_invoice_id,
        "e_waybill_validity": None,
        "mode_of_transport": "",
        "vehicle_no": "",
        "vehicle_type": "",
        "transporter_gst_no": "",
        "transporter_name": "",
    }
    si.get.side_effect = lambda key, default=None: state.get(key, default)
    si.flags = MagicMock()

    def _submit():
        si.docstatus = 1

    si.submit.side_effect = _submit
    si.reload = MagicMock()
    return si


# ============================================================
# Toggle helper unit tests
# ============================================================


class TestShouldMintEInvoice(unittest.TestCase):

    def test_returns_true_when_account_is_none(self):
        self.assertTrue(_should_mint_einvoice(None))

    def test_returns_true_when_account_is_empty_string(self):
        self.assertTrue(_should_mint_einvoice(""))

    def test_returns_true_when_toggle_field_returns_1(self):
        with patch("frappe.db.get_value", return_value=1):
            self.assertTrue(_should_mint_einvoice("Thuraya Fashion"))

    def test_returns_false_when_toggle_field_returns_0(self):
        with patch("frappe.db.get_value", return_value=0):
            self.assertFalse(_should_mint_einvoice("Thuraya Fashion"))

    def test_returns_true_when_field_missing(self):
        # patch not applied yet → get_value returns None
        with patch("frappe.db.get_value", return_value=None):
            self.assertTrue(_should_mint_einvoice("Thuraya Fashion"))

    def test_returns_true_when_db_lookup_raises(self):
        with patch("frappe.db.get_value", side_effect=RuntimeError("boom")):
            self.assertTrue(_should_mint_einvoice("Thuraya Fashion"))


class TestShouldMintEwaybill(unittest.TestCase):

    def test_returns_true_when_account_is_none(self):
        self.assertTrue(_should_mint_ewaybill(None))

    def test_returns_true_when_toggle_field_returns_1(self):
        with patch("frappe.db.get_value", return_value=1):
            self.assertTrue(_should_mint_ewaybill("Thuraya Fashion"))

    def test_returns_false_when_toggle_field_returns_0(self):
        with patch("frappe.db.get_value", return_value=0):
            self.assertFalse(_should_mint_ewaybill("Thuraya Fashion"))

    def test_returns_true_when_field_missing(self):
        with patch("frappe.db.get_value", return_value=None):
            self.assertTrue(_should_mint_ewaybill("Thuraya Fashion"))


# ============================================================
# mint_irn_for_si — toggle gating
# ============================================================


class TestMintIrnToggle(unittest.TestCase):

    def test_toggle_off_submits_si_but_skips_nic_call(self):
        """gsp_mint_einvoice OFF: SI is submitted (GL impact happens),
        but generate_e_invoice is NEVER called. Response has empty IRN."""
        si = _fake_si(docstatus=0)
        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=0),  # toggle OFF
            patch(
                "india_compliance.gst_india.utils.e_invoice.generate_e_invoice",
            ) as mock_generate,
        ):
            response = mint_irn_for_si(
                "ACC-SINV-2026-00001", ee_account="Thuraya Fashion",
            )

        mock_generate.assert_not_called()
        si.submit.assert_called_once()
        self.assertEqual(response["irn"], "")
        self.assertEqual(response["ack_number"], "")
        self.assertEqual(response["irn_qr"], "")
        self.assertEqual(response["erp_invoice_num"], "ACC-SINV-2026-00001")
        # PDF URL still populated — Custom GSP's primary purpose
        self.assertIn("Sales+Invoice", response["invoice_pdf"])

    def test_toggle_on_calls_generate_e_invoice(self):
        """gsp_mint_einvoice ON: generate_e_invoice IS called."""
        si = _fake_si(docstatus=0)
        # After generate_e_invoice + reload, the SI should look like
        # it has an IRN.
        irn_after = "1234567890123456789012345678901234567890123456789012345678901234"

        def _reload_with_irn():
            current = si.get.side_effect
            new_state = {
                "irn": irn_after,
                "ack_no": "112010012345678",
                "ack_dt": None,
                "signed_qr_code": "qrqrqr",
                "ecs_easyecom_invoice_id": "EE-INV-123",
            }
            si.get.side_effect = (
                lambda key, default=None: new_state.get(key, current(key, default))
            )

        si.reload.side_effect = _reload_with_irn

        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=1),  # toggle ON
            patch(
                "india_compliance.gst_india.utils.e_invoice.generate_e_invoice",
            ) as mock_generate,
        ):
            response = mint_irn_for_si(
                "ACC-SINV-2026-00001", ee_account="Thuraya Fashion",
            )

        mock_generate.assert_called_once_with(
            docname="ACC-SINV-2026-00001", throw=True, force=False,
        )
        self.assertEqual(response["irn"], irn_after)
        self.assertEqual(response["ack_number"], "112010012345678")

    def test_toggle_off_with_idempotent_cached_irn_returns_cached(self):
        """If SI already has IRN, return cached regardless of toggle."""
        cached_irn = "1" * 64
        si = _fake_si(docstatus=1, irn=cached_irn)
        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=0),  # toggle OFF
            patch(
                "india_compliance.gst_india.utils.e_invoice.generate_e_invoice",
            ) as mock_generate,
        ):
            response = mint_irn_for_si(
                "ACC-SINV-2026-00001", ee_account="Thuraya Fashion",
            )

        mock_generate.assert_not_called()
        si.submit.assert_not_called()
        self.assertEqual(response["irn"], cached_irn)

    def test_no_account_passed_defaults_to_minting(self):
        """ee_account=None → behaviour falls back to mint (pre-toggle default)."""
        si = _fake_si(docstatus=0)
        irn_after = "9" * 64

        def _reload_with_irn():
            new_state = {
                "irn": irn_after,
                "ack_no": "",
                "ack_dt": None,
                "signed_qr_code": "",
                "ecs_easyecom_invoice_id": "EE-INV-123",
            }
            si.get.side_effect = lambda key, default=None: new_state.get(key, default)

        si.reload.side_effect = _reload_with_irn

        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch(
                "india_compliance.gst_india.utils.e_invoice.generate_e_invoice",
            ) as mock_generate,
        ):
            mint_irn_for_si("ACC-SINV-2026-00001")  # no ee_account

        mock_generate.assert_called_once()


# ============================================================
# mint_eway_for_si — toggle gating
# ============================================================


class TestMintEwaybillToggle(unittest.TestCase):

    def _transport_values(self) -> dict:
        return {
            "transporter_gst_no": "29ABCDE1234F1Z5",
            "transporter_name": "Delhivery",
            "vehicle_no": "KA01AB1234",
            "vehicle_type": "Regular",
            "mode_of_transport": "Road",
            "lr_no": "LR-001",
        }

    def test_toggle_off_skips_nic_call_and_echoes_transport_fields(self):
        """gsp_mint_ewaybill OFF: NO generate_e_waybill call. Response
        carries empty eway_bill_number but echoes transport_* from
        request."""
        si = _fake_si(docstatus=1)
        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=0),  # toggle OFF
            patch(
                "india_compliance.gst_india.utils.e_waybill.generate_e_waybill",
            ) as mock_generate,
        ):
            response = mint_eway_for_si(
                "ACC-SINV-2026-00001",
                transport_values=self._transport_values(),
                ee_account="Thuraya Fashion",
            )

        mock_generate.assert_not_called()
        self.assertEqual(response["eway_bill_number"], "")
        self.assertEqual(response["eway_bill_date"], "")
        self.assertEqual(response["eway_bill_pdf"], "")
        # transport fields echoed from request
        self.assertEqual(response["vehicle_number"], "KA01AB1234")
        self.assertEqual(response["transporter_name"], "Delhivery")
        self.assertEqual(response["transport_mode"], "Road")

    def test_toggle_on_calls_generate_e_waybill(self):
        si = _fake_si(docstatus=1)
        ewb_after = "121234567890"

        def _reload_with_ewb():
            new_state = {
                "ewaybill": ewb_after,
                "ecs_easyecom_invoice_id": "EE-INV-123",
                "e_waybill_validity": None,
            }
            si.get.side_effect = lambda key, default=None: new_state.get(key, default)

        si.reload.side_effect = _reload_with_ewb

        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=1),  # toggle ON
            patch(
                "india_compliance.gst_india.utils.e_waybill.generate_e_waybill",
            ) as mock_generate,
        ):
            response = mint_eway_for_si(
                "ACC-SINV-2026-00001",
                transport_values=self._transport_values(),
                ee_account="Thuraya Fashion",
            )

        mock_generate.assert_called_once()
        self.assertEqual(response["eway_bill_number"], ewb_after)

    def test_toggle_off_with_cached_ewaybill_returns_cached(self):
        """Idempotency: pre-existing ewaybill → return cached
        regardless of toggle."""
        cached_ewb = "111111111111"
        si = _fake_si(docstatus=1, ewaybill=cached_ewb)
        with (
            patch("frappe.get_doc", return_value=si),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=0),  # toggle OFF
            patch(
                "india_compliance.gst_india.utils.e_waybill.generate_e_waybill",
            ) as mock_generate,
        ):
            response = mint_eway_for_si(
                "ACC-SINV-2026-00001",
                transport_values=self._transport_values(),
                ee_account="Thuraya Fashion",
            )

        mock_generate.assert_not_called()
        self.assertEqual(response["eway_bill_number"], cached_ewb)


# ============================================================
# Print format resolution
# ============================================================


class TestResolvePrintFormat(unittest.TestCase):

    def test_default_when_account_is_none(self):
        result = _resolve_print_format(None, "gsp_print_format", default="Standard")
        self.assertEqual(result, "Standard")

    def test_default_when_field_is_blank(self):
        with patch("frappe.db.get_value", return_value=""):
            result = _resolve_print_format(
                "Thuraya Fashion", "gsp_print_format", default="Standard",
            )
        self.assertEqual(result, "Standard")

    def test_default_when_field_is_none(self):
        with patch("frappe.db.get_value", return_value=None):
            result = _resolve_print_format(
                "Thuraya Fashion", "gsp_print_format", default="Standard",
            )
        self.assertEqual(result, "Standard")

    def test_returns_custom_when_set(self):
        with patch("frappe.db.get_value", return_value="GST Tax Invoice"):
            result = _resolve_print_format(
                "Thuraya Fashion", "gsp_print_format", default="Standard",
            )
        self.assertEqual(result, "GST Tax Invoice")

    def test_strips_whitespace(self):
        with patch("frappe.db.get_value", return_value="  GST Tax Invoice  "):
            result = _resolve_print_format(
                "Thuraya Fashion", "gsp_print_format", default="Standard",
            )
        self.assertEqual(result, "GST Tax Invoice")

    def test_default_when_lookup_raises(self):
        with patch("frappe.db.get_value", side_effect=RuntimeError("boom")):
            result = _resolve_print_format(
                "Thuraya Fashion", "gsp_print_format", default="Standard",
            )
        self.assertEqual(result, "Standard")


class TestResolveInvoicePdfUrl(unittest.TestCase):

    def _patches(self, format_lookup_return):
        return (
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=format_lookup_return),
        )

    def test_uses_standard_when_account_has_no_override(self):
        si = _fake_si(name="ACC-SINV-2026-00099")
        get_url_p, get_value_p = self._patches(None)
        with get_url_p, get_value_p:
            url = _resolve_invoice_pdf_url(si, ee_account="Thuraya Fashion")
        self.assertIn("format=Standard", url)
        self.assertIn("name=ACC-SINV-2026-00099", url)

    def test_uses_custom_format_when_set(self):
        si = _fake_si(name="ACC-SINV-2026-00099")
        get_url_p, get_value_p = self._patches("GST Tax Invoice")
        with get_url_p, get_value_p:
            url = _resolve_invoice_pdf_url(si, ee_account="Thuraya Fashion")
        # URL-encoded space → %20
        self.assertIn("format=GST%20Tax%20Invoice", url)

    def test_no_account_falls_back_to_standard(self):
        si = _fake_si(name="ACC-SINV-2026-00099")
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
            return_value="https://site.example",
        ):
            url = _resolve_invoice_pdf_url(si)
        self.assertIn("format=Standard", url)


class TestResolveEwayPdfUrl(unittest.TestCase):

    def test_uses_ewaybill_default_when_no_override(self):
        si = _fake_si(name="ACC-SINV-2026-00099")
        with (
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value=None),
        ):
            url = _resolve_eway_pdf_url(si, ee_account="Thuraya Fashion")
        # 'e-Waybill' contains a hyphen; quoted preserves it
        self.assertIn("format=e-Waybill", url)

    def test_uses_custom_eway_format_when_set(self):
        si = _fake_si(name="ACC-SINV-2026-00099")
        with (
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.gsp_handler.frappe.utils.get_url",
                return_value="https://site.example",
            ),
            patch("frappe.db.get_value", return_value="MyCo EWB"),
        ):
            url = _resolve_eway_pdf_url(si, ee_account="Thuraya Fashion")
        self.assertIn("format=MyCo%20EWB", url)


if __name__ == "__main__":
    unittest.main()
