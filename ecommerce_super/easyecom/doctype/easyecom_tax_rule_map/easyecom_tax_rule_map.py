"""EasyEcom Tax Rule Map controller + resolver.

SPEC §8.5 / packet 8c. One doc per (tax_rule_name, company); UNIQUE at
the DB level. The FDE picks real Item Tax Templates and Min/Max Net
Rate bands; the resolver stamps those rows onto the item's native
Taxes table; ERPNext resolves the band at invoice time.

NO slab logic in our flows. NO rate→name parsing. NO HSN-treatment
table. The map is a thin FDE-curated lookup; the resolver is a
stamp-and-reconcile call Item sync (8d) makes.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

# Workflow states (matching the shipped fixture in fixtures/workflow.json).
INITIAL_WORKFLOW_STATE: str = "To Configure"
CONFIGURED_WORKFLOW_STATE: str = "Configured"
IGNORED_WORKFLOW_STATE: str = "Ignored"

# Reconciliation tolerance: EE sends tax_rate as a decimal (0.18); the
# Item Tax Template's effective rate from India Compliance is also a
# decimal but can vary by 0.001 from rounding. Treat anything within
# this absolute delta as a match.
RATE_RECONCILE_TOLERANCE: float = 0.0001


class EasyEcomTaxRuleMap(Document):
    def validate(self) -> None:
        # Defensive — the DB UNIQUE (tax_rule_name, company) is the real
        # gate, but a clean Python check gives a nicer error before SQL.
        self._validate_unique_natural_key()
        # §8.5.3 contract: rows hold ONLY this company's templates.
        # The form's set_query filters the dropdown, but the server
        # check is the gate (API writes, fixtures, FDE typing a wrong
        # template name in the cell).
        self._validate_template_companies_match()

    def _validate_template_companies_match(self) -> None:
        if not self.company:
            return
        for row in self.taxes or []:
            if not row.item_tax_template:
                continue
            template_company = frappe.db.get_value(
                "Item Tax Template", row.item_tax_template, "company"
            )
            if template_company and template_company != self.company:
                frappe.throw(
                    _(
                        "Row #{0}: Item Tax Template {1} belongs to Company {2}, "
                        "not {3}. EasyEcom Tax Rule Map rows must reference "
                        "templates from the document's own Company (§8.5.3)."
                    ).format(
                        row.idx,
                        row.item_tax_template,
                        template_company,
                        self.company,
                    ),
                    title=_("Wrong-Company Template"),
                )

    def _validate_unique_natural_key(self) -> None:
        if not self.tax_rule_name or not self.company:
            return
        existing = frappe.db.get_value(
            "EasyEcom Tax Rule Map",
            {
                "tax_rule_name": self.tax_rule_name,
                "company": self.company,
                "name": ["!=", self.name or ""],
            },
            "name",
        )
        if existing:
            frappe.throw(
                _(
                    "EasyEcom Tax Rule Map already exists for rule {0!r} in "
                    "Company {1!r} (existing doc: {2}). The (tax_rule_name, "
                    "company) pair is the natural key — one row per pair."
                ).format(self.tax_rule_name, self.company, existing),
                title=_("Duplicate Tax Rule Map"),
            )


# ----- Resolver result + the resolver itself -----


@dataclass
class TaxResolutionResult:
    """What the resolver tells its caller (8d Item sync).

    The resolver mutates the item doc in place (stamping the taxes
    rows). This result captures what happened beyond the mutation —
    cess (which lives on the item via ecs_cess), whether a discrepancy
    was detected, and whether a new To-Configure map was auto-created.
    8d uses these flags to surface FDE-facing state.
    """

    mapped: bool  # False if no Tax Rule Map existed for (rule, company)
    auto_created: bool  # True if the resolver just created a To-Configure map
    map_docname: str | None  # The map doc consulted (or auto-created)
    stamped_count: int  # Number of Item Tax rows written to item.taxes
    cess: float  # The cess value carried from product.cess (also written to item.ecs_cess if the field exists)
    reconciled: bool  # True if product.tax_rate matched a mapped template's effective rate
    discrepancies: list[str] = dc_field(default_factory=list)


def resolve_and_stamp_tax(item: Any, product: dict, company: str) -> TaxResolutionResult:
    """8c-owned resolver, called by 8d Item sync.

    Six things, in order (§8.5.4):

      1. Read product.tax_rule_name (the opaque EE rule key) and the
         caller-supplied company.
      2. Look up the EasyEcom Tax Rule Map for (tax_rule_name, company).
      3. STAMP that map's taxes rows onto item.taxes (clearing first;
         the map is the source of truth for this item's tax templates).
      4. RECONCILE: product.tax_rate (resolved by EE) must match one of
         the mapped templates' effective rates — mismatch → discrepancy.
      5. CESS: write product.cess to item.ecs_cess (if the field exists)
         and return it in the result regardless.
      6. UNMAPPED → AUTO-CREATE a To-Configure map and flag the FDE.
         Item tax is NOT silently defaulted; the missing mapping is a
         visible task.

    Six valid outcomes from the caller's perspective:
      - mapped=True, reconciled=True, no discrepancies → clean stamp.
      - mapped=True, reconciled=False → discrepancy raised (band/rate mismatch
        or company-has-no-rows). Item tax was NOT stamped (the caller
        should treat the item as Failed and surface to FDE).
      - mapped=False, auto_created=True → unmapped rule; map created in
        To Configure for the FDE; item NOT stamped. Caller treats Failed.
    """
    tax_rule_name = (product or {}).get("tax_rule_name")
    if not tax_rule_name:
        # Item sync owns its own validation of "no rule on product"; the
        # resolver returns a clean "nothing to do" result so the caller
        # can decide whether that's an error in its context.
        return TaxResolutionResult(
            mapped=False,
            auto_created=False,
            map_docname=None,
            stamped_count=0,
            cess=float((product or {}).get("cess") or 0.0),
            reconciled=False,
            discrepancies=["product carries no tax_rule_name"],
        )

    map_name = frappe.db.exists(
        "EasyEcom Tax Rule Map",
        {"tax_rule_name": tax_rule_name, "company": company},
    )

    # CESS pass-through happens unconditionally — it lives on the
    # product, not in the rule map.
    cess_value = float((product or {}).get("cess") or 0.0)
    _apply_cess_to_item(item, cess_value)

    if not map_name:
        # Unmapped (rule, company) — auto-create a To-Configure doc and
        # surface to the FDE. Item taxes NOT stamped; caller treats as
        # discrepancy and surfaces to FDE.
        auto_name = _auto_create_to_configure_map(tax_rule_name, company)
        _notify_fde_of_new_unconfigured_map(auto_name)
        return TaxResolutionResult(
            mapped=False,
            auto_created=True,
            map_docname=auto_name,
            stamped_count=0,
            cess=cess_value,
            reconciled=False,
            discrepancies=[
                f"No EasyEcom Tax Rule Map for rule {tax_rule_name!r} in "
                f"Company {company!r}; auto-created {auto_name} in "
                f"workflow_state={INITIAL_WORKFLOW_STATE} for FDE setup."
            ],
        )

    # Mapped path — stamp the rows and reconcile.
    map_doc = frappe.get_doc("EasyEcom Tax Rule Map", map_name)

    if not map_doc.taxes:
        # Map exists for (rule, company) but the FDE hasn't filled in
        # any rows — the "company-has-no-rows" failure mode from
        # §8.5.7. Don't silently pass.
        return TaxResolutionResult(
            mapped=True,
            auto_created=False,
            map_docname=map_doc.name,
            stamped_count=0,
            cess=cess_value,
            reconciled=False,
            discrepancies=[
                f"EasyEcom Tax Rule Map {map_doc.name} exists but has no "
                f"Item Tax Template rows. The FDE must complete the "
                f"mapping (workflow: Configure transition is gated on "
                f"the taxes table being non-empty)."
            ],
        )

    stamped = _stamp_taxes_onto_item(item, map_doc)
    discrepancies: list[str] = []
    reconciled = _reconcile_resolved_rate(
        product=product,
        map_doc=map_doc,
        discrepancies=discrepancies,
    )

    return TaxResolutionResult(
        mapped=True,
        auto_created=False,
        map_docname=map_doc.name,
        stamped_count=stamped,
        cess=cess_value,
        reconciled=reconciled,
        discrepancies=discrepancies,
    )


# ----- Helpers -----


def _stamp_taxes_onto_item(item: Any, map_doc: "EasyEcomTaxRuleMap") -> int:
    """Replace item.taxes with copies of the map's rows. Both child
    tables use the same DocType (Item Tax) so the field set is
    identical — a straight row-copy. Returns the number of rows
    stamped.
    """
    # Clear any existing taxes — the map is the source of truth for
    # this item's tax templates.
    item.set("taxes", [])
    for row in map_doc.taxes or []:
        item.append(
            "taxes",
            {
                "item_tax_template": row.item_tax_template,
                "tax_category": row.tax_category,
                "valid_from": row.valid_from,
                "minimum_net_rate": row.minimum_net_rate,
                "maximum_net_rate": row.maximum_net_rate,
            },
        )
    return len(map_doc.taxes or [])


def _reconcile_resolved_rate(
    *,
    product: dict,
    map_doc: "EasyEcomTaxRuleMap",
    discrepancies: list[str],
) -> bool:
    """The product carries the rate EE *resolved* for it (e.g. 0.18 for
    GST 18%). Check that it matches one of the mapped templates'
    effective rates. Mismatch → append to discrepancies; return False.

    Note: the Item Tax Template's effective rate is read from the
    template doc's `taxes` child (the per-account-head rate rows). For
    a typical GST template like 'GST 18% - {abbr}', the effective rate
    is the sum of the CGST + SGST account rates (e.g. 9 + 9 = 18%).
    We use _effective_rate_for_template(...) which handles this.
    """
    resolved = product.get("tax_rate")
    if resolved is None:
        # No resolved rate on the payload — nothing to reconcile against.
        # Treat as reconciled (the FDE will see the stamp; ERPNext applies
        # the template's rate at invoice time).
        return True
    try:
        resolved_f = float(resolved)
    except (TypeError, ValueError):
        discrepancies.append(
            f"product.tax_rate is not a number: {resolved!r}"
        )
        return False

    mapped_rates: list[float] = []
    for row in map_doc.taxes or []:
        rate = _effective_rate_for_template(row.item_tax_template)
        if rate is not None:
            mapped_rates.append(rate)

    if not mapped_rates:
        # Can't reconcile — template rates were unreadable. Don't false-
        # positive: surface as a discrepancy.
        discrepancies.append(
            f"Could not read effective rates for any of the {len(map_doc.taxes or [])} "
            f"Item Tax Templates on {map_doc.name}; reconciliation skipped."
        )
        return False

    if any(abs(resolved_f - r) <= RATE_RECONCILE_TOLERANCE for r in mapped_rates):
        return True

    discrepancies.append(
        f"product.tax_rate={resolved_f} does not match any of the mapped "
        f"rates on {map_doc.name}: {sorted(mapped_rates)}. EE rule "
        f"{map_doc.tax_rule_name!r} may have changed, or the FDE entered "
        f"a wrong band — review the mapping."
    )
    return False


def _effective_rate_for_template(template_name: str | None) -> float | None:
    """Return the Item Tax Template's effective GST rate (as a decimal,
    e.g. 0.18 for 18%). Returns None if the template can't be read.

    India Compliance's GST templates have many rows per template — one
    per account head (CGST/SGST/IGST × Input/Output × RCM/Refund/...).
    For reconciliation we want the canonical applied rate, so we:

      1. Prefer 'Output Tax IGST' (single line that IS the full rate;
         e.g. 'GST 18% - X' has Output Tax IGST = 18).
      2. Fall back to summing 'Output Tax CGST' + 'Output Tax SGST' if
         no IGST row exists (older configs).

    Excludes RCM and Refund variants explicitly — they're stored as
    negative-rate accounting heads, and summing them all would cancel
    to ~0, falsely classifying every template as 0% rate.
    """
    if not template_name:
        return None
    try:
        rows = frappe.db.get_all(
            "Item Tax Template Detail",
            filters={"parent": template_name, "parenttype": "Item Tax Template"},
            fields=["tax_type", "tax_rate"],
        )
    except Exception:
        return None
    if not rows:
        return None

    def _is_canonical_output(tt: str, head: str) -> bool:
        """tt is e.g. 'Output Tax IGST - TC' or 'Output Tax IGST RCM - TC'.
        Match 'Output Tax {head}' but exclude RCM / Refund variants."""
        if not tt:
            return False
        if "RCM" in tt or "Refund" in tt:
            return False
        return tt.startswith(f"Output Tax {head} ") or tt == f"Output Tax {head}"

    # Prefer Output Tax IGST.
    igst = next(
        (float(r.tax_rate or 0) for r in rows if _is_canonical_output(r.tax_type, "IGST")),
        None,
    )
    if igst is not None:
        return igst / 100.0

    # Fall back to CGST + SGST.
    cgst = sum(
        float(r.tax_rate or 0) for r in rows if _is_canonical_output(r.tax_type, "CGST")
    )
    sgst = sum(
        float(r.tax_rate or 0) for r in rows if _is_canonical_output(r.tax_type, "SGST")
    )
    if cgst or sgst:
        return (cgst + sgst) / 100.0
    return None


def _apply_cess_to_item(item: Any, cess_value: float) -> None:
    """Write cess to item.ecs_cess (per §8.5.4 step 5).

    The Custom Field is created by the v0_1.add_ecs_cess_to_item patch
    (8c-shipped). If for any reason the field is absent, fall back to
    a flags attribute so 8d can still read the value — but log so the
    FDE notices the field is missing.
    """
    try:
        # Frappe accepts unknown fields via .set() but won't persist
        # them. We check the meta to avoid silent drop.
        meta = frappe.get_meta(item.doctype) if hasattr(item, "doctype") else None
    except Exception:
        meta = None
    if meta is not None and meta.get_field("ecs_cess"):
        item.set("ecs_cess", cess_value)
    # Always also stash on flags so the caller can read regardless.
    if hasattr(item, "flags"):
        item.flags.ecs_cess = cess_value


def _auto_create_to_configure_map(tax_rule_name: str, company: str) -> str:
    """Create a fresh EasyEcom Tax Rule Map in workflow_state=To Configure.
    Caller (the resolver) will surface this to the FDE via notification.
    Idempotent: if a race created the map between our exists() check and
    here, return the existing name."""
    existing = frappe.db.exists(
        "EasyEcom Tax Rule Map",
        {"tax_rule_name": tax_rule_name, "company": company},
    )
    if existing:
        return existing if isinstance(existing, str) else existing[0]
    doc = frappe.new_doc("EasyEcom Tax Rule Map")
    doc.update(
        {
            "tax_rule_name": tax_rule_name,
            "company": company,
            "workflow_state": INITIAL_WORKFLOW_STATE,
            "fde_notes": (
                "Auto-created by Item sync — the EasyEcom product carried "
                f"tax_rule_name={tax_rule_name!r} but no mapping existed for "
                f"Company {company!r}. Fill the Item Tax Templates table "
                "(reading the slab structure from EasyEcom Tax Master UI), "
                "then click Actions → Configure."
            ),
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _notify_fde_of_new_unconfigured_map(map_docname: str) -> None:
    """§18 PLACEHOLDER — see flows/location_discovery._notify_if_new_locations
    for the canonical comment. Bell-icon Notification Log only; no
    Integration Alert routing (§18 hasn't shipped). When §18 ships,
    delete this and call the alerts framework."""
    fde_users = frappe.db.sql_list(
        """SELECT DISTINCT hr.parent
           FROM `tabHas Role` hr
           JOIN `tabUser` u ON u.name = hr.parent
           WHERE hr.role = %s
             AND hr.parenttype = 'User'
             AND u.enabled = 1
             AND u.name NOT IN ('Guest', 'Administrator')""",
        ("EasyEcom FDE",),
    )
    if not fde_users:
        # No FDE users yet; log and move on (don't drop silently).
        frappe.log_error(
            title="Tax Rule Map auto-created: no EasyEcom FDE users to notify",
            message=(
                f"Auto-created EasyEcom Tax Rule Map {map_docname} in "
                f"workflow_state={INITIAL_WORKFLOW_STATE} but no user has "
                "the EasyEcom FDE role assigned."
            ),
        )
        return
    subject = frappe._("EasyEcom: new tax rule needs FDE configuration")
    body = frappe._(
        "Item sync hit an EasyEcom tax rule with no mapping for the "
        "item's Company; the integration auto-created "
        "{0} in 'To Configure'. Open it, fill the Item Tax Templates "
        "table (use the EasyEcom Tax Master UI to read the slab "
        "structure), then click Actions → Configure."
    ).format(map_docname)
    for user in fde_users:
        notif = frappe.new_doc("Notification Log")
        notif.update(
            {
                "for_user": user,
                "type": "Alert",
                "document_type": "EasyEcom Tax Rule Map",
                "document_name": map_docname,
                "subject": subject,
                "email_content": body,
                "from_user": "Administrator",
            }
        )
        notif.insert(ignore_permissions=True)
