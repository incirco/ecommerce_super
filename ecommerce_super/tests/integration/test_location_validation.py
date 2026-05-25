"""§3.11 acceptance bar 7-ish: Location validation rules.

- Exactly one Location per account has is_primary = 1.
- frappe_company is mandatory when is_operational = 1.
- frappe_company is non-unique by design (many-to-one).
- A Location with neither flag is inert — created without error.

§8.4.1 changes the lifecycle: is_operational is workflow-derived (set
by the Go Live transition). Tests that need a Location in Live state
transition through the workflow rather than skip-setting workflow_state
on insert (which Frappe's active workflow refuses).
"""

from __future__ import annotations

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state


def _ensure_test_company(name: str = "_Test Company") -> str:
    """Reuse an existing Company or create a minimal test one.

    ERPNext's Company.on_update auto-creates standard warehouses, which
    require certain Warehouse Type records to exist. On a fresh site
    those may not be present yet, so we pre-create them.
    """
    if frappe.db.exists("Company", name):
        return name
    existing = frappe.db.get_value("Company", filters={}, fieldname="name")
    if existing:
        return existing
    # Pre-create Warehouse Type records ERPNext auto-creation expects.
    for wt in ("Transit", "Stores", "Work In Progress", "Finished Goods"):
        if not frappe.db.exists("Warehouse Type", wt):
            try:
                wt_doc = frappe.new_doc("Warehouse Type")
                wt_doc.name = wt
                wt_doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
            except Exception:
                pass
    doc = frappe.new_doc("Company")
    doc.update(
        {
            "company_name": name,
            "abbr": "TC",
            "default_currency": "INR",
            "country": "India",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_admin_has_fde_role_for_validation() -> None:
    """Same as test_location_workflow's helper — Administrator needs
    EasyEcom FDE to operate the Workflow."""
    admin = frappe.get_doc("User", "Administrator")
    has = any(r.role == "EasyEcom FDE" for r in admin.roles)
    if not has:
        admin.append("roles", {"role": "EasyEcom FDE"})
        admin.save(ignore_permissions=True)
        frappe.db.commit()
    frappe.clear_cache(user="Administrator")
    frappe.set_user("Administrator")


class TestLocationValidation(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        _ensure_admin_has_fde_role_for_validation()

    def setUp(self) -> None:
        cleanup_easyecom_state()

    def tearDown(self) -> None:
        cleanup_easyecom_state()

    def _make_live(self, key: str, *, frappe_company: str | None = None) -> str:
        """Insert in To Map, then transition through Map → Go Live to land
        in Live state. Returns the docname."""
        doc = self._new_location(
            location_key=key,
            workflow_state="To Map",
            frappe_company=None,
        )
        doc.insert(ignore_permissions=True)
        if frappe_company:
            frappe.db.set_value(
                "EasyEcom Location", doc.name, "frappe_company", frappe_company
            )
        reloaded = frappe.get_doc("EasyEcom Location", doc.name)
        apply_workflow(reloaded, "Map")
        reloaded.reload()
        apply_workflow(reloaded, "Go Live")
        return doc.name

    def _new_location(self, **fields) -> "frappe.model.document.Document":
        """Build a Location doc. is_operational is now workflow-derived
        (§8.4.1) — set workflow_state explicitly to drive it:
          - workflow_state='Live'                → is_operational=1
          - workflow_state='To Map'/'Mapped'/'Skipped' → is_operational=0
        """
        defaults = {
            "location_key": "L1",
            "location_name": "Test Location",
            "enabled": 1,
            "is_primary": 0,
            "workflow_state": "To Map",
            "is_wms_location": 0,
            "serialization_enabled": 0,
        }
        defaults.update(fields)
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(defaults)
        return doc

    def test_exactly_one_primary_enforced(self) -> None:
        a = self._new_location(location_key="L-PRIMARY-A", is_primary=1)
        a.insert(ignore_permissions=True)
        # A second primary must fail.
        b = self._new_location(location_key="L-PRIMARY-B", is_primary=1)
        with self.assertRaises(frappe.ValidationError):
            b.insert(ignore_permissions=True)

    def test_frappe_company_required_when_operational(self) -> None:
        # Try to reach Live without a Company → the workflow's Map
        # transition condition (doc.frappe_company) blocks it. The
        # controller's validate-time check is defence in depth; in
        # practice the workflow stops you first.
        doc = self._new_location(
            location_key="L-OP-NOCO", workflow_state="To Map", frappe_company=None
        )
        doc.insert(ignore_permissions=True)
        # Map without Company → workflow condition fails.
        with self.assertRaises(frappe.ValidationError):
            apply_workflow(frappe.get_doc("EasyEcom Location", doc.name), "Map")

        # Reach Live properly: To Map → set Company → Map → Go Live.
        name = self._make_live("L-OP-OK", frappe_company=self.company)
        live = frappe.get_doc("EasyEcom Location", name)
        self.assertEqual(live.workflow_state, "Live")
        self.assertEqual(live.is_operational, 1)

    def test_frappe_company_set_in_mapped_but_not_live_is_allowed(self) -> None:
        """§8.4.1 intermediate state: Map transition assigns
        frappe_company BEFORE Go Live flips is_operational. Therefore
        frappe_company set + is_operational=0 is a legal mid-lifecycle
        state, not a validation error."""
        doc = self._new_location(
            location_key="L-MID-LIFECYCLE",
            workflow_state="To Map",
            frappe_company=None,
        )
        doc.insert(ignore_permissions=True)
        # Set Company then Map → lands in Mapped but not Live.
        frappe.db.set_value(
            "EasyEcom Location", doc.name, "frappe_company", self.company
        )
        reloaded = frappe.get_doc("EasyEcom Location", doc.name)
        apply_workflow(reloaded, "Map")
        reloaded.reload()
        self.assertEqual(reloaded.workflow_state, "Mapped but not Live")
        # Must not raise — Mapped but not Live with Company set is valid.
        self.assertEqual(reloaded.is_operational, 0)
        self.assertEqual(reloaded.frappe_company, self.company)

    def test_frappe_company_is_non_unique(self) -> None:
        """Many-to-one resolution: two Locations may resolve to the same Company."""
        self._make_live("L-CO-1", frappe_company=self.company)
        # Must not raise — two locations sharing a Company is the design.
        self._make_live("L-CO-2", frappe_company=self.company)
        # Both Live, both pointing at the same Company.
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Location", "ECS-LOC-L-CO-1", "frappe_company"
            ),
            self.company,
        )
        self.assertEqual(
            frappe.db.get_value(
                "EasyEcom Location", "ECS-LOC-L-CO-2", "frappe_company"
            ),
            self.company,
        )

    def test_inert_location_is_valid(self) -> None:
        """Neither primary nor operational → recorded but not synced (§3.1.3)."""
        inert = self._new_location(
            location_key="L-INERT",
            is_primary=0,
            workflow_state="To Map",
        )
        inert.insert(ignore_permissions=True)
        self.assertFalse(inert.is_primary)
        # Derive sets is_operational=0 for non-Live states.
        self.assertFalse(inert.is_operational)
        self.assertIsNone(inert.frappe_company)

    def test_resolve_company_returns_none_for_inert(self) -> None:
        from ecommerce_super.easyecom.doctype.easyecom_location.easyecom_location import (
            resolve_company,
        )

        inert = self._new_location(
            location_key="L-INERT-2", is_primary=0, workflow_state="To Map"
        )
        inert.insert(ignore_permissions=True)
        self.assertIsNone(resolve_company("L-INERT-2"))
        self.assertIsNone(resolve_company("L-DOES-NOT-EXIST"))

    def test_jwt_encrypt_set_and_get(self) -> None:
        """set_jwt encrypts; get_jwt_plaintext returns the original."""
        loc = self._new_location(location_key="L-JWT", is_primary=1)
        loc.insert(ignore_permissions=True)
        # Reload to get a fresh handle (set_jwt uses db_set, which bypasses
        # the in-memory doc).
        loc = frappe.get_doc("EasyEcom Location", loc.name)
        original = "eyJraWQiOiJ0ZXN0IiwiYWxnIjoiUlMyNTYifQ.payload.signature"
        loc.set_jwt(original, validity_days=90)
        # Reload to read back from DB.
        loc = frappe.get_doc("EasyEcom Location", loc.name)
        # Cached value is ciphertext, not plaintext.
        self.assertNotEqual(loc.jwt_token, original)
        # But get_jwt_plaintext decrypts cleanly.
        self.assertEqual(loc.get_jwt_plaintext(), original)
        self.assertIsNotNone(loc.jwt_acquired_at)
        self.assertIsNotNone(loc.jwt_expires_at)
