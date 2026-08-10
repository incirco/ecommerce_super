"""EasyEcom Address — child DocType for buyer + shipping address on
Sales Invoice (and potentially other transactional docs later).

Lives as a Table child on Sales Invoice (via the `ecs_ee_address`
Table Custom Field). Frappe stores each row in `tabEasyEcom Address`
with the usual `parent` / `parenttype` / `parentfield` triplet — the
parent DocType (Sales Invoice) itself has ZERO DB columns from this
DocType, which is the whole point (Sales Invoice's InnoDB row is
already close to the 65535-byte limit — see PR #265 context).

No custom validation for now; the values are all EE-supplied strings
that the caller (invoice_builder) already normalised.
"""
from __future__ import annotations

from frappe.model.document import Document


class EasyEcomAddress(Document):
    pass
