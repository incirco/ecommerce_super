"""EasyEcom Sync Record Line — child table of EasyEcom Sync Record.

SPEC §7.1.1 / §31.2.3. Carries per-line outcomes for nested-document
flows (GRN→Purchase Receipt §9, Order→Sales Invoice §12, Return §13)
where the unit of work is a composite document but the source payload
has child lines that each need their own structural outcome row.

This file ships with the foundation packet but the table is **empty by
contract until §9**: single-entity flows (Item/Customer/Supplier in §8)
do not populate it. Adding the schema now avoids a migration on the
core EasyEcom Sync Record DocType when §9 lands.

The contract this row helps enforce (§7.3 binary per-record outcome):

  - line_status = OK         → line processed cleanly.
  - line_status = Failed     → line blocked document creation; the
                               parent Sync Record's status is Failed
                               and no ERPNext document was posted.
  - line_status = Discrepancy → line reconciled with a variance beyond
                               tolerance; the parent Sync Record's
                               status is Failed (NEVER Success) and an
                               Integration Discrepancy (§23) is raised.

A discrepancy on any line never softens the parent into a partial-
success state — the parent is binary Success | Failed (§7.3) and any
line that is not OK makes the parent Failed.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomSyncRecordLine(Document):
    pass
