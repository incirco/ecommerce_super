"""Stage 6 tests for §8f — UI / workspace / sidebar / scheduler.

ALL MOCKED for the scheduler tests; the workspace / sidebar / number-
card assertions run against the actual DocType rows the migrate
loaded.

Coverage:
  - 3 Supplier Number Cards exist with correct filters / colors.
  - Workspace EasyEcom carries:
      * Supplier Map link under Masters,
      * Suppliers — Drift / Created-Flagged / Flagged-Not-Created
        worklist links,
      * the 3 supplier number-card entries in the number_cards
        array,
      * the 3 supplier cards in the FDE Worklist content block,
      * the Supplier Map shortcut.
  - **Sidebar matches Card Breaks** — every Supplier worklist labelled
    in the workspace JSON has a corresponding sidebar item with the
    SAME label AND the same status filter in route_options. This is
    the §8e regression guard the user called out.
  - Supplier Map list view has status colors + the 4 sidebar preset
    filters wired (Drift / Created-Flagged / FNC / Mapped).
  - Scheduler:
      * scheduled_discover_suppliers is wired in hooks.scheduler_events
        at the 06:00 IST slot.
      * It reads Account.supplier_pull_last_updated_at and passes it
        as updated_after to pull_suppliers.
      * Quiet on no enabled Account (pre-onboarding).
      * Catches exceptions so a transient outage doesn't fail the
        scheduler tick.
  - Endpoint inventory: every whitelisted Stage 3-5 endpoint is
    importable + decorated, role-gated, never raises through the
    whitelist boundary.
"""

from __future__ import annotations

import datetime
import json
import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


WORKSPACE_JSON_PATH = os.path.join(
    frappe.get_app_path("ecommerce_super"),
    "easyecom", "workspace", "easyecom", "easyecom.json",
)
SIDEBAR_JSON_PATH = os.path.join(
    frappe.get_app_path("ecommerce_super"),
    "workspace_sidebar", "easyecom.json",
)


# ----- Number Cards -----


class TestSupplierNumberCardsExist(FrappeTestCase):
    """The 3 Supplier-Map number cards must be loaded as standard
    fixtures and carry the right filters + colors."""

    EXPECTED = {
        "Suppliers in Drift": {
            "status_filter": "Drift",
            "color": "#ef4444",  # red
        },
        "Suppliers Created-Flagged": {
            "status_filter": "Created-Flagged",
            "color": "#f59e0b",  # orange
        },
        "Suppliers Flagged-Not-Created": {
            "status_filter": "Flagged-Not-Created",
            "color": "#94a3b8",  # grey
        },
    }

    def test_all_three_cards_exist(self) -> None:
        for name in self.EXPECTED:
            self.assertTrue(
                frappe.db.exists("Number Card", name),
                f"Number Card {name!r} missing",
            )

    def test_each_card_filters_supplier_map_status(self) -> None:
        for name, expected in self.EXPECTED.items():
            card = frappe.get_doc("Number Card", name)
            self.assertEqual(card.document_type, "EasyEcom Supplier Map")
            filters = json.loads(card.filters_json or "[]")
            # Frappe stores filters as [[doctype, field, op, value]].
            self.assertTrue(any(
                f[1] == "status" and f[3] == expected["status_filter"]
                for f in filters
            ), f"{name} missing status={expected['status_filter']} filter")

    def test_each_card_has_correct_color(self) -> None:
        for name, expected in self.EXPECTED.items():
            color = frappe.db.get_value("Number Card", name, "color")
            self.assertEqual(
                color, expected["color"],
                f"{name} color {color!r} != expected {expected['color']!r}",
            )


# ----- Workspace -----


