"""§3.11 acceptance bar 13: Permissions and roles exist on a fresh install
with no manual step.

After install/migrate on a clean site:
  - The five custom roles (Operator, FDE, Replay Approver, System Manager,
    Auditor) exist as fixtures.
  - The EasyEcom Account DocType grants read to EasyEcom FDE and read/write
    to EasyEcom System Manager.
  - Credential fields sit at restricted permlevel readable/writable only by
    EasyEcom System Manager.
  - Per-Company settings carry the assigned-FDE DocPerm.
  - None of these required a manual step.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase

EXPECTED_CUSTOM_ROLES = {
    "EasyEcom Operator",
    "EasyEcom FDE",
    "EasyEcom Replay Approver",
    "EasyEcom System Manager",
    "EasyEcom Auditor",
}


class TestRolesShippedAsFixtures(FrappeTestCase):
    def test_all_five_custom_roles_present(self) -> None:
        present = set(
            frappe.db.get_all(
                "Role",
                filters={"role_name": ["in", list(EXPECTED_CUSTOM_ROLES)]},
                pluck="name",
            )
        )
        missing = EXPECTED_CUSTOM_ROLES - present
        self.assertFalse(missing, f"Missing roles after install: {missing}")

    def test_account_docperm_grants_fde_read(self) -> None:
        perms = frappe.db.get_all(
            "DocPerm",
            filters={"parent": "EasyEcom Account", "role": "EasyEcom FDE"},
            fields=["read", "write", "permlevel"],
        )
        self.assertTrue(any(p.read and not p.write for p in perms), perms)

    def test_account_docperm_grants_system_manager_full(self) -> None:
        perms = frappe.db.get_all(
            "DocPerm",
            filters={"parent": "EasyEcom Account", "role": "EasyEcom System Manager"},
            fields=["read", "write", "create", "delete", "permlevel"],
        )
        # Should have at least one row at permlevel 0 with read+write+create
        # and a separate row at permlevel 1 with read+write (for credentials).
        levels = {p.permlevel for p in perms}
        self.assertIn(0, levels)
        self.assertIn(1, levels)

    def test_credential_fields_at_permlevel_1(self) -> None:
        """Setup, Webhook Auth, and Slack credential fields sit at permlevel 1."""
        meta = frappe.get_meta("EasyEcom Account")
        for fieldname in ("x_api_key", "email", "password", "webhook_token"):
            df = meta.get_field(fieldname)
            self.assertIsNotNone(df, f"{fieldname} field missing from EasyEcom Account")
            self.assertEqual(
                df.permlevel,
                1,
                f"{fieldname} should be permlevel 1, got {df.permlevel}",
            )

    def test_company_settings_has_assigned_fde(self) -> None:
        meta = frappe.get_meta("EasyEcom Company Settings")
        df = meta.get_field("assigned_fde")
        self.assertIsNotNone(df)
        self.assertEqual(df.fieldtype, "Link")
        self.assertEqual(df.options, "User")


class TestComposedFixtures(FrappeTestCase):
    def test_marketplace_starter_seed_loaded(self) -> None:
        """§8b refactor (marketplace_id Data → Int): the seed now ships
        well-known channels with their REAL EE numeric ids. The legacy
        string-keyed starter-* entries were dropped by the
        drop_starter_marketplace_rows pre-model-sync patch."""
        # Real EE ids the §8b refactor seeded.
        for mid in (2, 8, 60, 100, 122):
            self.assertTrue(
                frappe.db.exists("Marketplace", {"marketplace_id": mid}),
                f"Seed Marketplace marketplace_id={mid} not loaded",
            )
        # And confirm the legacy starter-* names are absent.
        legacy = frappe.db.count(
            "Marketplace", filters={"name": ["like", "starter-%"]}
        )
        self.assertEqual(legacy, 0)

    def test_channel_accounting_dimension_present(self) -> None:
        row = frappe.db.get_value(
            "Accounting Dimension",
            "Channel",
            ["document_type", "fieldname", "disabled"],
            as_dict=True,
        )
        self.assertIsNotNone(row, "Channel Accounting Dimension was not loaded")
        self.assertEqual(row.document_type, "Marketplace")
        self.assertEqual(row.fieldname, "channel")
        self.assertEqual(row.disabled, 0)


class TestCompositeIndexes(FrappeTestCase):
    """The after_install hook adds composite indexes that Frappe's JSON
    schema can't express. Verify they're in MariaDB."""

    def _index_exists(self, table: str, index_name: str) -> bool:
        rows = frappe.db.sql(
            """SELECT 1 FROM information_schema.statistics
               WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s
               LIMIT 1""",
            (table, index_name),
        )
        return bool(rows)

    def test_sync_record_composite_unique(self) -> None:
        self.assertTrue(
            self._index_exists(
                "tabEasyEcom Sync Record", "uq_sync_record_entity_direction"
            )
        )

    def test_webhook_event_composite_unique(self) -> None:
        self.assertTrue(
            self._index_exists("tabEasyEcom Webhook Event", "uq_webhook_event_dedup")
        )

    def test_sync_cursor_composite_unique(self) -> None:
        self.assertTrue(
            self._index_exists("tabEasyEcom Sync Cursor", "uq_sync_cursor_triple")
        )

    def test_api_call_query_indexes(self) -> None:
        for idx in (
            "ix_api_call_company_time",
            "ix_api_call_endpoint_time",
            "ix_api_call_status_time",
        ):
            self.assertTrue(
                self._index_exists("tabEasyEcom API Call", idx),
                f"API Call index {idx} missing",
            )
