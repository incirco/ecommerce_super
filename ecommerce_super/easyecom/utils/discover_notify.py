"""Completion-notification helper for async Discover jobs (gh#11).

The Discover Products / Customers / Suppliers buttons enqueue a long-queue
RQ job and return immediately. The original UX showed "enqueued" and then
went silent — the user had no signal when the job actually finished,
forcing them to refresh the cursor field or eyeball the Map list.

This helper closes that gap with two channels:

  1. Notification Log row — persists in the bell icon so the user can
     see the result later (matches what the user asked for: "should be
     proper notification in the notifications of erpnext").
  2. Realtime event (`easyecom:discover_done`) — instant in-page popup
     via `frappe.publish_realtime`, scoped to the user who triggered
     the job. The form JS subscribes once on init and shows a
     `frappe.show_alert` so the FDE sees the completion immediately if
     they're still in the desk.

When `triggered_by` is None (cron path, no human caller), only the
Notification Log row is written, addressed to EasyEcom FDE role users —
the realtime channel is skipped (nobody to push to in real time).
"""

from __future__ import annotations

from typing import Any

import frappe

# Single canonical event name across all three Discover flows. The form
# JS subscribes to this one event and renders based on `kind`.
REALTIME_EVENT = "easyecom:discover_done"


def notify_discover_complete(
    *,
    triggered_by: str | None,
    kind: str,
    ok: bool,
    summary: str,
    list_route: str | None = None,
    document_type: str | None = None,
    document_name: str | None = None,
) -> None:
    """Tell the triggering user that an async Discover job finished.

    Args:
        triggered_by: User who clicked the Discover button (from the
            web request's `frappe.session.user`, captured at enqueue
            time). None when no human caller (cron / system path).
        kind: One of "Products", "Customers", "Suppliers" — drives the
            subject line and the form-JS dispatch.
        ok: True on successful completion, False when the worker raised.
        summary: Short result line for the body (e.g. "Total: 412 |
            Created: 38 | Failed: 0"). Kept short so it fits the bell
            popover.
        list_route: Optional desk route to the result list (e.g.
            "/app/easyecom-item-map"). Renders as a clickable link in
            both channels.
        document_type / document_name: Optional bell-icon link target;
            if set, clicking the notification jumps to the form.

    Never raises — notification failure must not bubble back into the
    worker's success path.
    """
    try:
        _create_notification_log(
            triggered_by=triggered_by,
            kind=kind,
            ok=ok,
            summary=summary,
            list_route=list_route,
            document_type=document_type,
            document_name=document_name,
        )
    except Exception as e:
        frappe.log_error(
            title="EasyEcom Discover notify — Notification Log failed",
            message=f"{type(e).__name__}: {e}",
        )

    if triggered_by and triggered_by not in ("Administrator", "Guest", ""):
        try:
            frappe.publish_realtime(
                event=REALTIME_EVENT,
                message={
                    "kind": kind,
                    "ok": ok,
                    "summary": summary,
                    "list_route": list_route,
                },
                user=triggered_by,
            )
        except Exception as e:
            # Realtime failure (Redis down, socketio offline) — the bell
            # icon entry above is still authoritative.
            frappe.log_error(
                title="EasyEcom Discover notify — realtime failed",
                message=f"{type(e).__name__}: {e}",
            )


def _create_notification_log(
    *,
    triggered_by: str | None,
    kind: str,
    ok: bool,
    summary: str,
    list_route: str | None,
    document_type: str | None,
    document_name: str | None,
) -> None:
    subject_verb = "completed" if ok else "failed"
    subject = frappe._("EasyEcom: Discover {0} {1}").format(kind, subject_verb)

    body_lines = [summary]
    if list_route:
        body_lines.append(
            f'<a href="{frappe.utils.escape_html(list_route)}">'
            f'Open {frappe.utils.escape_html(kind)} list →</a>'
        )
    body = "<br>".join(body_lines)

    targets = [triggered_by] if triggered_by and triggered_by != "Guest" else []
    if not targets:
        # Cron / system path — fan out to EasyEcom FDE role users so the
        # completion isn't invisible.
        targets = _users_with_role("EasyEcom FDE") or ["Administrator"]

    for user in targets:
        notif = frappe.new_doc("Notification Log")
        notif.update(
            {
                "for_user": user,
                "type": "Alert" if ok else "Energy Point",
                "subject": subject,
                "email_content": body,
                "from_user": "Administrator",
                "document_type": document_type or "",
                "document_name": document_name or "",
            }
        )
        notif.insert(ignore_permissions=True)


def _users_with_role(role: str) -> list[str]:
    return frappe.db.sql_list(
        """SELECT DISTINCT hr.parent
           FROM `tabHas Role` hr
           JOIN `tabUser` u ON u.name = hr.parent
           WHERE hr.role = %s
             AND hr.parenttype = 'User'
             AND u.enabled = 1
             AND u.name NOT IN ('Guest', 'Administrator')""",
        (role,),
    )


def safe_caller() -> str | None:
    """Return the current request's user, or None if no usable session.

    Use this at the enqueue site (the whitelisted endpoint) to capture
    who clicked the button. The captured value is passed into the worker
    via kwargs because RQ workers don't share `frappe.session.user` with
    the originating HTTP request. "Administrator" is treated as no
    usable caller — operating as the superuser is typically a console
    or smoke-test path, not a real FDE.
    """
    user = getattr(frappe.session, "user", None) if frappe.session else None
    if not user or user in ("Guest", "Administrator"):
        return None
    return user
