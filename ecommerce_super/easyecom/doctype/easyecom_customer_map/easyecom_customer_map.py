"""EasyEcom Customer Map controller.

SPEC §8.2 — the persistent, explicit correspondence between an
EasyEcom wholesale customer (c_id) and an ERPNext Customer. Mirrors
the EasyEcom Item Map pattern from §8.1 / §8d: direction-agnostic,
one row per EE c_id, UNIQUE on ee_c_id at the DB level (the join key).

The link is a Dynamic Link: `erpnext_doctype` ∈ {Customer} +
`erpnext_name`. The dynamic-link shape mirrors §8d so future entity-
sync flows (Supplier, etc.) follow the same substrate.

Stage 1 builds only the substrate; Stages 2-5 wire the actual
lookups / pull / push / lifecycle / drift flows in.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# §8.2: the link target is restricted to Customer for now. The dynamic-
# link shape is deliberate so that future entity-sync masters (e.g.
# Supplier) can extend without a schema change — they'd add their own
# Map DocType, not new options here.
ALLOWED_LINK_DOCTYPES: frozenset[str] = frozenset({"Customer"})

# §8.2 status enum — must match the Select options in the JSON.
# Same five values as Item Map (§8.1.9): the enum is entity-agnostic.
VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {"Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"}
)


class EasyEcomCustomerMap(Document):
    def validate(self) -> None:
        self._validate_link_doctype_allowed()
        self._validate_link_target_exists_when_set()

    def _validate_link_doctype_allowed(self) -> None:
        """Defensive — the form's set_query filters the dropdown to
        Customer, but API writes can pick any DocType. Refuse anything
        else: §8.2 is explicit about Customer being the only target."""
        if not self.erpnext_doctype:
            return
        if self.erpnext_doctype not in ALLOWED_LINK_DOCTYPES:
            frappe.throw(
                _(
                    "EasyEcom Customer Map can only link to {0} — got {1!r}. "
                    "§8.2 restricts the link to that object type."
                ).format(
                    " or ".join(sorted(ALLOWED_LINK_DOCTYPES)),
                    self.erpnext_doctype,
                ),
                title=_("Unsupported Link DocType"),
            )

    def _validate_link_target_exists_when_set(self) -> None:
        """Frappe's Dynamic Link validates the target exists on save —
        but only if erpnext_name is set. We don't require either field
        (a Flagged-Not-Created row may have no target — the EE customer
        was rejected by India Compliance and no ERPNext Customer was
        created). When BOTH are set, we still verify defensively."""
        if self.erpnext_doctype and self.erpnext_name:
            if not frappe.db.exists(self.erpnext_doctype, self.erpnext_name):
                frappe.throw(
                    _(
                        "Linked {0} {1!r} does not exist."
                    ).format(self.erpnext_doctype, self.erpnext_name),
                    title=_("Broken Link"),
                )
