"""§10 Stage 1 — child table on EasyEcom Transfer Map.

Schema-only DocType. Stage 3 populates these when multiple IPRs land
against the same DN (multi-GRN partial receipts). The child wiring is
substrate; no behaviour belongs here.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomTransferIPRLink(Document):
    pass
