"""Stage 6 tests for §8e — UI / workspace surfaces.

Locks the user-visible affordances that closeout §8e:
  - 3 Customer Map Number Card fixtures exist + reference the right doctype/filter
  - Workspace JSON references all 3 cards + has Customer Map link in Masters card
  - Whitelisted endpoint inventory (button-wired methods) is complete
  - Scheduler cron entry for daily customer pull is wired
  - List view indicators + sidebar quick-filters are present
  - scheduled_discover_customers exists and gracefully handles 'no enabled
    account' (the pre-onboarding state — must NOT raise from cron)

ALL MOCKED — zero real EE traffic. The scheduler test uses a stub
pull_customers so no HTTP call happens.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


# ---------------------------------------------------------------------
# Q1 — Number Card fixtures
# ---------------------------------------------------------------------


class TestNumberCardFixtures(FrappeTestCase):
    """3 Customer Map cards must exist on the site after migrate, each
    pointing at EasyEcom Customer Map with the right status filter."""

    def _read_card(self, name: str) -> dict:
        return frappe.db.get_value(
            "Number Card",
            name,
            ["document_type", "filters_json", "function", "label"],
            as_dict=True,
        )

    def test_customers_in_drift_card_exists(self) -> None:
        c = self._read_card("Customers in Drift")
        self.assertIsNotNone(c, "fixture 'Customers in Drift' not loaded")
        self.assertEqual(c.document_type, "EasyEcom Customer Map")
        self.assertEqual(c.function, "Count")
        self.assertIn("Drift", c.filters_json)

    def test_customers_created_flagged_card_exists(self) -> None:
        c = self._read_card("Customers Created-Flagged")
        self.assertIsNotNone(c)
        self.assertEqual(c.document_type, "EasyEcom Customer Map")
        self.assertIn("Created-Flagged", c.filters_json)

    def test_customers_flagged_not_created_card_exists(self) -> None:
        c = self._read_card("Customers Flagged-Not-Created")
        self.assertIsNotNone(c)
        self.assertEqual(c.document_type, "EasyEcom Customer Map")
        self.assertIn("Flagged-Not-Created", c.filters_json)


# ---------------------------------------------------------------------
# Q2 — Workspace references
# ---------------------------------------------------------------------


class TestWorkspaceReferences(FrappeTestCase):
    """The EasyEcom workspace JSON must reference the 3 new Customer
    cards in the FDE Worklist row + the Customer Map link in the
    Masters card."""

    def _workspace_json(self) -> dict:
        path = os.path.join(
            frappe.get_app_path("ecommerce_super"),
            "easyecom", "workspace", "easyecom", "easyecom.json",
        )
        with open(path) as f:
            return json.load(f)

    def test_workspace_lists_three_customer_cards_in_number_cards_array(self) -> None:
        ws = self._workspace_json()
        labels = {c["label"] for c in ws.get("number_cards", [])}
        self.assertIn("Customers in Drift", labels)
        self.assertIn("Customers Created-Flagged", labels)
        self.assertIn("Customers Flagged-Not-Created", labels)

    def test_workspace_content_block_includes_customer_cards(self) -> None:
        """The rendered `content` (string-encoded layout) must mention
        all 3 customer cards by name so they render in the FDE Worklist
        row."""
        ws = self._workspace_json()
        content = ws["content"]
        self.assertIn("Customers in Drift", content)
        self.assertIn("Customers Created-Flagged", content)
        self.assertIn("Customers Flagged-Not-Created", content)

    def test_workspace_links_include_customer_map(self) -> None:
        """The Masters card must surface EasyEcom Customer Map as a
        navigable Link."""
        ws = self._workspace_json()
        link_targets = {
            link.get("link_to")
            for link in ws.get("links", [])
            if link.get("type") == "Link"
        }
        self.assertIn("EasyEcom Customer Map", link_targets)


# ---------------------------------------------------------------------
# Q3 — Whitelisted endpoint inventory
# ---------------------------------------------------------------------


class TestEndpointInventory(FrappeTestCase):
    """All §8e whitelisted endpoints must exist and be importable.
    Locks the FDE button surface so a future refactor that renames or
    moves a function trips this test immediately."""

    def test_all_eight_endpoints_importable(self) -> None:
        from ecommerce_super.easyecom.api.customer_pull import (
            discover_customers,
        )
        from ecommerce_super.easyecom.api.customer_push import (
            push_all_pending_customers,
            push_one_customer_now,
        )
        from ecommerce_super.easyecom.api.customer_master_mode import (
            flip_to_erpnext_mastered_customers,
        )
        from ecommerce_super.easyecom.api.customer_lookups import (
            refresh_countries_and_states,
        )
        from ecommerce_super.easyecom.flows.customer_pull import (
            dismiss_drift,
            push_to_ee_for_drift,
        )

        for fn in (
            discover_customers,
            push_all_pending_customers,
            push_one_customer_now,
            flip_to_erpnext_mastered_customers,
            refresh_countries_and_states,
            dismiss_drift,
            push_to_ee_for_drift,
        ):
            # Frappe's @whitelist registers the function in
            # frappe.whitelisted (a global set). The validate_argument_types
            # decorator wraps the original — both the wrapper and the
            # original may need checking; check at least one is whitelisted.
            unwrapped = getattr(fn, "__wrapped__", fn)
            self.assertTrue(
                fn in frappe.whitelisted or unwrapped in frappe.whitelisted,
                f"{fn.__name__} must carry @frappe.whitelist()",
            )


# ---------------------------------------------------------------------
# Q4 — Scheduler
# ---------------------------------------------------------------------


class TestSchedulerWiring(FrappeTestCase):
    """The customer-pull cron must be registered in hooks.py and the
    target function must exist + handle the no-account case gracefully."""

    def test_scheduler_event_registered(self) -> None:
        import ecommerce_super.hooks as h
        crons = h.scheduler_events.get("cron", {})
        # 05:30 IST entry per Stage 6 spec.
        self.assertIn("30 5 * * *", crons)
        self.assertIn(
            "ecommerce_super.easyecom.flows.customer_pull.scheduled_discover_customers",
            crons["30 5 * * *"],
        )

    def test_scheduled_function_exists_and_is_importable(self) -> None:
        from ecommerce_super.easyecom.flows.customer_pull import (
            scheduled_discover_customers,
        )
        self.assertTrue(callable(scheduled_discover_customers))

    def test_scheduled_function_handles_no_enabled_account(self) -> None:
        """Pre-onboarding state: no enabled Account. The cron must
        return None without raising (quiet log; no EE call)."""
        from ecommerce_super.easyecom.flows.customer_pull import (
            scheduled_discover_customers,
        )
        # Temporarily disable all accounts so the function takes the
        # no-account branch.
        previously_enabled = frappe.db.get_all(
            "EasyEcom Account",
            filters={"enabled": 1},
            pluck="name",
        )
        for n in previously_enabled:
            frappe.db.set_value(
                "EasyEcom Account", n, "enabled", 0, update_modified=False
            )
        frappe.db.commit()
        try:
            # Mock pull_customers so a stray code path can't make an EE call.
            with patch(
                "ecommerce_super.easyecom.flows.customer_pull.pull_customers"
            ) as mock_pull:
                result = scheduled_discover_customers()
                self.assertIsNone(result)
                mock_pull.assert_not_called()
        finally:
            for n in previously_enabled:
                frappe.db.set_value(
                    "EasyEcom Account", n, "enabled", 1, update_modified=False
                )
            frappe.db.commit()


# ---------------------------------------------------------------------
# Q5 — List view JS
# ---------------------------------------------------------------------


class TestListViewIndicators(FrappeTestCase):
    """The list view JS file (added in Stage 1, confirmed Stage 6) must
    define indicator colours for every status + sidebar quick-filters."""

    def _list_js(self) -> str:
        path = os.path.join(
            frappe.get_app_path("ecommerce_super"),
            "easyecom", "doctype", "easyecom_customer_map",
            "easyecom_customer_map_list.js",
        )
        with open(path) as f:
            return f.read()

    def test_list_view_defines_get_indicator(self) -> None:
        js = self._list_js()
        self.assertIn("get_indicator", js)

    def test_list_view_indicator_colours_present(self) -> None:
        js = self._list_js()
        # Each status appears with a colour mapping.
        for status, color in (
            ("Mapped", "green"),
            ("Created-Flagged", "orange"),
            ("Flagged-Not-Created", "grey"),
            ("Drift", "red"),
            ("Disabled", "darkgrey"),
        ):
            self.assertIn(status, js, f"missing status: {status}")
            self.assertIn(color, js, f"missing color: {color}")

    def test_list_view_sidebar_quick_filters(self) -> None:
        js = self._list_js()
        # add_menu_item entries for the FDE worklist quick-jumps.
        for filter_label in (
            "Show only Drift",
            "Show only Created-Flagged",
            "Show only Flagged-Not-Created",
            "Show only Mapped",
        ):
            self.assertIn(filter_label, js, f"missing menu item: {filter_label}")


# ---------------------------------------------------------------------
# Hooks fixture export
# ---------------------------------------------------------------------


class TestNumberCardsExportedAsFixtures(FrappeTestCase):
    """hooks.py fixtures list must include the 3 Customer cards so
    `bench export-fixtures` and downstream syncs pick them up."""

    def test_customer_cards_in_fixtures(self) -> None:
        import ecommerce_super.hooks as h
        # Find the Number Card fixture entry.
        nc_entries = [
            f for f in h.fixtures
            if isinstance(f, dict) and f.get("dt") == "Number Card"
        ]
        self.assertEqual(len(nc_entries), 1)
        names_filter = nc_entries[0]["filters"][0][2]  # [['name', 'in', [...]]]
        for card in (
            "Customers in Drift",
            "Customers Created-Flagged",
            "Customers Flagged-Not-Created",
        ):
            self.assertIn(card, names_filter)