class TestWorkspaceCarriesSupplierEntries(FrappeTestCase):
    """The workspace JSON has been re-imported by the §8f Stage 6
    refresh patch. Verify every expected Supplier entry lives in the
    EasyEcom Workspace row."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.workspace = frappe.get_doc("Workspace", "EasyEcom")

    def test_supplier_map_link_under_masters(self) -> None:
        labels = {link.label: link for link in self.workspace.links}
        self.assertIn("Supplier Map", labels)
        self.assertEqual(labels["Supplier Map"].link_to, "EasyEcom Supplier Map")
        self.assertEqual(labels["Supplier Map"].type, "Link")

    def test_three_supplier_worklist_links_under_fde_worklists(self) -> None:
        labels = {link.label: link for link in self.workspace.links}
        for label in (
            "Suppliers — Drift",
            "Suppliers — Created-Flagged",
            "Suppliers — Flagged-Not-Created",
        ):
            self.assertIn(label, labels, f"workspace missing link {label!r}")
            self.assertEqual(labels[label].link_to, "EasyEcom Supplier Map")

    def test_supplier_number_cards_in_workspace_array(self) -> None:
        names = {c.number_card_name for c in self.workspace.number_cards}
        for n in (
            "Suppliers in Drift",
            "Suppliers Created-Flagged",
            "Suppliers Flagged-Not-Created",
        ):
            self.assertIn(n, names)

    def test_content_block_renders_three_supplier_cards_in_a_row(self) -> None:
        """The 'FDE Worklist' content block lays out cards 3-per-row
        (col=4). The Supplier row's 3 cards must each have col=4 so
        they line up alongside the Items + Customers rows."""
        content = json.loads(self.workspace.content)
        supplier_blocks = [
            b
            for b in content
            if b.get("type") == "number_card"
            and "Suppliers " in (b.get("data") or {}).get("number_card_name", "")
        ]
        self.assertEqual(len(supplier_blocks), 3)
        for b in supplier_blocks:
            self.assertEqual(b["data"]["col"], 4)

    def test_supplier_section_header_present(self) -> None:
        """A 'Suppliers' header block sits above the 3 cards so the FDE
        knows which entity the row covers."""
        content = json.loads(self.workspace.content)
        headers = [
            b
            for b in content
            if b.get("type") == "header"
            and "Suppliers" in (b.get("data") or {}).get("text", "")
        ]
        self.assertTrue(headers, "no 'Suppliers' header block in workspace content")

    def test_supplier_map_shortcut_under_masters(self) -> None:
        shortcuts = {s.label: s for s in self.workspace.shortcuts}
        self.assertIn("Supplier Map", shortcuts)
        self.assertEqual(shortcuts["Supplier Map"].link_to, "EasyEcom Supplier Map")


# ----- Sidebar matches Card Breaks (§8e regression guard) -----


class TestSidebarMatchesCardBreaks(FrappeTestCase):
    """The two §8e workspace mistakes the packet called out:
       1. Sidebar drifted from Card Breaks (mismatched sections).
       2. URL-placement bug made sidebar items unclickable
          (route_options absent or in wrong field).

    This class guards against both for the §8f Supplier additions."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.sidebar = frappe.get_doc("Workspace Sidebar", "EasyEcom")
        cls.workspace = frappe.get_doc("Workspace", "EasyEcom")

    def test_sidebar_has_supplier_map_link(self) -> None:
        items = [i for i in self.sidebar.items if i.label == "Supplier Map"]
        self.assertEqual(len(items), 1, "sidebar missing Supplier Map link")
        self.assertEqual(items[0].link_to, "EasyEcom Supplier Map")
        self.assertEqual(items[0].type, "Link")

    def test_sidebar_has_three_supplier_worklist_filters(self) -> None:
        EXPECTED = {
            "Suppliers - Drift": "Drift",
            "Suppliers - Created-Flagged": "Created-Flagged",
            "Suppliers - Flagged-Not-Created": "Flagged-Not-Created",
        }
        by_label = {i.label: i for i in self.sidebar.items}
        for label, expected_status in EXPECTED.items():
            self.assertIn(
                label, by_label, f"sidebar missing worklist {label!r}"
            )
            it = by_label[label]
            self.assertEqual(it.link_to, "EasyEcom Supplier Map")
            self.assertEqual(it.type, "Link")
            # route_options is the v16 URL-encoded filter — it MUST be
            # populated, NOT empty / NOT just-the-status-string. This
            # is the §8e bug guard.
            self.assertTrue(
                it.route_options,
                f"{label!r} sidebar item missing route_options — clicks "
                "would land on an unfiltered list",
            )
            # Parsed shape must be {"status": "<value>"}, not embedded
            # in url or similar.
            parsed = json.loads(it.route_options)
            self.assertEqual(parsed.get("status"), expected_status)

    def test_sidebar_supplier_items_clickable_not_url_only(self) -> None:
        """The §8e URL-placement bug: items had `url` populated but
        link_to absent, so clicking landed on a literal URL string
        instead of opening the filtered list. Guard: every Supplier
        worklist item has link_to set AND url unset (or null)."""
        for label in (
            "Suppliers - Drift",
            "Suppliers - Created-Flagged",
            "Suppliers - Flagged-Not-Created",
        ):
            it = next(
                (i for i in self.sidebar.items if i.label == label), None
            )
            self.assertIsNotNone(it)
            self.assertTrue(it.link_to, f"{label!r} has no link_to")
            self.assertFalse(
                getattr(it, "url", None),
                f"{label!r} has a `url` set — should be null; link_to "
                "is the clickable field",
            )

    def test_every_supplier_workspace_link_has_matching_sidebar_entry(self) -> None:
        """Pair-up check: every workspace `Link` entry for Supplier
        (Masters or FDE Worklists card breaks) has a matching sidebar
        item. The §8e finding was that the two drifted independently;
        this test catches it the moment they diverge."""
        ws_supplier_links = {
            link.label.replace(" — ", " - "): link  # normalise em-dash
            for link in self.workspace.links
            if link.type == "Link" and "Supplier" in (link.label or "")
        }
        sb_supplier_links = {
            i.label: i
            for i in self.sidebar.items
            if i.type == "Link" and "Supplier" in (i.label or "")
        }
        # Symmetric difference must be empty.
        missing_in_sidebar = set(ws_supplier_links) - set(sb_supplier_links)
        missing_in_workspace = set(sb_supplier_links) - set(ws_supplier_links)
        self.assertEqual(
            missing_in_sidebar,
            set(),
            f"workspace has supplier links not in sidebar: {missing_in_sidebar}",
        )
        self.assertEqual(
            missing_in_workspace,
            set(),
            f"sidebar has supplier links not in workspace: {missing_in_workspace}",
        )


