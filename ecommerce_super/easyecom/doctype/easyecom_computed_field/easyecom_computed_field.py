"""EasyEcom Computed Field — child of EasyEcom Field Mapping (§5.6).

Compile-time validation of the expression and reserved-name uniqueness is
performed by the parent's compiler (compiler.py), not the controller —
that way a save that violates a cross-table rule fails with a single
clear error, not one error per child row.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomComputedField(Document):
    pass
