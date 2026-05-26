"""EasyEcom Supplier Map controller.

SPEC §8.3 — the persistent, explicit correspondence between an
EasyEcom wholesale vendor and an ERPNext Supplier. Mirrors §8e
Customer Map but with a TWO-IDENTIFIER SPLIT:

  - ee_vendor_c_id (this DocType's autoname + join key): READ-side
    `vendor_c_id` from /wms/V2/getVendors. UNIQUE.
  - ee_vendor_id: WRITE-side `vendor_id` (= `vendor_code`), captured
    from CreateVendor response. Consumed by UpdateVendor as `vendorId`.

Unlike §8e Customer (where c_id == customerId observed live), Supplier
has DISTINCT read/write identifiers (e.g. vendor_c_id=166334 +
vendor_code=145 in real Harmony data). PO/STN flows (§9/§10) will use
ee_vendor_id when writing to EE.

Stage 1 builds only the substrate; Stages 2-5 wire the actual
lookups / pull / push / lifecycle / drift flows in.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# §8.3: the link target is restricted to Supplier for now. Same
# dynamic-link convention as §8d Item Map / §8e Customer Map — keeps
# the door open for future entity-sync masters without a schema
# change here.
ALLOWED_LINK_DOCTYPES: frozenset[str] = frozenset({"Supplier"})

# §8.3 status enum — same 5 values as Item Map / Customer Map.
# The enum is entity-agnostic; we just re-declare to keep this
# controller importable without reaching into other flows.
VALID_STATUS_VALUES: frozenset[str] = frozenset(
    {"Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"}
)


class EasyEcomSupplierMap(Document):
    def validate(self) -> None:
        self._validate_link_doctype_allowed()
        self._validate_link_target_exists_when_set()

    def _validate_link_doctype_allowed(self) -> None:
        """Defensive — the form's set_query filters the dropdown to
        Supplier, but API writes can pick any DocType. Refuse anything
        else: §8.3 is explicit about Supplier being the only target."""
        if not self.erpnext_doctype:
            return
        if self.erpnext_doctype not in ALLOWED_LINK_DOCTYPES:
            frappe.throw(
                _(
                    "EasyEcom Supplier Map can only link to {0} — got {1!r}. "
                    "§8.3 restricts the link to that object type."
                ).format(
                    " or ".join(sorted(ALLOWED_LINK_DOCTYPES)),
                    self.erpnext_doctype,
                ),
                title=_("Unsupported Link DocType"),
            )

    def _validate_link_target_exists_when_set(self) -> None:
        """Frappe's Dynamic Link validates the target exists on save —
        but only if erpnext_name is set. A Flagged-Not-Created row may
        have no target (India Compliance rejected the GSTIN/PAN and
        no ERPNext Supplier was created). When BOTH are set, we still
        verify defensively."""
        if self.erpnext_doctype and self.erpnext_name:
            if not frappe.db.exists(self.erpnext_doctype, self.erpnext_name):
                frappe.throw(
                    _(
                        "Linked {0} {1!r} does not exist."
                    ).format(self.erpnext_doctype, self.erpnext_name),
                    title=_("Broken Link"),
                )
