"""EasyEcom Queue Job controller.

Tracks async work units (push, pull, retry, etc.). Distinct from API Call —
one Queue Job may spawn multiple API Calls across its retries.

Two-layer model (§6.3.1):
  - Frappe RQ executes the actual worker. The tracking row created here is
    a separate observability artefact carrying correlation_id, idempotency
    key, business state, and per-record counts that bare RQ does not provide.

State machine (§6.3.3):
  Queued → Running → {Success | Partial | Failed | Retrying | Cancelled}
  Retrying → Running (when picked up again)
  Failed → Queued (via manual Retry)
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# State transitions per §6.3.3. Empty set means terminal.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "Queued": {"Running", "Cancelled"},
    "Running": {"Success", "Partial", "Failed", "Retrying"},
    "Retrying": {"Running", "Failed", "Cancelled"},
    "Success": set(),
    "Partial": set(),
    "Failed": {"Queued"},  # FDE manual Retry returns to Queued
    "Cancelled": set(),
}


class EasyEcomQueueJob(Document):
    def validate(self) -> None:
        if not self.created_at:
            self.created_at = frappe.utils.now_datetime()
        self._validate_state_transition()
        self._validate_partial_counts()

    def _validate_state_transition(self) -> None:
        if self.is_new() or not self.get_doc_before_save():
            return
        prior = self.get_doc_before_save()
        if not prior or prior.state == self.state:
            return
        allowed = ALLOWED_TRANSITIONS.get(prior.state, set())
        if self.state not in allowed:
            frappe.throw(
                _("Queue Job cannot transition {0} → {1} (allowed: {2}).").format(
                    prior.state, self.state, ", ".join(sorted(allowed)) or "(terminal)"
                )
            )

    def _validate_partial_counts(self) -> None:
        """A Partial job must have both succeeded_count > 0 and failed_count > 0
        (§6.3.3). Pure-success → Success; pure-failure → Failed."""
        if self.state != "Partial":
            return
        if not self.succeeded_count or not self.failed_count:
            frappe.throw(
                _(
                    "Partial state requires both succeeded_count and failed_count > 0. "
                    "All success → Success; all failure → Failed."
                )
            )

    # ----- State transition helpers (used by workers.py) -----

    def transition_to_running(self) -> None:
        """Move Queued/Retrying → Running and bump attempts."""
        self.db_set(
            {
                "state": "Running",
                "attempts": (self.attempts or 0) + 1,
                "last_attempted_at": frappe.utils.now_datetime(),
                "rq_job_id": (
                    frappe.get_running_job_id()
                    if hasattr(frappe, "get_running_job_id")
                    else None
                ),
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()

    def transition_to_success(self) -> None:
        self.db_set(
            {
                "state": "Success",
                "completed_at": frappe.utils.now_datetime(),
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()

    def transition_to_failed(
        self, *, error: str, translation_key: str | None = None
    ) -> None:
        self.db_set(
            {
                "state": "Failed",
                "completed_at": frappe.utils.now_datetime(),
                "last_error": error[:65000],  # Long Text limit safety
                "last_error_translation_key": translation_key,
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()

    def transition_to_retrying(
        self,
        *,
        next_attempt_at,
        error: str,
        translation_key: str | None = None,
    ) -> None:
        self.db_set(
            {
                "state": "Retrying",
                "next_attempt_at": next_attempt_at,
                "last_error": error[:65000],
                "last_error_translation_key": translation_key,
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()

    def cancel(self, reason: str) -> None:
        if self.state in {"Success", "Partial", "Failed", "Cancelled"}:
            return  # already terminal
        self.db_set(
            {
                "state": "Cancelled",
                "completed_at": frappe.utils.now_datetime(),
                "last_error": f"Cancelled: {reason}",
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()
