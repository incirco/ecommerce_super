"""gh#214-followup — variance-swallow fix in gsp_handler.

Regression background: `find_or_create_si_for_gsp` previously caught
`InvoiceMirrorVariance` and returned the just-created SI as if success.
That muffled the variance signal — a wrong-totalled SI shipped as an
invoice to EE (live: SO-2610405 → SI dropped ₹180 IGST, variance-check
fired but the exception was silently caught).

Post-fix:
  1. Comment posted on the Draft SI naming the variance
  2. GSPHandlerError raised — EE receives an error response
  3. Under-billed SI stays in Draft awaiting FDE review

These tests lock (1)+(2)+(3) so a future refactor can't re-introduce
the swallow.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales import gsp_handler as mod
from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
    InvoiceMirrorVariance,
)


def _ee_row():
    return {
        "invoice_id": "EE-INV-9999",
        "reference_code": "SO-VARIANCE-TEST",
        "total_amount": 1000.0,
    }


class TestVarianceNoLongerSwallowed(unittest.TestCase):
    """The core contract: when the mirror raises InvoiceMirrorVariance,
    the handler MUST raise GSPHandlerError — not return the SI.

    Note on the SI-lookup mock pattern used below:
      `find_or_create_si_for_gsp` queries `Sales Invoice` with the
      invoice_id filter TWICE — once as path-1 idempotency check
      (miss = None, so we proceed to mirror), and again in the
      variance except-branch (hit = SI name, so we post a comment).
      Tests differentiate the two calls by counting Sales Invoice
      lookups specifically.
    """

    def _si_lookup_stub(self, *, post_variance_hit: str | None = "SI-DRAFT-XYZ"):
        """Return a get_value side_effect that misses on the first
        Sales Invoice lookup (path-1 idempotency), then returns
        `post_variance_hit` on subsequent Sales Invoice lookups
        (the except-branch lookup after mirror raised)."""
        state = {"si_lookups": 0}

        def _side_effect(doctype, filters=None, field=None, **_kw):
            if doctype == "Sales Invoice":
                state["si_lookups"] += 1
                # First SI lookup = path-1 miss; subsequent = post-variance hit
                return None if state["si_lookups"] == 1 else post_variance_hit
            if doctype == "EasyEcom B2B Order Map":
                # Called with as_dict=True + multiple fields at path-2
                # check; called scalar at path-3 create. Return the
                # dict shape (both call sites tolerate a dict).
                if isinstance(field, list):
                    return {"name": "MAP-VAR", "sales_invoice": None}
                return "MAP-VAR"
            return None
        return _side_effect

    def _map_doc(self):
        m = MagicMock()
        m.name = "MAP-VAR"
        m.get = lambda k, d=None: None
        return m

    def test_variance_raised_by_mirror_propagates_as_gsp_handler_error(self):
        """The bug fix. Previously the handler caught, looked up the SI,
        and returned it. Now the handler surfaces the variance."""
        with (
            patch.object(mod.frappe.db, "get_value",
                         side_effect=self._si_lookup_stub()),
            patch.object(mod.frappe, "get_doc", return_value=self._map_doc()),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                side_effect=InvoiceMirrorVariance(
                    "SI SI-DRAFT-XYZ total ₹1200 vs EE total ₹1000 — +20.0% "
                    "variance exceeds 1% threshold."
                ),
            ),
            patch.object(mod, "_post_variance_comment_on_si"),
            self.assertRaises(mod.GSPHandlerError) as ctx,
        ):
            mod.find_or_create_si_for_gsp(
                ee_row=_ee_row(), ee_account="EE-ACC-01",
            )
        msg = str(ctx.exception)
        self.assertIn("variance exceeded threshold", msg)
        self.assertIn("under-billed", msg)
        self.assertIn("SI left in Draft", msg)

    def test_comment_posted_on_si_when_variance_fires(self):
        """When variance fires and an SI exists in Draft, a Comment
        must land on the SI so the FDE sees WHY it's in Draft."""
        post_comment_mock = MagicMock()

        with (
            patch.object(mod.frappe.db, "get_value",
                         side_effect=self._si_lookup_stub(
                             post_variance_hit="SI-DRAFT-VAR")),
            patch.object(mod.frappe, "get_doc", return_value=self._map_doc()),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                side_effect=InvoiceMirrorVariance("test variance detail"),
            ),
            patch.object(mod, "_post_variance_comment_on_si", post_comment_mock),
            self.assertRaises(mod.GSPHandlerError),
        ):
            mod.find_or_create_si_for_gsp(
                ee_row=_ee_row(), ee_account="EE-ACC-01",
            )

        post_comment_mock.assert_called_once()
        args = post_comment_mock.call_args.args
        self.assertEqual(args[0], "SI-DRAFT-VAR")
        self.assertEqual(args[1], "EE-INV-9999")
        self.assertIn("test variance detail", args[2])

    def test_variance_still_raises_even_when_si_lookup_returns_none(self):
        """Defensive: if the mirror raised before the SI was flushed
        (rare — insert() completes before variance check), the SI
        lookup returns None on BOTH calls. The handler must STILL
        raise; helper isn't called (no SI to comment on)."""
        with (
            patch.object(mod.frappe.db, "get_value",
                         side_effect=self._si_lookup_stub(post_variance_hit=None)),
            patch.object(mod.frappe, "get_doc", return_value=self._map_doc()),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror."
                "mirror_si_from_ee_response",
                side_effect=InvoiceMirrorVariance("no SI to link"),
            ),
            patch.object(mod, "_post_variance_comment_on_si") as post_comment,
            self.assertRaises(mod.GSPHandlerError),
        ):
            mod.find_or_create_si_for_gsp(
                ee_row=_ee_row(), ee_account="EE-ACC-01",
            )
        post_comment.assert_not_called()


