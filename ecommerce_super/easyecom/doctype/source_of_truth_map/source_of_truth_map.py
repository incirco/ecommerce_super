"""EasyEcom Source-of-Truth Map controller.

SPEC §8.4.2 / §31.2.23. Per-warehouse mapping declaring which system owns
inventory authority. Built fresh with the §8a Location packet; the §9-§11
flows READ the authority fields (`inventory_master`, `pr_origination`,
`adjustment_origination`, `mirror_stock_reservations`) without extending
the schema.

Validation rules:
  - Warehouse must belong to Company (single-Company invariant).
  - If `ee_location_key` is set, the linked EasyEcom Location's
    `frappe_company` must match this row's Company (or be blank — an
    unmapped location can be on its way to being mapped).
  - `is_linked` is computed: True iff `ee_location_key` is set.
  - The (company, warehouse) UNIQUE composite index is enforced at the
    DB layer via install.after_install (§8a build item 5).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class SourceofTruthMap(Document):
    def validate(self) -> None:
        self._derive_is_linked()
        self._validate_warehouse_in_company()
        self._validate_location_company_match()

    def _derive_is_linked(self) -> None:
        """is_linked is True iff ee_location_key is populated."""
        self.is_linked = 1 if self.ee_location_key else 0

    def _validate_warehouse_in_company(self) -> None:
        if not self.warehouse or not self.company:
            return
        wh_company = frappe.db.get_value("Warehouse", self.warehouse, "company")
        if wh_company and wh_company != self.company:
            frappe.throw(
                _(
                    "Warehouse {0} belongs to Company {1}, not {2}. Source-of-Truth Map "
                    "rows are per-Company."
                ).format(self.warehouse, wh_company, self.company)
            )

    def _validate_location_company_match(self) -> None:
        """If a linked EasyEcom Location has a frappe_company set, it must
        match this row's Company. An EE Location with frappe_company blank
        (To Map / Skipped) is allowed — the map row may pre-stage the
        warehouse side."""
        if not self.ee_location_key:
            return
        loc_company = frappe.db.get_value(
            "EasyEcom Location", self.ee_location_key, "frappe_company"
        )
        if loc_company and loc_company != self.company:
            frappe.throw(
                _(
                    "EasyEcom Location {0} resolves to Company {1}, not {2}. "
                    "Source-of-Truth Map rows must agree with their linked location."
                ).format(self.ee_location_key, loc_company, self.company)
            )
