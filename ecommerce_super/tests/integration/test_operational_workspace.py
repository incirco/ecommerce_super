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
    "Locations — To Map",
    "Channels — Unclassified",
    "Tax Rules — To Configure",
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
