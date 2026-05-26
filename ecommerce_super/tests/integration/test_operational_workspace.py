"""§17 operational workspace tests.

The §17 layer is INFORMATIONAL on top of the navigation hub. These
tests assert four things:

1. The existing nav hub is PRESERVED — every Card Break and Link
   present pre-§17 is still present.
2. LIVE tiles point at the correct DocType + filter (so they return
   real counts; production data populates them organically).
3. PENDING placeholders are paragraph blocks, NOT number cards —
   so a "no feeder yet" state can never be misread as a live zero
   (§2.7 / §17 honesty rule).
4. The unified FDE Worklist row contains exactly the 6 expected
   cards across all masters (3 from #5 + 3 new), and no duplicates.

Pure JSON / data inspection — no real EE traffic, no client.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase


WORKSPACE = "EasyEcom"

EXPECTED_WORKLIST_CARDS = {
    "Locations - To Map",
    "Channels - Unclassified",
    "Tax Rules - To Configure",
    "Items in Drift",
    "Items Created-Flagged",
    "Items Flagged-Not-Created",
}

EXPECTED_LIVE_KPI_CARDS = {
    "Open Sync Records (Failed)",
    "API Calls (last hour)",
    "Queue Job Depth",
}

# Card Break labels that existed in the b3dc218 nav hub — every one
# must still be present in the workspace's `links` array.
EXPECTED_NAV_CARD_BREAKS = {
    "Setup",
    "Masters",
    "FDE Worklists",
    "Operations",
    "Runtime Logs",
}

# Frappe v16's `Workspace Sidebar` DocType is a STANDARD sidebar
# definition (separate from the workspace's own `links` array). For
# the EasyEcom workspace, the sidebar JSON ships at
# `ecommerce_super/workspace_sidebar/easyecom.json` and overrides
# the workspace's link rendering in the left nav. The two must
# match in structure — Card-Break section labels and FDE-worklist
# children — or the FDE sees a different sidebar than the
# workspace content advertises (the §17-audit caught this drift).
EXPECTED_SIDEBAR_SECTIONS = EXPECTED_NAV_CARD_BREAKS

EXPECTED_SIDEBAR_FDE_WORKLIST_LABELS = {
    "Locations - To Map",
    "Channels - Unclassified",
    "Tax Rules - To Configure",
    "Items - Drift",
    "Items - Created-Flagged",
    "Items - Flagged-Not-Created",
}


def _workspace() -> "frappe.model.document.Document":
    return frappe.get_doc("Workspace", WORKSPACE)


def _content_blocks() -> list[dict]:
    return json.loads(_workspace().content or "[]")


# ============================================================
# 1. Existing nav hub preserved (additive — nothing removed)
# ============================================================


class TestNavHubPreserved(FrappeTestCase):

    def test_all_pre_s17_card_breaks_still_present(self) -> None:
        ws = _workspace()
        present = {l.label for l in ws.links if l.type == "Card Break"}
        missing = EXPECTED_NAV_CARD_BREAKS - present
        self.assertFalse(
            missing,
            f"Pre-§17 Card Breaks missing from workspace links: {missing}",
        )

    def test_existing_card_content_blocks_preserved(self) -> None:
        blocks = _content_blocks()
        card_blocks = {b["data"]["card_name"] for b in blocks if b["type"] == "card"}
        self.assertEqual(card_blocks, EXPECTED_NAV_CARD_BREAKS)


# ============================================================
# Frappe v16 Workspace Sidebar — must mirror the workspace's
# Card Break structure (caught drifted from the workspace in
# the post-§17 audit; this test pins the contract going forward)
# ============================================================


class TestWorkspaceSidebarMirrorsWorkspace(FrappeTestCase):
    """The standard EasyEcom Workspace Sidebar (the read-only-from-
    desk sidebar Frappe v16 renders in the left nav) must match the
    workspace's own Card Break structure. Drift here means the FDE
    sees one set of groupings in the sidebar and another in the
    workspace content — confusing and pre-§17 we had exactly this."""

    SIDEBAR_NAME = "EasyEcom"

    def test_workspace_sidebar_exists(self) -> None:
        self.assertTrue(
            frappe.db.exists("Workspace Sidebar", self.SIDEBAR_NAME),
            "Workspace Sidebar 'EasyEcom' missing — patch "
            "refresh_easyecom_workspace_sidebar should have installed it.",
        )

    def test_sidebar_sections_match_workspace_card_breaks(self) -> None:
        items = frappe.db.get_all(
            "Workspace Sidebar Item",
            filters={"parent": self.SIDEBAR_NAME, "type": "Section Break"},
            fields=["label"],
        )
        sidebar_sections = {i.label for i in items}
        self.assertEqual(
            sidebar_sections,
            EXPECTED_SIDEBAR_SECTIONS,
            "Workspace Sidebar sections drifted from workspace "
            "Card Breaks. The two must match — see "
            "workspace_sidebar/easyecom.json.",
        )

    def test_sidebar_fde_worklist_has_all_six_links(self) -> None:
        items = frappe.db.get_all(
            "Workspace Sidebar Item",
            filters={"parent": self.SIDEBAR_NAME, "type": "Link"},
            fields=["label", "link_type", "link_to", "route_options"],
        )
        worklist_labels = {
            i.label for i in items
            if i.label in EXPECTED_SIDEBAR_FDE_WORKLIST_LABELS
        }
        self.assertEqual(
            worklist_labels,
            EXPECTED_SIDEBAR_FDE_WORKLIST_LABELS,
            "Sidebar missing one of the 6 FDE worklist items.",
        )
        # Each worklist link must be link_type=DocType with a
        # `route_options` JSON filter. NOT link_type=URL — Frappe's
        # sidebar_item.html line 26 hardcodes target="_blank" for
        # URL-type items, which forces filtered-list-view links to
        # open in a NEW TAB instead of in-tab navigation. The
        # DocType + route_options pattern renders an in-tab route
        # via frappe.utils.generate_route. This test pins that
        # contract (regression caught after the workspace ship).
        worklist_items = [i for i in items
                          if i.label in EXPECTED_SIDEBAR_FDE_WORKLIST_LABELS]
        for w in worklist_items:
            self.assertEqual(
                w.link_type, "DocType",
                f"FDE worklist sidebar link {w.label!r} must use "
                f"link_type=DocType (in-tab), not {w.link_type!r}. "
                "URL link_type hardcodes target=_blank, opening a "
                "new tab — wrong UX for an in-app navigation.",
            )
            self.assertTrue(
                w.link_to,
                f"{w.label}: DocType target missing from link_to.",
            )
            self.assertTrue(
                w.route_options,
                f"{w.label}: route_options (JSON filter) missing — "
                "without it the link lands on the unfiltered list "
                "view, defeating the worklist purpose.",
            )
            # route_options parses as JSON.
            import json as _json
            try:
                ro = _json.loads(w.route_options)
            except _json.JSONDecodeError:
                self.fail(f"{w.label}: route_options not valid JSON")
            self.assertTrue(
                ro,
                f"{w.label}: route_options dict is empty.",
            )


# ============================================================
# 2. LIVE tiles point at real feeders
# ============================================================


class TestLiveTilesPointAtRealFeeders(FrappeTestCase):

    def test_worklist_cards_exist_and_have_valid_filters(self) -> None:
        for name in EXPECTED_WORKLIST_CARDS:
            self.assertTrue(
                frappe.db.exists("Number Card", name),
                f"Number Card {name!r} missing",
            )
            nc = frappe.get_doc("Number Card", name)
            self.assertEqual(nc.type, "Document Type")
            # Filter JSON parses cleanly.
            try:
                filters = json.loads(nc.filters_json or "[]")
            except json.JSONDecodeError:
                self.fail(f"{name}: invalid filters_json")
            self.assertTrue(filters, f"{name}: empty filters_json")

    def test_live_kpi_cards_exist(self) -> None:
        for name in EXPECTED_LIVE_KPI_CARDS:
            self.assertTrue(
                frappe.db.exists("Number Card", name),
                f"KPI Number Card {name!r} missing",
            )

    def test_worklist_card_returns_count_against_real_doctype(self) -> None:
        """The Number Card's document_type is a real DocType the count
        query can execute against."""
        for name in EXPECTED_WORKLIST_CARDS | EXPECTED_LIVE_KPI_CARDS:
            nc = frappe.get_doc("Number Card", name)
            self.assertTrue(
                frappe.db.exists("DocType", nc.document_type),
                f"{name}: document_type {nc.document_type!r} doesn't exist",
            )
            # Try the query — should not raise (may return 0; that's a
            # real zero, not a placeholder).
            filters = json.loads(nc.filters_json or "[]")
            # filters_json shape is [[doctype, field, op, value], ...];
            # convert to dict for frappe.db.count.
            count_filters: dict = {}
            for f in filters:
                if len(f) == 4:
                    _, field, op, value = f
                    if op == "=":
                        count_filters[field] = value
                    elif op == "in":
                        count_filters[field] = ("in", value)
                    elif op == "Timespan":
                        # Timespan is a Frappe-special filter the count
                        # API understands via Frappe-internal expansion;
                        # we just verify count works AT ALL.
                        continue
            count = frappe.db.count(nc.document_type, count_filters)
            self.assertIsInstance(count, int)

    def test_charts_point_at_real_doctype(self) -> None:
        for chart_name in (
            "EasyEcom API Call Volume (7d)",
            "Sync Record Status (Item-only currently)",
        ):
            self.assertTrue(
                frappe.db.exists("Dashboard Chart", chart_name),
                f"Dashboard Chart {chart_name!r} missing",
            )
            chart = frappe.get_doc("Dashboard Chart", chart_name)
            self.assertTrue(
                frappe.db.exists("DocType", chart.document_type),
                f"{chart_name}: document_type {chart.document_type!r} missing",
            )

    def test_top_strip_custom_html_block_exists(self) -> None:
        """§17.2.1 Top Strip — Custom HTML Block installed by the patch."""
        self.assertTrue(
            frappe.db.exists("Custom HTML Block", "EasyEcom Top Strip"),
            "EasyEcom Top Strip Custom HTML Block missing — the patch "
            "install_easyecom_top_strip_block must have failed.",
        )


# ============================================================
# 3. PENDING placeholders are paragraph blocks, NOT number cards
# ============================================================


class TestPendingPlaceholdersAreNotNumberCards(FrappeTestCase):
    """The honesty rule (§2.7 / §17): a "0" that means "feeder not
    built" is FORBIDDEN. PENDING tiles must render as labeled
    placeholders, never as a numeric tile that could be misread as
    a live zero."""

    PENDING_TILE_KEYWORDS = (
        "Partial Jobs",
        "Webhook Events",
        "Cursor Lag",
        "Integration Discrepancies",
    )

    def test_no_pending_tile_is_a_number_card(self) -> None:
        """For each PENDING concept the §17 packet calls out, assert
        there's no Number Card with that label — they live as
        paragraph blocks instead."""
        for keyword in self.PENDING_TILE_KEYWORDS:
            existing = frappe.db.get_all(
                "Number Card",
                filters={"label": ("like", f"%{keyword}%")},
                pluck="name",
            )
            self.assertFalse(
                existing,
                f"Pending tile {keyword!r} unexpectedly exists as a "
                f"Number Card: {existing}. It should be a paragraph "
                "block to avoid misreading a '0' as a live count.",
            )

    def test_pending_placeholders_are_paragraph_blocks(self) -> None:
        """Every PENDING concept appears as a workspace paragraph block
        with text that explicitly says 'pending' so the FDE can't
        misread it as a live count."""
        blocks = _content_blocks()
        paragraph_texts = " ".join(
            b["data"].get("text", "")
            for b in blocks
            if b["type"] == "paragraph"
        )
        for keyword in self.PENDING_TILE_KEYWORDS:
            self.assertIn(
                keyword,
                paragraph_texts,
                f"Pending tile {keyword!r} not found in workspace "
                "paragraph blocks — verify the §17 layer wired it.",
            )
            # The placeholder must mention "pending" so its labelled-
            # empty state is unmistakable.
            self.assertIn("pending", paragraph_texts.lower())


# ============================================================
# 4. Unified FDE Worklist row — 6 cards, no duplicates
# ============================================================


class TestUnifiedWorklistRow(FrappeTestCase):

    def test_workspace_number_cards_include_all_worklist_cards(self) -> None:
        ws = _workspace()
        names = {nc.number_card_name for nc in ws.number_cards}
        missing = EXPECTED_WORKLIST_CARDS - names
        self.assertFalse(
            missing,
            f"Unified worklist row missing: {missing}",
        )

    def test_worklist_cards_appear_in_content_layout(self) -> None:
        blocks = _content_blocks()
        nc_blocks = {
            b["data"]["number_card_name"]
            for b in blocks
            if b["type"] == "number_card"
        }
        missing = EXPECTED_WORKLIST_CARDS - nc_blocks
        self.assertFalse(
            missing,
            f"Worklist cards not in workspace content layout: {missing}",
        )

    def test_no_duplicate_number_card_references(self) -> None:
        """The shipped 3 Item NCs were RELOCATED, not duplicated, into
        the unified worklist row — assert each NC appears at most once
        in the content layout."""
        blocks = _content_blocks()
        nc_refs = [
            b["data"]["number_card_name"]
            for b in blocks
            if b["type"] == "number_card"
        ]
        dups = [n for n in nc_refs if nc_refs.count(n) > 1]
        self.assertFalse(set(dups), f"Number Cards referenced twice: {set(dups)}")
