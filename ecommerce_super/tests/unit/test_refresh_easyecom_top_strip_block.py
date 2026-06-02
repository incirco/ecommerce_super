"""Unit tests for the gh#3-followup Top Strip refresh patch.

The patch's job is to overwrite html/script/style/is_standard/module/
private on an existing `EasyEcom Top Strip` Custom HTML Block from the
on-disk JSON, while preserving creation/owner. We assert three things:

  1. **Refresh path** — when an existing block has empty render fields,
     the patch sets html/script/style and calls save().
  2. **Idempotent path** — when the on-disk JSON already matches the DB
     row, the patch makes no changes (no save() call, no DB write).
  3. **Fresh-install path** — when the block doesn't exist, the patch
     inserts it from the JSON.

Each test stubs the small Frappe surface the patch touches
(`frappe.db.exists`, `frappe.get_doc`, `frappe.get_app_path`,
`frappe.db.commit`) so the patch can run outside a live site.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


# The JSON shape the patch reads from disk. Mirrors the real on-disk
# easyecom_top_strip.json — we only care about the fields the patch
# touches; other DocType-JSON keys are tolerated and ignored.
_JSON_PAYLOAD = {
    "doctype": "Custom HTML Block",
    "name": "EasyEcom Top Strip",
    "html": "<div class='ecs-top-strip'>strip</div>",
    "script": "console.log('strip');",
    "style": ".ecs-top-strip { padding: 8px; }",
    "is_standard": 1,
    "module": "EasyEcom",
    "private": 0,
    "__islocal": 1,
}


class _FakeBlock:
    """Drop-in for the live `Custom HTML Block` doc returned by
    frappe.get_doc. Implements the minimal surface the patch uses:
    .get(field), .set(field, value), .save()."""

    def __init__(self, initial: dict | None = None):
        self._fields: dict = dict(initial or {})
        self.save_calls = 0

    def get(self, field: str):
        return self._fields.get(field)

    def set(self, field: str, value):
        self._fields[field] = value

    def save(self, ignore_permissions: bool = False):  # noqa: ARG002
        self.save_calls += 1

    def insert(self, ignore_permissions: bool = False):  # noqa: ARG002
        self.save_calls += 1


def _make_tmp_json() -> tuple[Path, TemporaryDirectory]:
    tmpdir = TemporaryDirectory()
    json_path = Path(tmpdir.name) / "easyecom_top_strip.json"
    json_path.write_text(json.dumps(_JSON_PAYLOAD))
    return json_path, tmpdir


class TestTopStripRefreshPatch(unittest.TestCase):
    def _run_patch(
        self, *, exists: bool, block: _FakeBlock | None
    ) -> _FakeBlock:
        """Drive the patch with a mocked Frappe surface. Returns the
        FakeBlock the patch interacted with (existing one for the
        refresh path, new one for the install path)."""
        json_path, _tmp = _make_tmp_json()

        with patch(
            "ecommerce_super.patches.v0_1.refresh_easyecom_top_strip_block.frappe"
        ) as frappe_mock:
            frappe_mock.get_app_path = MagicMock(return_value=str(json_path))
            frappe_mock.db.exists = MagicMock(return_value=exists)
            frappe_mock.db.commit = MagicMock()

            inserted_block = _FakeBlock()

            def _get_doc(*args, **kwargs):
                if exists:
                    return block
                # Fresh-install path — frappe.get_doc(data_dict).
                return inserted_block

            frappe_mock.get_doc = MagicMock(side_effect=_get_doc)

            from ecommerce_super.patches.v0_1 import (
                refresh_easyecom_top_strip_block,
            )

            refresh_easyecom_top_strip_block.execute()

            return block if exists else inserted_block

    def test_refresh_path_rewrites_empty_render_fields(self) -> None:
        """Pre-patch DB row exists with null html/script/style — patch
        sets them all and saves once."""
        empty = _FakeBlock(
            {
                "html": None, "script": None, "style": None,
                "is_standard": 1, "module": "EasyEcom", "private": 0,
            }
        )

        result = self._run_patch(exists=True, block=empty)

        self.assertEqual(result.get("html"), _JSON_PAYLOAD["html"])
        self.assertEqual(result.get("script"), _JSON_PAYLOAD["script"])
        self.assertEqual(result.get("style"), _JSON_PAYLOAD["style"])
        self.assertEqual(result.save_calls, 1)

    def test_idempotent_when_already_in_sync(self) -> None:
        """Pre-patch DB row has identical render payload — patch makes
        zero writes (no save() call)."""
        in_sync = _FakeBlock(
            {
                "html": _JSON_PAYLOAD["html"],
                "script": _JSON_PAYLOAD["script"],
                "style": _JSON_PAYLOAD["style"],
                "is_standard": _JSON_PAYLOAD["is_standard"],
                "module": _JSON_PAYLOAD["module"],
                "private": _JSON_PAYLOAD["private"],
            }
        )

        result = self._run_patch(exists=True, block=in_sync)

        self.assertEqual(result.save_calls, 0)

    def test_fresh_install_inserts_from_json(self) -> None:
        """Block doesn't exist — patch inserts a fresh doc from JSON."""
        result = self._run_patch(exists=False, block=None)

        # The fresh-install path calls .insert(). Our FakeBlock counts
        # both save() and insert() into save_calls.
        self.assertEqual(result.save_calls, 1)

    def test_partial_refresh_only_touches_changed_fields(self) -> None:
        """Pre-patch row has correct html but stale script — patch
        rewrites both stale fields and saves once."""
        partial = _FakeBlock(
            {
                "html": _JSON_PAYLOAD["html"],         # in sync
                "script": "stale-old-script",          # stale
                "style": _JSON_PAYLOAD["style"],       # in sync
                "is_standard": 1, "module": "EasyEcom", "private": 0,
            }
        )

        result = self._run_patch(exists=True, block=partial)

        self.assertEqual(result.get("script"), _JSON_PAYLOAD["script"])
        # html / style left unchanged in value (already correct).
        self.assertEqual(result.get("html"), _JSON_PAYLOAD["html"])
        self.assertEqual(result.get("style"), _JSON_PAYLOAD["style"])
        self.assertEqual(result.save_calls, 1)


if __name__ == "__main__":
    unittest.main()
