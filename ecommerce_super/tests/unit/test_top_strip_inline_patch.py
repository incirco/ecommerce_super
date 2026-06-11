"""gh#3 follow-up #2 — inline-content patch for EasyEcom Top Strip.

Confirms the patch's branch logic against a mocked frappe.db:
  - Table missing → quiet no-op
  - Block missing → inserts from embedded content
  - Block exists with empty html → heals from embedded content
  - Block exists with populated html → no-op (preserves FDE edits)

The embedded content itself is bytewise-equivalent to the on-disk JSON
fixture; that's verified in a separate integration test on a live site,
not here.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestInsertEasyEcomTopStripFromInline(unittest.TestCase):
    def test_no_table_short_circuits(self) -> None:
        from ecommerce_super.patches.v0_1 import (
            insert_easyecom_top_strip_from_inline as patch_mod,
        )

        with (
            patch.object(frappe.db, "table_exists", return_value=False),
            patch.object(frappe.db, "exists") as exists_mock,
            patch("frappe.new_doc") as new_doc_mock,
        ):
            patch_mod.execute()

        exists_mock.assert_not_called()
        new_doc_mock.assert_not_called()

    def test_block_missing_inserts_from_embedded(self) -> None:
        from ecommerce_super.patches.v0_1 import (
            insert_easyecom_top_strip_from_inline as patch_mod,
        )

        fake_doc = MagicMock()
        with (
            patch.object(frappe.db, "table_exists", return_value=True),
            patch.object(frappe.db, "exists", return_value=False),
            patch("frappe.new_doc", return_value=fake_doc) as new_doc_mock,
            patch.object(frappe.db, "commit"),
        ):
            patch_mod.execute()

        new_doc_mock.assert_called_once_with("Custom HTML Block")
        fake_doc.update.assert_called_once()
        update_payload = fake_doc.update.call_args.args[0]
        # Embedded payload reached the doc.
        self.assertEqual(update_payload["name"], "EasyEcom Top Strip")
        self.assertIn("ecs-top-strip", update_payload["html"])
        self.assertIn("frappe.call", update_payload["script"])
        self.assertIn("ecs-tile", update_payload["style"])
        fake_doc.insert.assert_called_once()

    def test_block_exists_with_empty_html_is_healed(self) -> None:
        from ecommerce_super.patches.v0_1 import (
            insert_easyecom_top_strip_from_inline as patch_mod,
        )

        fake_existing = MagicMock()
        fake_existing.html = None
        fake_existing.script = None
        # Mock `set` to write into a dict so we can verify what got written.
        captured: dict = {}
        fake_existing.set = lambda field, value: captured.update({field: value})

        with (
            patch.object(frappe.db, "table_exists", return_value=True),
            patch.object(frappe.db, "exists", return_value=True),
            patch("frappe.get_doc", return_value=fake_existing),
            patch.object(frappe.db, "commit"),
        ):
            patch_mod.execute()

        # Healed all five embedded fields.
        for field in ("html", "script", "style", "is_standard", "module", "private"):
            self.assertIn(field, captured)
        fake_existing.save.assert_called_once()

    def test_block_exists_with_populated_content_is_noop(self) -> None:
        """Don't clobber FDE customisations."""
        from ecommerce_super.patches.v0_1 import (
            insert_easyecom_top_strip_from_inline as patch_mod,
        )

        fake_existing = MagicMock()
        fake_existing.html = "<div>customised by FDE</div>"
        fake_existing.script = "console.log('custom')"

        with (
            patch.object(frappe.db, "table_exists", return_value=True),
            patch.object(frappe.db, "exists", return_value=True),
            patch("frappe.get_doc", return_value=fake_existing),
            patch.object(frappe.db, "commit") as commit_mock,
        ):
            patch_mod.execute()

        fake_existing.save.assert_not_called()
        commit_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
