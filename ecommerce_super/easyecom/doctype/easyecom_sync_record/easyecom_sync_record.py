"""EasyEcom Sync Record controller.

Entity-centric. One per (ERPNext doc, sync direction). Mutable in place
across retries — NOT append-only (contrast API Call and Webhook Event).

The composite UNIQUE constraint (company, entity_doctype, entity_name,
direction) is enforced at the DB level via an index added in
install.after_install. The controller's `find_or_create` is the canonical
upsert path so callers never construct duplicates by accident.
"""

from __future__ import annotations

from typing import Any, Callable

import frappe
from frappe import _
from frappe.model.document import Document

# Valid status transitions (defensive — the integration owns transitions, but
# the controller validates them so any out-of-band UPDATE is caught).
#
# Per SPEC §7.3 the per-record outcome is BINARY — Success | Failed. Drift
# findings (the §8d "succeeded-but-found-divergence" case) write Failed with
# the divergence detail captured in last_error and on child line_status.
# This map intentionally omits "Discrepancy" as a parent state — that was a
# pre-§7.3-correction artefact migrated out by gh#16.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "Pending": {"Running", "Cancelled", "AlreadySynced"},
    "Running": {"Success", "Failed", "Pending"},
    "Failed": {"Pending", "Cancelled"},  # FDE retry returns to Pending
    "Success": set(),  # terminal
    "Cancelled": set(),  # terminal
    "AlreadySynced": {"Pending"},  # FDE force-resync
}


class EasyEcomSyncRecord(Document):
    def validate(self) -> None:
        self._validate_entity_doctype_exists()
        self._validate_status_transition()
        self._validate_company_not_changed()
        self._recompute_lines_summary()

    def _recompute_lines_summary(self) -> None:
        """§9 Stage 4 — line-child outcome chip for the list view.

        The Sync Record's `lines` child carries per-line outcomes for
        nested-document flows (GRN line × N). The list view needs a
        compact, sortable indicator like '8/10 OK · 2 Discrepancy'
        without loading the child table for every row. We derive it
        on save and store on the parent.
        """
        lines = self.get("lines") or []
        total = len(lines)
        if total == 0:
            self.ecs_lines_summary = ""
            return
        counts = {"OK": 0, "Failed": 0, "Discrepancy": 0}
        for ln in lines:
            st = (ln.line_status or "").strip()
            if st in counts:
                counts[st] += 1
        parts = [f"{counts['OK']}/{total} OK"]
        if counts["Failed"]:
            parts.append(f"{counts['Failed']} Failed")
        if counts["Discrepancy"]:
            parts.append(f"{counts['Discrepancy']} Discrepancy")
        self.ecs_lines_summary = " · ".join(parts)

    def _validate_entity_doctype_exists(self) -> None:
        if self.entity_doctype and not frappe.db.exists("DocType", self.entity_doctype):
            frappe.throw(
                _("entity_doctype {0} is not a known DocType.").format(
                    self.entity_doctype
                )
            )

    def _validate_status_transition(self) -> None:
        if self.is_new() or not self.get_doc_before_save():
            return
        prior = self.get_doc_before_save()
        if not prior:
            return
        if prior.status == self.status:
            return
        allowed = ALLOWED_TRANSITIONS.get(prior.status, set())
        if self.status not in allowed:
            frappe.throw(
                _("Sync Record cannot transition {0} → {1} (allowed: {2}).").format(
                    prior.status,
                    self.status,
                    ", ".join(sorted(allowed)) or "(terminal)",
                )
            )

    def _validate_company_not_changed(self) -> None:
        if self.is_new() or not self.get_doc_before_save():
            return
        prior = self.get_doc_before_save()
        if prior and prior.company != self.company:
            frappe.throw(_("Sync Record Company cannot be changed once set."))


def find_existing(
    *,
    company: str,
    entity_doctype: str,
    entity_name: str,
    direction: str,
) -> EasyEcomSyncRecord | None:
    """Return the (single) Sync Record for this (entity, direction), or None."""
    name = frappe.db.get_value(
        "EasyEcom Sync Record",
        {
            "company": company,
            "entity_doctype": entity_doctype,
            "entity_name": entity_name,
            "direction": direction,
        },
        "name",
    )
    if not name:
        return None
    return frappe.get_doc("EasyEcom Sync Record", name)


