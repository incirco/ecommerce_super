"""EasyEcom Company MultiSelect — reusable child table of Company links.

Used by Field Mapping's `company_scope` (empty = all companies). Mirrors
EasyEcom Item Group MultiSelect.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomCompanyMultiSelect(Document):
    pass
