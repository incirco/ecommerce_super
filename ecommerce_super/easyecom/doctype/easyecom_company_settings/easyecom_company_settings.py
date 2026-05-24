"""EasyEcom Company Settings controller.

Per-Company configuration (SPEC §3.5). One record per operational Company.

Validation rules:
  - company is immutable post-create (autoname field; Frappe enforces this
    via the autoname mechanism, but we additionally guard against form-level
    rename attempts).
  - daily_digest_time must be a valid time.
  - alert recipient channels match the closed vocabulary.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EasyEcomCompanySettings(Document):
    def validate(self) -> None:
        self._validate_company_immutable()
        self._validate_alert_channels()
        self._validate_thresholds()

    def _validate_company_immutable(self) -> None:
        if self.is_new() or not self.get_doc_before_save():
            return
        prior = self.get_doc_before_save()
        if prior and prior.company != self.company:
            frappe.throw(
                _(
                    "Company is immutable on EasyEcom Company Settings — create a new record for a different Company."
                ),
                title=_("Cannot Change Company"),
            )

    def _validate_alert_channels(self) -> None:
        valid = {"Notification", "Email", "Slack"}
        for table_field in (
            "alert_recipients_critical",
            "alert_recipients_error",
            "alert_recipients_warning",
        ):
            for row in self.get(table_field) or []:
                if row.channel not in valid:
                    frappe.throw(
                        _("Alert channel {0} on row {1} is not one of {2}.").format(
                            row.channel, row.idx, ", ".join(sorted(valid))
                        )
                    )

    def _validate_thresholds(self) -> None:
        if (
            self.queue_depth_warning_threshold
            and self.queue_depth_critical_threshold
            and self.queue_depth_warning_threshold
            >= self.queue_depth_critical_threshold
        ):
            frappe.throw(
                _("Queue Depth Warning ({0}) must be less than Critical ({1}).").format(
                    self.queue_depth_warning_threshold,
                    self.queue_depth_critical_threshold,
                )
            )
        if (
            self.api_error_rate_warning_pct
            and self.api_error_rate_critical_pct
            and self.api_error_rate_warning_pct >= self.api_error_rate_critical_pct
        ):
            frappe.throw(
                _(
                    "API Error Rate Warning ({0}%) must be less than Critical ({1}%)."
                ).format(
                    self.api_error_rate_warning_pct, self.api_error_rate_critical_pct
                )
            )
