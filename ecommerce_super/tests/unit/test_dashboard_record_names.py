"""Guard test for gh#79 — site migration failed during dashboard
sync because two shipped Number Card JSONs had `>` characters in
their `name` field. Frappe's `validate_name` (frappe/model/naming.py)
rejects names containing `<` or `>`, so the whole migrate aborted
before the §11 cards could be inserted.

Root cause: §11 Phase 1 Stage 3 packet shipped:
  - "B2B orders awaiting invoice (>24h)"
  - "New B2B orders missing IDs (>2h)"

Fixed by renaming to `(24h+)` / `(2h+)` in both `name` and `label`
fields.

This test scans every Number Card, Dashboard Chart, and Custom HTML
Block JSON we ship and asserts none of them carry `<` or `>` in the
`name` field. Catches the next time someone reaches for a
mathematical-relation glyph in a record name.
"""
from __future__ import annotations

import json
import pathlib
import unittest


_APP_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _ship_jsons_by_doctype() -> list[tuple[pathlib.Path, dict]]:
    """Return (path, parsed_json) for every JSON we ship under the
    EasyEcom module whose top-level doctype is one of the dashboard
    record types loaded by Frappe's `sync_dashboards`."""
    record_doctypes = {
        "Number Card", "Dashboard Chart", "Custom HTML Block",
        "Dashboard", "Workspace",
    }
    base = _APP_ROOT / "ecommerce_super" / "easyecom"
    out: list[tuple[pathlib.Path, dict]] = []
    for path in base.rglob("*.json"):
        # Skip DocType JSONs — those have "doctype": "DocType" and a
        # different naming convention (no `<`/`>` in DocType names is
        # enforced elsewhere).
        try:
            with open(path) as f:
                doc = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # Not a Frappe JSON record — skip.
        if not isinstance(doc, dict):
            continue
        if doc.get("doctype") in record_doctypes:
            out.append((path, doc))
    return out


class TestDashboardRecordNamesAreValid(unittest.TestCase):
    """gh#79 — Frappe's set_new_name → validate_name rejects names
    containing `<` or `>`. Every shipped dashboard record name must
    be ASCII-clean of these glyphs."""

    @classmethod
    def setUpClass(cls):
        cls.ship_jsons = _ship_jsons_by_doctype()

    def test_at_least_one_dashboard_record_shipped(self) -> None:
        """Sanity: we should be finding at least the §11 / §17
        worklist cards. Empty result means the test isn't actually
        scanning anything (path bug)."""
        self.assertGreater(
            len(self.ship_jsons), 0,
            "No dashboard records found under "
            "ecommerce_super/easyecom/. Either we shipped none "
            "(unlikely) or this test isn't scanning the right path."
        )

    def test_no_record_name_contains_lt_or_gt(self) -> None:
        """The actual gh#79 regression guard."""
        offenders: list[tuple[str, str]] = []
        for path, doc in self.ship_jsons:
            name = doc.get("name", "")
            if "<" in name or ">" in name:
                rel = path.relative_to(_APP_ROOT)
                offenders.append((str(rel), name))
        self.assertEqual(
            offenders, [],
            "Found shipped dashboard records with '<' or '>' in "
            "their `name` field — Frappe's validate_name will reject "
            "these and abort sync_dashboards during migrate. Replace "
            "with ASCII-safe alternatives (e.g., '(>24h)' → '(24h+)'). "
            f"Offending records: {offenders}"
        )

    def test_label_consistency_with_name(self) -> None:
        """A weaker sanity: when both `name` and `label` are present,
        the label should not carry `<`/`>` either. Frappe doesn't
        validate label characters but it would surface in the UI as
        broken HTML if not escaped — and historically labels and
        names are kept in sync for these worklist cards."""
        for path, doc in self.ship_jsons:
            label = doc.get("label") or ""
            if "<" in label or ">" in label:
                rel = path.relative_to(_APP_ROOT)
                self.fail(
                    f"Dashboard record at {rel} has '<' or '>' in "
                    f"label {label!r} — replace with ASCII-safe glyphs."
                )


if __name__ == "__main__":
    unittest.main()
