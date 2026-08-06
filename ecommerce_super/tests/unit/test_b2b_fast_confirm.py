"""§11.3.5 — Fast-confirm queue check unit tests.

Verifies the four shapes:
  - Queue finishes on first poll → backfill IDs immediately
  - Queue finishes on later poll (NEW → Finished)
  - Queue errors out → Map → Failed, error CSV parsed + SO surfaced (gh#254)
  - Queue still NEW after MAX_ATTEMPTS → timeout, fall through

Mocks getQueueStatus + getOrderDetails responses (we don't hit
EE in unit tests). The fast-confirm path is purely additive — if
it fails for any reason, the */5 polling cron + PR #101 backfill
is the safety net.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2b_sales.fast_confirm import (
    MAX_ATTEMPTS,
    POLL_INTERVAL_SEC,
    _parse_ee_error_csv,
    _surface_rejection_on_so,
    fast_confirm_new_b2b,
)


def _queue_response(status_id, message=None, notes=None, result_url=None):
    """Build a synthetic getQueueStatus response body."""
    return {
        "code": 200,
        "message": "Successful",
        "data": {
            "id": "176452494",
            "status_id": str(status_id),
            "message": message or {"1": "NEW", "3": "Finished", "4": "Error"}.get(str(status_id), "Unknown"),
            "notes": notes or "",
            "result": result_url or "",
            "upload_file": "https://ee-uploaded-files.s3.example.com/payload.csv",
            "process_time": "2026-06-28 14:00:00" if str(status_id) != "1" else "",
        },
    }


def _order_details_response(reference_code, order_id, invoice_id, suborder_id):
    """Build a synthetic /orders/V2/getOrderDetails response."""
    return {
        "data": [
            {
                "order_type_key": "businessorder",
                "reference_code": reference_code,
                "order_id": order_id,
                "invoice_id": invoice_id,
                "order_items": [{"suborder_id": suborder_id}],
                "last_update_date": "2026-06-28 14:00:00",
            },
        ],
    }


def _fake_map(name, sales_order, ee_order_id=None, ee_suborder_id=None, ee_invoice_id=None):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.ee_order_id = ee_order_id
    m.ee_suborder_id = ee_suborder_id
    m.ee_invoice_id = ee_invoice_id
    return m


class TestFastConfirmNewB2B(unittest.TestCase):

    def test_queue_finishes_on_first_poll_backfills_all_ids(self) -> None:
        """Best case: EE finishes the queue in <5s. First /getQueueStatus
        call returns Finished, we fetch getOrderDetails for full IDs,
        write to Map."""
        fake_client = MagicMock()
        # First call: getQueueStatus returns Finished
        # Second call: getOrderDetails returns the full row
        fake_client.get.side_effect = [
            _queue_response(
                3, "Finished",
                notes='{"order_id":561435048,"reference_code":"SO-001"}',
            ),
            _order_details_response("SO-001", 561435048, 657806781, 864797685),
        ]
        fake_map = _fake_map("ECS-B2B-SO-001", "SO-001")

        with (
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.EasyEcomClient", return_value=fake_client),
            patch("frappe.get_doc", return_value=fake_map),
            patch("frappe.db.set_value") as set_value,
            patch("frappe.db.commit"),
        ):
            result = fast_confirm_new_b2b(
                map_name="ECS-B2B-SO-001",
                queue_id="176452494",
                location_key="ve67814409744",
            )

        self.assertEqual(result["terminal_status_id"], "3")
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["backfilled"], {
            "ee_order_id": "561435048",
            "ee_suborder_id": "864797685",
            "ee_invoice_id": "657806781",
        })
        # set_value called once with the IDs.
        set_value.assert_called_once()

    def test_queue_progresses_new_to_finished_over_multiple_polls(self) -> None:
        """Realistic case: status_id=1 on first poll, then =3 on second.
        Confirms the loop continues until terminal status (not just 1
        attempt)."""
        fake_client = MagicMock()
        fake_client.get.side_effect = [
            _queue_response(1, "NEW"),  # first attempt — not done yet
            _queue_response(3, "Finished",
                notes='{"order_id":561435048,"reference_code":"SO-002"}'),
            _order_details_response("SO-002", 561435048, 657806781, 864797685),
        ]
        fake_map = _fake_map("ECS-B2B-SO-002", "SO-002")

        with (
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.EasyEcomClient", return_value=fake_client),
            patch("frappe.get_doc", return_value=fake_map),
            patch("frappe.db.set_value"),
            patch("frappe.db.commit"),
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.time.sleep"),
        ):
            result = fast_confirm_new_b2b(
                map_name="ECS-B2B-SO-002",
                queue_id="176452494",
                location_key="ve67814409744",
            )

        self.assertEqual(result["terminal_status_id"], "3")
        self.assertEqual(result["attempts"], 2)

    def test_queue_errors_marks_map_failed_with_error_csv(self) -> None:
        """gh#254: status_id=4 → Map status flips to **Failed** (not the old
        ambiguous "Drift"), the error CSV URL is captured on last_error, and
        the SO-facing surfacing helper is invoked. The CSV parse + surfacing
        are patched here (their behaviour is covered in dedicated tests);
        this locks the single deterministic Map write."""
        fake_client = MagicMock()
        fake_client.get.return_value = _queue_response(
            4, "Error", result_url="https://ee.example.com/error_report.csv",
        )
        fake_map = _fake_map("ECS-B2B-SO-003", "SO-003")

        with (
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.EasyEcomClient", return_value=fake_client),
            patch("frappe.get_doc", return_value=fake_map),
            patch("frappe.db.set_value") as set_value,
            patch("frappe.db.commit"),
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm._parse_ee_error_csv", return_value=None),
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm._surface_rejection_on_so") as surface,
        ):
            result = fast_confirm_new_b2b(
                map_name="ECS-B2B-SO-003",
                queue_id="176452494",
                location_key="ve67814409744",
            )

        self.assertEqual(result["terminal_status_id"], "4")
        self.assertEqual(result["error_csv_url"], "https://ee.example.com/error_report.csv")
        self.assertIsNone(result["backfilled"])
        # The single deterministic Map write: status=Failed + last_error w/ CSV.
        set_value.assert_called_once()
        update_args = set_value.call_args.args
        # Frappe's set_value signature: doctype, name, fieldname_or_dict, value
        self.assertEqual(update_args[2]["status"], "Failed")
        self.assertIn("error_report.csv", update_args[2]["last_error"])
        # The SO-facing surfacing (comment + Discrepancy) was invoked.
        surface.assert_called_once()

    def test_queue_still_processing_after_max_attempts_times_out(self) -> None:
        """Worst case: queue still NEW after 30s ceiling. Return with
        timed_out=True; downstream relies on */5 polling cron to
        eventually backfill."""
        fake_client = MagicMock()
        # Always return NEW
        fake_client.get.return_value = _queue_response(1, "NEW")

        with (
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.EasyEcomClient", return_value=fake_client),
            patch("frappe.db.set_value"),
            patch("frappe.db.commit"),
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.time.sleep"),
        ):
            result = fast_confirm_new_b2b(
                map_name="ECS-B2B-SO-004",
                queue_id="176452494",
                location_key="ve67814409744",
            )

        self.assertIsNone(result["terminal_status_id"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["attempts"], MAX_ATTEMPTS)
        self.assertIsNone(result["backfilled"])
        # All MAX_ATTEMPTS calls made
        self.assertEqual(fake_client.get.call_count, MAX_ATTEMPTS)

    def test_getOrderDetails_failure_after_finished_still_writes_notes_order_id(self) -> None:
        """Edge case: getQueueStatus says Finished, we extract order_id
        from notes, but getOrderDetails throws (transient). We should
        still write the notes order_id to the Map (better than null);
        polling cron picks up suborder + invoice later."""
        from ecommerce_super.easyecom.exceptions import EasyEcomAPIError

        fake_client = MagicMock()
        fake_client.get.side_effect = [
            _queue_response(
                3, "Finished",
                notes='{"order_id":561435048,"reference_code":"SO-005"}',
            ),
            EasyEcomAPIError("transient 503"),
        ]
        fake_map = _fake_map("ECS-B2B-SO-005", "SO-005")

        with (
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.EasyEcomClient", return_value=fake_client),
            patch("frappe.get_doc", return_value=fake_map),
            patch("frappe.db.set_value") as set_value,
            patch("frappe.db.commit"),
        ):
            result = fast_confirm_new_b2b(
                map_name="ECS-B2B-SO-005",
                queue_id="176452494",
                location_key="ve67814409744",
            )

        self.assertEqual(result["terminal_status_id"], "3")
        # We only got order_id from notes; suborder + invoice missing.
        self.assertEqual(result["backfilled"], {"ee_order_id": "561435048"})

    def test_transient_queue_status_exception_returns_with_exception_flag(self) -> None:
        """If the first /getQueueStatus call throws (network blip), we
        return immediately with `exception=True` so the caller's logger
        can capture it. Polling cron still backfills later."""
        from ecommerce_super.easyecom.exceptions import EasyEcomAPIError

        fake_client = MagicMock()
        fake_client.get.side_effect = EasyEcomAPIError("connection timeout")

        with (
            patch("ecommerce_super.easyecom.flows.b2b_sales.fast_confirm.EasyEcomClient", return_value=fake_client),
            patch("frappe.db.set_value"),
            patch("frappe.db.commit"),
        ):
            result = fast_confirm_new_b2b(
                map_name="ECS-B2B-SO-006",
                queue_id="176452494",
                location_key="ve67814409744",
            )

        self.assertTrue(result.get("exception"))
        self.assertFalse(result["timed_out"])
        self.assertIsNone(result["terminal_status_id"])
        self.assertIsNone(result["backfilled"])


class TestParseEeErrorCsv(unittest.TestCase):
    """gh#254 — best-effort parse of EE's error-report CSV into a
    human-readable "reason: sku, sku" summary. Never raises."""

    _CSV = (
        "OrderItemId,Sku,Quantity,Price,Message\n"
        "SO-2610684-line-30,FG05015-102-2XL,1,619.5,Tax Rate Not Set\n"
        "SO-2610684-line-46,FG05015-108-XL,1,619.5,Tax Rate Not Set\n"
    )

    def test_groups_skus_by_reason(self) -> None:
        resp = MagicMock()
        resp.text = self._CSV
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            summary = _parse_ee_error_csv("https://ee.example.com/err.csv")
        self.assertEqual(
            summary, "Tax Rate Not Set: FG05015-102-2XL, FG05015-108-XL"
        )

    def test_network_error_returns_none(self) -> None:
        with patch("requests.get", side_effect=RuntimeError("boom")):
            self.assertIsNone(_parse_ee_error_csv("https://ee.example.com/err.csv"))

    def test_empty_csv_returns_none(self) -> None:
        resp = MagicMock()
        resp.text = "OrderItemId,Sku,Quantity,Price,Message\n"
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            self.assertIsNone(_parse_ee_error_csv("https://ee.example.com/err.csv"))

    def test_caps_long_sku_list(self) -> None:
        rows = "".join(
            f"L{i},SKU-{i},1,10,Tax Rate Not Set\n" for i in range(25)
        )
        resp = MagicMock()
        resp.text = "OrderItemId,Sku,Quantity,Price,Message\n" + rows
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            summary = _parse_ee_error_csv("https://ee.example.com/err.csv")
        self.assertIn("(+5 more)", summary)


class TestSurfaceRejectionOnSo(unittest.TestCase):
    """gh#254 — a rejection must be visible where the user looks: a Sales
    Order timeline comment + a 'B2B Push Failed' Discrepancy."""

    def _run(self, *, already_commented=False):
        map_row = {
            "sales_order": "SO-2610684",
            "company": "Modern Marwar Private Limited",
            "easyecom_account": "Modern Marwar",
        }
        so_doc = MagicMock()
        with (
            patch("frappe.db.get_value", return_value=map_row),
            patch(
                "frappe.db.exists",
                return_value=("Comment-1" if already_commented else None),
            ),
            patch("frappe.get_doc", return_value=so_doc),
            patch("frappe.db.commit"),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy"
            ) as raise_disc,
        ):
            _surface_rejection_on_so(
                map_name="ECS-B2B-SO-2610684",
                error_msg="Error",
                parsed_summary="Tax Rate Not Set: FG05015-102-2XL, FG05015-108-XL",
                error_csv_url="https://ee.example.com/err.csv",
            )
        return so_doc, raise_disc

    def test_adds_so_comment_and_raises_discrepancy(self) -> None:
        so_doc, raise_disc = self._run(already_commented=False)
        so_doc.add_comment.assert_called_once()
        # The comment carries the failing SKUs.
        comment_text = so_doc.add_comment.call_args.kwargs.get("text") or (
            so_doc.add_comment.call_args.args[1]
            if len(so_doc.add_comment.call_args.args) > 1 else ""
        )
        self.assertIn("EasyEcom rejected this order", comment_text)
        self.assertIn("FG05015-102-2XL", comment_text)
        raise_disc.assert_called_once()
        self.assertEqual(raise_disc.call_args.kwargs["kind"], "B2B Push Failed")
        self.assertEqual(
            raise_disc.call_args.kwargs["reference_name"], "SO-2610684"
        )

    def test_dedupes_so_comment_when_already_present(self) -> None:
        so_doc, _ = self._run(already_commented=True)
        so_doc.add_comment.assert_not_called()

    def test_no_sales_order_is_a_noop(self) -> None:
        with (
            patch("frappe.db.get_value", return_value={"sales_order": None}),
            patch("frappe.get_doc") as get_doc,
        ):
            _surface_rejection_on_so(
                map_name="ECS-B2B-SO-X", error_msg="Error",
                parsed_summary=None, error_csv_url=None,
            )
        get_doc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