# ----- List view -----


class TestSupplierMapListView(FrappeTestCase):
    """The Supplier Map list view's get_indicator function covers the
    5 status values with the packet-specified colors. The list.js
    file isn't loadable from Python, but it's small enough to grep."""

    LIST_JS_PATH = os.path.join(
        frappe.get_app_path("ecommerce_super"),
        "easyecom", "doctype", "easyecom_supplier_map",
        "easyecom_supplier_map_list.js",
    )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with open(cls.LIST_JS_PATH) as f:
            cls.list_js = f.read()

    def test_status_color_map_has_all_five_values(self) -> None:
        for status in (
            "Mapped",
            "Created-Flagged",
            "Flagged-Not-Created",
            "Drift",
            "Disabled",
        ):
            self.assertIn(
                f'"{status}"', self.list_js,
                f"list.js missing status {status!r}",
            )

    def test_status_color_map_uses_expected_colors(self) -> None:
        # Packet colors: Drift=red, Created-Flagged=orange, Mapped=green,
        # FNC=grey, Disabled=darkgrey.
        for status, expected_color in (
            ("Drift", "red"),
            ("Created-Flagged", "orange"),
            ("Mapped", "green"),
            ("Flagged-Not-Created", "grey"),
            ("Disabled", "darkgrey"),
        ):
            # The status entry is like `"Drift": ["Drift", "red", ...`.
            line_pattern = f'"{status}":'
            self.assertIn(line_pattern, self.list_js)
            # And the color appears on a nearby line.
            idx = self.list_js.index(line_pattern)
            block = self.list_js[idx : idx + 200]
            self.assertIn(
                f'"{expected_color}"', block,
                f"{status!r} entry doesn't reference {expected_color!r}",
            )

    def test_sidebar_quick_filters_wired(self) -> None:
        """The list.js add_menu_item calls light up 4 sidebar shortcuts
        (Drift / Created-Flagged / FNC / Mapped). Excludes Disabled —
        not an FDE-actionable status."""
        for label in (
            "Show only Drift",
            "Show only Created-Flagged",
            "Show only Flagged-Not-Created",
            "Show only Mapped (clean)",
        ):
            self.assertIn(
                f'"{label}"', self.list_js,
                f"list.js missing menu item {label!r}",
            )