def upsert(
    *,
    company: str,
    entity_doctype: str,
    entity_name: str,
    entity_type: str,
    direction: str,
    correlation_id: str,
    idempotency_key: str,
    ee_location_key: str | None = None,
    parent_correlation_id: str | None = None,
    status: str = "Pending",
    **extra: Any,
) -> EasyEcomSyncRecord:
    """Get-or-create a Sync Record for this (entity, direction).

    Honours the composite-uniqueness invariant (§31.2.3 / §6.7). If an
    existing record is found, it is returned unchanged — the caller decides
    whether to mutate (e.g. bump attempts on retry) or no-op.
    """
    existing = find_existing(
        company=company,
        entity_doctype=entity_doctype,
        entity_name=entity_name,
        direction=direction,
    )
    if existing:
        return existing

    doc = frappe.new_doc("EasyEcom Sync Record")
    doc.update(
        {
            "company": company,
            "entity_doctype": entity_doctype,
            "entity_name": entity_name,
            "entity_type": entity_type,
            "direction": direction,
            "status": status,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "ee_location_key": ee_location_key,
            "parent_correlation_id": parent_correlation_id,
            **extra,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


@frappe.whitelist()
def retry_now(sync_record_name: str) -> dict:
    """FDE-facing Retry Now action (§6.5.1).

    Behaviour per §6.5.1:
      - Status moves Failed → Pending (the AlreadySynced→Pending
        transition is also allowed for force-resync, but force-resync
        proper is §6.5.4 / §8 — out of scope here).
      - `attempts` counter UNCHANGED — the inherited attempts history is
        preserved per §6.1 'retry inherits original key', not reset.
      - `last_error` and `last_error_translation_key` cleared.
      - The original idempotency_key is REUSED (the row's existing
        `idempotency_key` field is untouched). Per §6.1, retries never
        recompute the key.

    Re-enqueue (the actual job dispatch) is dispatched per
    `entity_doctype` via `_REFIRE_HANDLERS` below. Doctypes not in the
    registry fall back to the original "flip flag, let the next
    polling tick pick it up" behaviour — preserved for backwards
    compatibility with flows that have their own re-fire path.

    gh#86: §10 Delivery Note Sync Records used to be stranded in
    Pending after Retry because there's no §10 polling tick — the
    push only fires on `DN.on_submit`, and an already-submitted DN
    can't re-fire that hook. The Delivery Note handler below enqueues
    a Transfer Push job so the retry actually re-runs the push.
    """
    doc = frappe.get_doc("EasyEcom Sync Record", sync_record_name)
    if doc.status not in {"Failed", "Cancelled"}:
        frappe.throw(
            frappe._(
                "Only Failed or Cancelled Sync Records can be retried; this one is {0}."
            ).format(doc.status)
        )

    doc.db_set(
        {
            "status": "Pending",
            "last_error": None,
            "last_error_translation_key": None,
        },
        update_modified=False,
        commit=True,
    )

    refire_result = _refire_pending_sync_record(doc)

    return {
        "name": sync_record_name,
        "status": "Pending",
        "attempts_preserved": doc.attempts,
        "idempotency_key_preserved": doc.idempotency_key,
        "refire": refire_result,
    }


def _refire_pending_sync_record(doc: Document) -> dict:
    """Dispatch a re-fire for the now-Pending Sync Record based on its
    `entity_doctype`. Returns a small dict the caller surfaces to the
    FDE so they know whether the retry actually fired or just flipped
    the flag.

    Doctypes without a registered handler return
    `{"enqueued": False, "reason": "no handler"}` — preserved
    backwards-compat for flows that own their own re-fire path.
    """
    handler = _REFIRE_HANDLERS.get(doc.entity_doctype)
    if handler is None:
        return {
            "enqueued": False,
            "reason": (
                f"No retry-refire handler registered for entity_doctype "
                f"{doc.entity_doctype!r} — the Sync Record is now in "
                "Pending and will be picked up by the flow's next "
                "polling tick (if any)."
            ),
        }
    try:
        return handler(doc)
    except Exception as exc:
        # Don't unflip the status — leaving it in Pending is no worse
        # than the pre-fix behaviour, and the FDE gets a clear error.
        frappe.log_error(
            title=(
                f"gh#86: retry re-fire handler raised for {doc.name} "
                f"({doc.entity_doctype})"
            ),
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "enqueued": False,
            "reason": (
                f"Retry re-fire handler raised: "
                f"{type(exc).__name__}: {exc}"
            ),
        }


def _refire_delivery_note(doc: Document) -> dict:
    """§10 Transfer Push re-fire. Enqueues a Transfer Push job for the
    DN named by `entity_name`. Mirrors the enqueue shape used by the
    batch sweep in `transfer_push.push_all_pending_transfers`."""
    from ecommerce_super.easyecom.queue import enqueue_easyecom_job
    from ecommerce_super.easyecom.utils.idempotency import internal_job_key

    job = enqueue_easyecom_job(
        job_type="Transfer Push",
        company=doc.company,
        target_doctype="Delivery Note",
        target_name=doc.entity_name,
        payload={"dn_name": doc.entity_name},
        idempotency_key=internal_job_key(
            job_type="transfer_push",
            company=doc.company,
            target_doctype="Delivery Note",
            target_name=doc.entity_name,
        ),
    )
    return {
        "enqueued": True,
        "job_type": "Transfer Push",
        "queue_job_name": getattr(job, "name", None) if job else None,
    }


# Registry of per-doctype retry re-fire handlers. Each handler takes
# the now-Pending Sync Record doc and returns a `{enqueued, ...}` dict.
# gh#86: §10 Delivery Note was the originally-reported entity; add
# other entity_doctypes here as their flows ship if Retry needs to
# re-fire something more than a status flip.
_REFIRE_HANDLERS: dict[str, Callable[[Document], dict]] = {
    "Delivery Note": _refire_delivery_note,
}
