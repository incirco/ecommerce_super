"""gh#3 follow-up #2 — drift detection between the inline patch's
embedded content and the on-disk JSON fixture.

The `insert_easyecom_top_strip_from_inline` patch carries the Top
Strip's render payload (html / script / style) as Python triple-quoted
strings so the patch is reachable on deployments that didn't bundle
the on-disk JSON (the mmpl16 case). The on-disk JSON at
`easyecom/custom_html_block/easyecom_top_strip/easyecom_top_strip.json`
is the canonical source of truth for future content edits.

These two copies of the same data CAN drift if an FDE edits the JSON
without syncing the patch. This test asserts byte-equivalence so
drift surfaces at CI time, not at deployment time.

Drift means a deployment that takes the inline-patch rescue path
silently serves the stale content. Drift in the opposite direction
(patch edited, JSON not) means fresh installs that DO find the JSON
serve stale content — equally bad.

Resolution if this test fails: sync whichever of the two files was
edited later, then re-run.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import frappe


class TestTopStripInlineMatchesJson(unittest.TestCase):
    """Asserts every render-payload field in the inline patch equals
    the JSON fixture's corresponding field. Catches drift in either
    direction — patch-vs-JSON or JSON-vs-patch.
    """

    def setUp(self) -> None:
        from ecommerce_super.patches.v0_1 import (
            insert_easyecom_top_strip_from_inline as patch_mod,
        )
        self.patch_mod = patch_mod

        json_path = Path(
            frappe.get_app_path(
                "ecommerce_super",
                "easyecom",
                "custom_html_block",
                "easyecom_top_strip",
                "easyecom_top_strip.json",
            )
        )
        # The JSON is checked into source; if it's missing, that itself
        # is a configuration error worth surfacing as a test failure.
        self.assertTrue(
            json_path.exists(),
            f"on-disk JSON fixture not found at {json_path}",
        )
        self.json_data = json.loads(json_path.read_text())

    def test_html_matches(self) -> None:
        self.assertEqual(
            self.json_data.get("html"),
            self.patch_mod._HTML,
            "Inline patch _HTML drifted from easyecom_top_strip.json. "
            "Sync whichever side was edited later.",
        )

    def test_script_matches(self) -> None:
        self.assertEqual(
            self.json_data.get("script"),
            self.patch_mod._SCRIPT,
            "Inline patch _SCRIPT drifted from easyecom_top_strip.json. "
            "Sync whichever side was edited later.",
        )

    def test_style_matches(self) -> None:
        self.assertEqual(
            self.json_data.get("style"),
            self.patch_mod._STYLE,
            "Inline patch _STYLE drifted from easyecom_top_strip.json. "
            "Sync whichever side was edited later.",
        )

    def test_block_name_matches(self) -> None:
        """The DocType name is the join key — if the JSON's `name` field
        differs from the patch's `CUSTOM_BLOCK_NAME`, the patches act
        on different rows and one of them silently fails to heal."""
        self.assertEqual(
            self.json_data.get("name"),
            self.patch_mod.CUSTOM_BLOCK_NAME,
        )


if __name__ == "__main__":
    unittest.main()