class TestPostVarianceCommentHelper(unittest.TestCase):
    """The _post_variance_comment_on_si helper — never raises even on
    Frappe failure, so the outer throw is never muffled."""

    def test_adds_comment_with_variance_details_and_ee_invoice_id(self):
        fake_si = MagicMock()
        with patch.object(mod.frappe, "get_doc", return_value=fake_si):
            mod._post_variance_comment_on_si(
                si_name="SI-COMMENT-TEST",
                ee_invoice_id="EE-INV-42",
                variance_msg="dropped ₹180 IGST",
            )
        fake_si.add_comment.assert_called_once()
        _positional, kwargs = fake_si.add_comment.call_args
        # First positional is "Comment" type, second is text; may be
        # passed either way. Check the text substance either shape.
        args = fake_si.add_comment.call_args.args
        text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
        self.assertIn("dropped ₹180 IGST", text)
        self.assertIn("EE-INV-42", text)
        self.assertIn("Draft", text)

    def test_never_raises_when_get_doc_fails(self):
        """A missing SI must not muffle the outer variance error."""
        with patch.object(
            mod.frappe, "get_doc",
            side_effect=Exception("SI vanished"),
        ):
            # Must NOT raise
            mod._post_variance_comment_on_si(
                si_name="SI-GONE",
                ee_invoice_id="EE-INV-1",
                variance_msg="test",
            )

    def test_never_raises_when_add_comment_fails(self):
        """A Frappe add_comment failure must not muffle the outer
        variance error either."""
        fake_si = MagicMock()
        fake_si.add_comment.side_effect = Exception("comment insert failed")
        with patch.object(mod.frappe, "get_doc", return_value=fake_si):
            # Must NOT raise
            mod._post_variance_comment_on_si(
                si_name="SI-01",
                ee_invoice_id="EE-INV-1",
                variance_msg="test",
            )


if __name__ == "__main__":
    unittest.main()