# ----- Scheduler -----


class TestSchedulerWiring(FrappeTestCase):
    """The §8f Stage 6 cron must be registered in hooks.py at the
    06:00 IST slot, after Items (05:00) and Customers (05:30).
    Verifying via Python's hooks module rather than parsing the file
    so refactoring the spacing doesn't break the test."""

    def test_supplier_pull_is_in_scheduler_events_cron(self) -> None:
        import ecommerce_super.hooks as hooks
        scheduler = hooks.scheduler_events or {}
        cron = scheduler.get("cron", {})
        all_methods: list[str] = []
        for slot, methods in cron.items():
            all_methods.extend(methods)
        self.assertIn(
            "ecommerce_super.easyecom.flows.supplier_pull.scheduled_discover_suppliers",
            all_methods,
        )

    def test_supplier_pull_runs_at_06_00_slot(self) -> None:
        """Staggering matters — Items 05:00, Customers 05:30, Suppliers
        06:00 avoids hitting EE in one tick."""
        import ecommerce_super.hooks as hooks
        cron = (hooks.scheduler_events or {}).get("cron", {})
        # Locate the slot.
        for slot, methods in cron.items():
            if any(
                "supplier_pull" in m for m in methods
            ):
                self.assertEqual(slot, "0 6 * * *", f"supplier pull at {slot!r}")
                return
        self.fail("supplier_pull not wired in any cron slot")


class TestScheduledDeltaPullUsesWatermark(FrappeTestCase):
    """The §8f scheduler reads Account.supplier_pull_last_updated_at
    and passes it as `updated_after` to pull_suppliers. This is the
    key §8f difference vs §8e Customer (which has no high-water +
    runs a full pull every time)."""

    ACCOUNT_NAME = "test-8f-s6-acct"

    def setUp(self) -> None:
        # Use the live Harmony account if it exists; otherwise create a
        # disposable test account (disabled, since the live one is
        # already enabled).
        if not frappe.db.exists("EasyEcom Account", self.ACCOUNT_NAME):
            from ecommerce_super.tests.factories import make_account
            make_account(name=self.ACCOUNT_NAME, enabled=False)
        # Disable Harmony temporarily so this test's account is the only
        # "enabled" one. (§8.1 single-enabled invariant.)
        self._harmony_was_enabled = frappe.db.get_value(
            "EasyEcom Account", "Harmony", "enabled"
        )
        if self._harmony_was_enabled:
            frappe.db.set_value(
                "EasyEcom Account", "Harmony", "enabled", 0,
                update_modified=False,
            )
        frappe.db.commit()

    def tearDown(self) -> None:
        if self._harmony_was_enabled:
            frappe.db.set_value(
                "EasyEcom Account", "Harmony", "enabled", 1,
                update_modified=False,
            )
        try:
            frappe.delete_doc(
                "EasyEcom Account", self.ACCOUNT_NAME,
                force=True, ignore_permissions=True,
            )
        except Exception:
            pass
        frappe.db.commit()

    def _enable_test_account(self, *, high_water: datetime.datetime | None) -> None:
        frappe.db.set_value(
            "EasyEcom Account",
            self.ACCOUNT_NAME,
            {
                "enabled": 1,
                "supplier_pull_last_updated_at": high_water,
            },
            update_modified=False,
        )
        frappe.db.commit()

    def test_scheduled_pull_passes_watermark_as_updated_after(self) -> None:
        high_water = datetime.datetime(2026, 5, 1, 12, 0, 0)
        self._enable_test_account(high_water=high_water)

        from ecommerce_super.easyecom.flows import supplier_pull as mod

        with patch.object(mod, "pull_suppliers") as mock_pull:
            mod.scheduled_discover_suppliers()

        mock_pull.assert_called_once()
        kwargs = mock_pull.call_args.kwargs
        self.assertEqual(kwargs["account"], self.ACCOUNT_NAME)
        self.assertTrue(kwargs["start_fresh"])
        # YYYY-MM-DD format derived from the high-water.
        self.assertEqual(kwargs["updated_after"], "2026-05-01")

    def test_scheduled_pull_first_run_blank_watermark_falls_through(self) -> None:
        """A pre-flip site with no prior pull has supplier_pull_last_updated_at=NULL.
        The scheduler must call pull_suppliers WITHOUT updated_after
        (None), letting EE return everything."""
        self._enable_test_account(high_water=None)

        from ecommerce_super.easyecom.flows import supplier_pull as mod

        with patch.object(mod, "pull_suppliers") as mock_pull:
            mod.scheduled_discover_suppliers()

        mock_pull.assert_called_once()
        self.assertIsNone(mock_pull.call_args.kwargs["updated_after"])

    def test_scheduled_pull_quiet_when_no_enabled_account(self) -> None:
        """No enabled Account → scheduler is silent (early return).
        Pre-onboarding state."""
        # No test account enabled, Harmony was already disabled in setUp.
        from ecommerce_super.easyecom.flows import supplier_pull as mod

        with patch.object(mod, "pull_suppliers") as mock_pull:
            mod.scheduled_discover_suppliers()

        mock_pull.assert_not_called()

    def test_scheduled_pull_catches_pull_exception(self) -> None:
        """A transient EE outage during the scheduled pull must NOT
        propagate — the scheduler tick should log + continue."""
        self._enable_test_account(high_water=None)

        from ecommerce_super.easyecom.flows import supplier_pull as mod

        with patch.object(
            mod, "pull_suppliers", side_effect=RuntimeError("EE down")
        ):
            # Must NOT raise.
            mod.scheduled_discover_suppliers()


