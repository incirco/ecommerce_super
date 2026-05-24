"""EasyEcom Field Mapping Rule — child of EasyEcom Field Mapping (§5.3).

Per-rule compile-time validation (path syntax, transformer args contract,
condition expression sandbox check) is performed by the parent's compiler
(compiler.py), not the controller. This keeps the error message authoring
in one place and lets the compiler report rule_index for the FDE.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomFieldMappingRule(Document):
    pass
