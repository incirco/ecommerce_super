"""gh#3 (re-open) — `after_install` must invoke the Top Strip rescue.

The rescue patch (`insert_easyecom_top_strip_from_inline`) is registered
in `patches.txt`, but `bench install-app` stamps every registered patch
as already-applied in `tabPatch Log` without running its body. The
result: fresh installs never plant the Custom HTML Block row, and the
EasyEcom Control Panel renders "undefined" in the Operational Status
section.

Fix: `after_install` calls the patch's `execute()` directly so the row
gets planted on fresh installs too. Upgrade-time provisioning still
runs via patches.txt for benches that bootstrapped before this commit.

This test pins the call so future hands-off rearrangements of
`after_install` don't silently drop the rescue.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ecommerce_super import install as install_mod


class TestAfterInstallInvokesTopStripRescue(unittest.TestCase):
    """The presence of the call is the load-bearing fact — the patch's
    own `execute()` behaviour is covered by
    `test_top_strip_inline_patch.py`. We just confirm wire-up here."""

    def test_after_install_calls_top_strip_rescue(self) -> None:
        with (
            patch.object(install_mod, "_add_composite_indexes") as add_idx,
            patch.object(install_mod, "_run_custom_field_audit") as audit,
            patch(
                "ecommerce_super.patches.v0_1."
                "insert_easyecom_top_strip_from_inline.execute"
            ) as rescue,
            patch("frappe.db.commit") as commit,
        ):
            install_mod.after_install()

        add_idx.assert_called_once()
        audit.assert_called_once()
        rescue.assert_called_once()
        commit.assert_called_once()

    def test_top_strip_rescue_failure_does_not_abort_install(self) -> None:
        """A rescue exception is logged and swallowed — the rest of
        after_install (composite indexes, custom field audit, commit)
        must still run."""
        with (
            patch.object(install_mod, "_add_composite_indexes") as add_idx,
            patch.object(install_mod, "_run_custom_field_audit") as audit,
            patch(
                "ecommerce_super.patches.v0_1."
                "insert_easyecom_top_strip_from_inline.execute",
                side_effect=RuntimeError("simulated DB hiccup"),
            ),
            patch("frappe.log_error") as log_error,
            patch("frappe.db.commit") as commit,
        ):
            install_mod.after_install()  # must not raise

        add_idx.assert_called_once()
        audit.assert_called_once()
        log_error.assert_called_once()
        commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