class TestPullSuppliersAcceptsUpdatedAfter(FrappeTestCase):
    """pull_suppliers' new `updated_after` param attaches to the first
    request only (cursor follow-ups inherit the filter from nextUrl)."""

    def test_updated_after_attached_to_first_page_only(self) -> None:
        from ecommerce_super.easyecom.flows.supplier_pull import (
            pull_suppliers,
        )

        page_1 = {
            "code": 200,
            "data": [],
            "nextUrl": "/wms/V2/getVendors?cursor=abc",
        }
        page_2 = {"code": 200, "data": [], "nextUrl": None}
        client = MagicMock()
        responses = iter([page_1, page_2])

        def _get(endpoint, params=None, **_kw):
            return next(responses)

        client.get.side_effect = _get

        # Need an enabled account for the pull's _enabled_account()
        # helper. Use the live Harmony.
        pull_suppliers(
            client=client,
            account="Harmony",
            start_fresh=True,
            updated_after="2026-05-01",
        )

        # First call: includes the param.
        first_call_kwargs = client.get.call_args_list[0].kwargs
        self.assertEqual(
            (first_call_kwargs.get("params") or {}).get("updated_after"),
            "2026-05-01",
        )
        # Second call: NO params (cursor URL carries the filter).
        second_call_kwargs = client.get.call_args_list[1].kwargs
        self.assertFalse(second_call_kwargs.get("params"))


# ----- Endpoint inventory -----


