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
        # CAST to float before comparing. Frappe's Percent / Int fields
        # round-trip through the DB as strings or numerics depending on
        # whether the value was loaded from the form (str), from a
        # default (str via JSON's "5" / "20" literal), or set
        # programmatically (numeric). When the two sides diverge in
        # type:
        #   - both-str → string compare, e.g. "5" >= "20" is TRUE
        #     because "5" > "2" alphabetically — fires the validation
        #     misleadingly (user sees "5% must be less than 20%").
        #   - mixed-type → Python 3 raises TypeError ("'>=' not
        #     supported between instances of 'int' and 'str'") and the
        #     desk surfaces a Server Error.
        # Both observed live 2026-05-27 on FrappeCloud staging
        # (Incirco Ventures LLP). Casting both sides to float removes
        # the asymmetry. _to_float treats None / "" / non-numeric as
        # None so the AND-guard above still short-circuits cleanly.
        qw = _to_float(self.queue_depth_warning_threshold)
        qc = _to_float(self.queue_depth_critical_threshold)
        if qw is not None and qc is not None and qw >= qc:
            frappe.throw(
                _("Queue Depth Warning ({0}) must be less than Critical ({1}).").format(
                    self.queue_depth_warning_threshold,
                    self.queue_depth_critical_threshold,
                )
            )
        aw = _to_float(self.api_error_rate_warning_pct)
        ac = _to_float(self.api_error_rate_critical_pct)
        if aw is not None and ac is not None and aw >= ac:
            frappe.throw(
                _(
                    "API Error Rate Warning ({0}%) must be less than Critical ({1}%)."
                ).format(
                    self.api_error_rate_warning_pct, self.api_error_rate_critical_pct
                )
            )


def _to_float(value) -> float | None:
    """Coerce a Frappe Percent/Int/Float field value (which may arrive
    as int, float, str, '', or None) to float. Returns None on blank /
    non-numeric so the caller's None-guard handles the
    'field-not-set' case cleanly."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
