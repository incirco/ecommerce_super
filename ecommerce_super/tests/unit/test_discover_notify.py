"""gh#11 — `notify_discover_complete` helper.

Covers the two channels the helper drives:
  1. Notification Log row (persists in the bell icon).
  2. Realtime publish (instant in-page popup; skipped for system callers).

These are unit tests with frappe primitives mocked — the helper is pure
glue, no DB, no socketio. The integration paths (workers actually call
the helper on success/failure) are exercised by the existing pull
integration tests via a separate burst scenario.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.utils.discover_notify import (
    REALTIME_EVENT,
    notify_discover_complete,
    safe_caller,
)


class TestNotifyDiscoverComplete(unittest.TestCase):
    def _patch_frappe(self):
        """Patches the three frappe surfaces the helper touches."""
        new_doc = MagicMock(return_value=MagicMock())
        publish = MagicMock()
        log_error = MagicMock()
        users_with_role = MagicMock(return_value=["fde-user-1@x.com"])
        return new_doc, publish, log_error, users_with_role

    def _run(
        self,
        *,
        triggered_by,
        ok=True,
        summary="Total: 10 | Created: 8",
        list_route="/app/easyecom-item-map",
        kind="Products",
    ):
        new_doc, publish, log_error, users_with_role = self._patch_frappe()
        with (
            patch.object(frappe, "new_doc", new_doc),
            patch.object(frappe, "publish_realtime", publish),
            patch.object(frappe, "log_error", log_error),
            patch(
                "ecommerce_super.easyecom.utils.discover_notify._users_with_role",
                users_with_role,
            ),
        ):
            notify_discover_complete(
                triggered_by=triggered_by,
                kind=kind,
                ok=ok,
                summary=summary,
                list_route=list_route,
            )
        return new_doc, publish, log_error, users_with_role

    def test_real_user_gets_notification_log_and_realtime(self) -> None:
        new_doc, publish, _, _ = self._run(triggered_by="fde-real@x.com")
        # One Notification Log row for the user.
        new_doc.assert_called_once_with("Notification Log")
        # Realtime published to that user with the right shape.
        publish.assert_called_once()
        call_kwargs = publish.call_args.kwargs
        self.assertEqual(call_kwargs["event"], REALTIME_EVENT)
        self.assertEqual(call_kwargs["user"], "fde-real@x.com")
        msg = call_kwargs["message"]
        self.assertEqual(msg["kind"], "Products")
        self.assertTrue(msg["ok"])
        self.assertEqual(msg["list_route"], "/app/easyecom-item-map")
        self.assertIn("Created", msg["summary"])

    def test_administrator_caller_skips_realtime_fans_out_to_fde_role(self) -> None:
        new_doc, publish, _, users_with_role = self._run(triggered_by=None)
        # No realtime publish — there's no human in the desk to push to.
        publish.assert_not_called()
        # Notification Log written to each FDE-role user (one per row).
        users_with_role.assert_called_once_with("EasyEcom FDE")
        self.assertEqual(new_doc.call_count, 1)  # one FDE user in the mock

    def test_failure_path_still_notifies(self) -> None:
        new_doc, publish, _, _ = self._run(
            triggered_by="fde-real@x.com",
            ok=False,
            summary="Discover Products failed: ValueError: boom",
            list_route="/app/error-log",
        )
        new_doc.assert_called_once_with("Notification Log")
        publish.assert_called_once()
        self.assertFalse(publish.call_args.kwargs["message"]["ok"])

    def test_notification_log_failure_does_not_block_realtime(self) -> None:
        """If creating the Notification Log row raises, the realtime
        publish should still fire (and the notification failure should
        land in Error Log instead of bubbling up)."""
        publish = MagicMock()
        log_error = MagicMock()
        bad_new_doc = MagicMock(side_effect=Exception("disk full"))
        with (
            patch.object(frappe, "new_doc", bad_new_doc),
            patch.object(frappe, "publish_realtime", publish),
            patch.object(frappe, "log_error", log_error),
            patch(
                "ecommerce_super.easyecom.utils.discover_notify._users_with_role",
                MagicMock(return_value=[]),
            ),
        ):
            notify_discover_complete(
                triggered_by="fde-real@x.com",
                kind="Customers",
                ok=True,
                summary="ok",
            )
        # Notification Log failure was logged.
        log_error.assert_called()
        # Realtime still fired.
        publish.assert_called_once()

    def test_realtime_failure_does_not_raise(self) -> None:
        """If realtime publish itself raises (Redis down), the helper
        must swallow it — caller's success path can't degrade because
        the bell-icon notification was already written."""
        new_doc = MagicMock(return_value=MagicMock())
        bad_publish = MagicMock(side_effect=Exception("redis down"))
        log_error = MagicMock()
        with (
            patch.object(frappe, "new_doc", new_doc),
            patch.object(frappe, "publish_realtime", bad_publish),
            patch.object(frappe, "log_error", log_error),
            patch(
                "ecommerce_super.easyecom.utils.discover_notify._users_with_role",
                MagicMock(return_value=[]),
            ),
        ):
            # Should NOT raise.
            notify_discover_complete(
                triggered_by="fde-real@x.com",
                kind="Suppliers",
                ok=True,
                summary="ok",
            )
        log_error.assert_called()


class TestSafeCaller(unittest.TestCase):
    def _with_user(self, user):
        sess = MagicMock()
        sess.user = user
        return patch.object(frappe, "session", sess)

    def test_real_user_passes_through(self) -> None:
        with self._with_user("fde-real@x.com"):
            self.assertEqual(safe_caller(), "fde-real@x.com")

    def test_administrator_is_none(self) -> None:
        with self._with_user("Administrator"):
            self.assertIsNone(safe_caller())

    def test_guest_is_none(self) -> None:
        with self._with_user("Guest"):
            self.assertIsNone(safe_caller())

    def test_empty_is_none(self) -> None:
        with self._with_user(""):
            self.assertIsNone(safe_caller())


if __name__ == "__main__":
    unittest.main()