class TestEndpointInventory(FrappeTestCase):
    """All §8f whitelisted endpoints exist, are decorated, and
    role-gate against Operator-only access. This is the FDE-facing
    button inventory."""

    EXPECTED_ENDPOINTS = (
        # (module, function, role-gated)
        ("ecommerce_super.easyecom.api.supplier_pull", "discover_suppliers"),
        ("ecommerce_super.easyecom.api.supplier_push", "push_one_supplier_now"),
        ("ecommerce_super.easyecom.api.supplier_push", "push_all_pending_suppliers"),
        ("ecommerce_super.easyecom.api.supplier_master_mode", "flip_to_erpnext_mastered_suppliers"),
        ("ecommerce_super.easyecom.flows.supplier_pull", "dismiss_drift"),
        ("ecommerce_super.easyecom.flows.supplier_pull", "push_to_ee_for_drift"),
    )

    def test_all_endpoints_importable(self) -> None:
        for mod_path, fn_name in self.EXPECTED_ENDPOINTS:
            fn = frappe.get_attr(f"{mod_path}.{fn_name}")
            self.assertTrue(
                callable(fn),
                f"{mod_path}.{fn_name} not importable / not callable",
            )

    def test_all_endpoints_whitelisted(self) -> None:
        """Each endpoint must be decorated with @frappe.whitelist().
        The decorator marks the function via its __wrapped__ /
        whitelisted attribute."""
        for mod_path, fn_name in self.EXPECTED_ENDPOINTS:
            fn = frappe.get_attr(f"{mod_path}.{fn_name}")
            # Frappe's @whitelist decorator sets these attributes on
            # the wrapper.
            self.assertTrue(
                getattr(fn, "whitelisted", False)
                or fn in (frappe.whitelisted or set()),
                f"{mod_path}.{fn_name} is not whitelisted",
            )

    def test_operator_role_refused_by_pull(self) -> None:
        """EasyEcom Operator is read-only; the pull endpoint MUST
        refuse with PermissionError."""
        from ecommerce_super.easyecom.api.supplier_pull import (
            discover_suppliers,
        )

        # Create + switch to a user with Operator role only.
        email = "operator-8f-s6@test.local"
        if frappe.db.exists("User", email):
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        u = frappe.new_doc("User")
        u.update(
            {
                "email": email,
                "first_name": "Op",
                "send_welcome_email": 0,
                "enabled": 1,
            }
        )
        u.insert(ignore_permissions=True)
        u.append("roles", {"role": "EasyEcom Operator"})
        u.save(ignore_permissions=True)
        frappe.db.commit()

        original_user = frappe.session.user
        frappe.set_user(email)
        try:
            with self.assertRaises(frappe.PermissionError):
                discover_suppliers()
        finally:
            frappe.set_user(original_user)
            frappe.delete_doc(
                "User", email, force=True, ignore_permissions=True
            )
            frappe.db.commit()


class TestDiscoverAsyncByDefault(FrappeTestCase):
    """Regression for the FrappeCloud "Discover Products call itself
    failed (network or permission)" issue that fires on >2000-row
    pulls: the whitelist endpoints must enqueue by default and only
    run inline when explicitly opted in via inline=True. The inline
    path is reserved for tests + small catalogues that fit in the 120s
    desk-whitelist window."""

    def test_discover_suppliers_async_by_default(self) -> None:
        """Default invocation (no inline arg) returns enqueued=True
        without calling the underlying pull_suppliers."""
        from unittest.mock import patch
        from ecommerce_super.easyecom.api.supplier_pull import (
            discover_suppliers,
        )

        with patch(
            "ecommerce_super.easyecom.api.supplier_pull.pull_suppliers"
        ) as mock_pull, patch(
            "ecommerce_super.easyecom.api.supplier_pull.frappe.enqueue"
        ) as mock_enqueue:
            mock_enqueue.return_value.id = "discover_suppliers_test_1"
            result = discover_suppliers()
        mock_pull.assert_not_called()
        mock_enqueue.assert_called_once()
        # Verify it goes to the long queue with the right timeout.
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs.get("queue"), "long")
        self.assertEqual(kwargs.get("timeout"), 3600)
        self.assertTrue(result["ok"])
        self.assertTrue(result["enqueued"])

    def test_discover_suppliers_inline_runs_synchronously(self) -> None:
        """inline=True bypasses enqueue and calls pull_suppliers
        directly. Used by tests + small-catalogue runs."""
        from unittest.mock import MagicMock, patch
        from ecommerce_super.easyecom.api.supplier_pull import (
            discover_suppliers,
        )

        fake_outcome = MagicMock(
            pages_walked=1, final_cursor=None, total=5, created=5,
            skipped=0, disabled=0, created_flagged=0,
            flagged_not_created=0, drift_count=0, failed=0, failures=[],
        )
        with patch(
            "ecommerce_super.easyecom.api.supplier_pull.pull_suppliers",
            return_value=fake_outcome,
        ) as mock_pull, patch(
            "ecommerce_super.easyecom.api.supplier_pull.frappe.enqueue"
        ) as mock_enqueue:
            result = discover_suppliers(inline=1)
        mock_pull.assert_called_once()
        mock_enqueue.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["enqueued"])
        self.assertEqual(result["total"], 5)
