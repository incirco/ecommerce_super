"""EasyEcom Integration Discrepancy — §23 STUB DocType.

================================================================
FROZEN CONTRACT WARNING — read before touching this file
================================================================

This is a minimal 7-field stub shipped pre-§9-Stage-2 (2026-05-28)
so the typed Link contract on `EasyEcom Sync Record Line.
ecs_integration_discrepancy` actually holds at runtime. Before
this stub existed, the Link pointed at a non-existent target;
Frappe tolerates that at DocType-definition time but rejects any
non-null write at runtime.

The §9 Stage 3 flow will raise discrepancies via this DocType:
  - po_status_drift           — PO Map ee_observed_po_status diverges from
                                 last_pushed_po_status in a way that
                                 indicates EE-side action contrary to
                                 ERPNext (e.g. EE→4 Rejected when
                                 ERPNext shows Approved=3).
  - grn_tolerance_breach      — GRN received_quantity exceeds PO
                                 original_quantity beyond the
                                 allow_over_receipt_pct threshold.
  - hsn_mismatch              — GRN line hsn != Item.gst_hsn_code.
  - deleted_post_receipt      — EE flipped grn_status_id to 4 after we
                                 already posted the PR.
  - out_of_order_grn          — GRN for an EE-born PO ERPNext doesn't
                                 know yet (linked_po_map empty).

§23 will EXTEND this DocType significantly:
  - Workflow (Open → In Progress → Escalated → Resolved).
  - SLA tracking (raised_at, sla_breach_at, escalation chain).
  - Routing (assigned_to, watchers).
  - Resolution actions (suppress-for-N-days, escalate-to-SM, etc.).
  - Cross-discrepancy correlation (de-dup, parent/child).
  - Notification + alert hooks (§18, §27 Error Translation).
  - Per-Company permission filters.
  - Reports + dashboard cards.

The FROZEN CONTRACT this stub establishes for §9 (and §11/§12/§13
that will also write discrepancies):

  1. `kind` exists, is reqd, accepts string. §23 may add a closed
     vocabulary but must NOT remove the field.
  2. `status` enum INCLUDES {Open, Resolved, Dismissed}. §23 may
     ADD intermediate states; it must NOT remove these three or
     reassign their semantic meaning.
  3. `reference_doctype` + `reference_name` form a Dynamic Link
     pair. §9+ flows write here; §23 may add resolution actions
     that operate on the linked doc but must not change the
     pairing.
  4. `company` is reqd and Link → Company. §23 may add
     Company-scoped permission rules; the field stays.
  5. `reason` exists and is reqd Long Text. §23 may add structured
     diff fields but must keep reason as the free-text narrative.
  6. `resolution_note` is optional Small Text. §23 may add
     structured resolution metadata; the free-text field stays.

§9 will populate this row when raising discrepancies. The Stage 3
flow code will:
  doc = frappe.get_doc({
      "doctype": "EasyEcom Integration Discrepancy",
      "kind": "grn_tolerance_breach",
      "status": "Open",
      "reference_doctype": "EasyEcom GRN Map",
      "reference_name": grn_map_name,
      "company": grn_map.company,  # derived from warehouse →
                                    # company resolution
      "reason": "GRN 141653 received_quantity (105) exceeds PO "
                "original_quantity (100) by 5% — over-tolerance "
                "threshold (3%) breached",
  })
  doc.insert(ignore_permissions=True)

Then on the Sync Record Line:
  line.ecs_integration_discrepancy = doc.name

Both Link directions work at runtime once this stub exists.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {"Open", "Resolved", "Dismissed"}
)


class EasyEcomIntegrationDiscrepancy(Document):
    def validate(self) -> None:
        self._validate_status_value()
        self._validate_reference_target_exists()

    def _validate_status_value(self) -> None:
        if self.status and self.status not in VALID_STATUS_VALUES:
            frappe.throw(
                _(
                    "EasyEcom Integration Discrepancy status must be one of "
                    "{0} — got {1!r}. (§23 may add intermediate states later; "
                    "the three terminal-equivalent values listed are the "
                    "frozen contract.)"
                ).format(
                    ", ".join(sorted(VALID_STATUS_VALUES)), self.status
                ),
                frappe.ValidationError,
            )

    def _validate_reference_target_exists(self) -> None:
        """Dynamic-Link layer accepts any DocType + name pair at the JSON
        layer; verify the row actually exists before allowing the insert.
        Prevents typo'd references from sitting open in the worklist
        pointing at nothing."""
        if not self.reference_doctype or not self.reference_name:
            return
        if not frappe.db.exists(self.reference_doctype, self.reference_name):
            frappe.throw(
                _(
                    "Reference {0} {1!r} does not exist — cannot raise a "
                    "discrepancy against a non-existent row."
                ).format(self.reference_doctype, self.reference_name),
                frappe.ValidationError,
            )
