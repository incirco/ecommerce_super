"""EasyEcom Item Map controller.

SPEC §8.1.2 — the persistent, explicit correspondence between an
EasyEcom SKU and an ERPNext Item OR Product Bundle. Direction-agnostic:
a row exists whether the product was born in EE (pulled in) or in
ERPNext (pushed out). UNIQUE on ee_sku at the DB level (the join key).

The link is a Dynamic Link: `erpnext_doctype` ∈ {Item, Product Bundle}
+ `erpnext_name`. Stage 4 bundle component-resolution depends on this
shape — looking up a sub_product's SKU returns either an Item docname
(for a normal sub_product) or a Product Bundle docname (for a nested
combo — though EE doesn't currently allow that, the schema supports it).

Stage 1 builds only the substrate; Stages 2-5 wire the actual
pull/push/lifecycle/drift flows in.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# §8.1.2: the link target is restricted to these two DocTypes.
# Other types (Bin, Batch, etc.) would point at the wrong object —
# Item Defaults / inventory live elsewhere.
ALLOWED_LINK_DOCTYPES: frozenset[str] = frozenset({"Item", "Product Bundle"})

# §8.1.9 status enum — must match the Select options in the JSON.
VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {"Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"}
)


class EasyEcomItemMap(Document):
    def validate(self) -> None:
        self._validate_link_doctype_allowed()
        self._validate_link_target_exists_when_set()

    def _validate_link_doctype_allowed(self) -> None:
        """Defensive — the form's set_query filters the dropdown to
        Item / Product Bundle, but API writes can pick any DocType.
        Refuse anything else: §8.1.2 is explicit about the two types."""
        if not self.erpnext_doctype:
            return
        if self.erpnext_doctype not in ALLOWED_LINK_DOCTYPES:
            frappe.throw(
                _(
                    "EasyEcom Item Map can only link to {0} — got {1!r}. "
                    "§8.1.2 restricts the link to those two object types."
                ).format(
                    " or ".join(sorted(ALLOWED_LINK_DOCTYPES)),
                    self.erpnext_doctype,
                ),
                title=_("Unsupported Link DocType"),
            )

    def _validate_link_target_exists_when_set(self) -> None:
        """Frappe's Dynamic Link validates the target exists on save —
        but only if erpnext_name is set. We don't require either field
        (a Flagged-Not-Created row may have no target — the EE SKU
        wasn't created on the ERPNext side). When BOTH are set,
        Frappe's own validator handles the existence check."""
        if self.erpnext_doctype and self.erpnext_name:
            if not frappe.db.exists(self.erpnext_doctype, self.erpnext_name):
                frappe.throw(
                    _(
                        "Linked {0} {1!r} does not exist."
                    ).format(self.erpnext_doctype, self.erpnext_name),
                    title=_("Broken Link"),
                )
