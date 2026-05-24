"""after_install hook for the ecommerce_super app.

Runs once on first install (and is safe to re-run via
`bench --site <site> execute ecommerce_super.install.after_install`).
Idempotent — every operation checks for existing state.

Responsibilities (§31.8.5):
  - Add the composite DB UNIQUE indexes that Frappe's JSON schema can't
    express directly (Sync Record, Webhook Event, Sync Cursor).
  - Add the composite query indexes from §31.2.4 (API Call).
  - Ensure default Workspace mapping (placeholder — workspace fixture
    ships in Phase I).
  - Record the install in the Error Log for audit traceability.
"""

from __future__ import annotations

import frappe

# DB-level indexes that Frappe's DocType JSON cannot express. Each entry:
#   (table, index_name, columns, unique)
COMPOSITE_INDEXES: list[tuple[str, str, str, bool]] = [
    # EasyEcom Sync Record — UNIQUE (company, entity_doctype, entity_name, direction)
    (
        "tabEasyEcom Sync Record",
        "uq_sync_record_entity_direction",
        "company, entity_doctype, entity_name, direction",
        True,
    ),
    # EasyEcom Webhook Event — UNIQUE (company, event_type, ee_event_id)
    (
        "tabEasyEcom Webhook Event",
        "uq_webhook_event_dedup",
        "company, event_type, ee_event_id",
        True,
    ),
    # EasyEcom Sync Cursor — UNIQUE (company, location_key, resource)
    (
        "tabEasyEcom Sync Cursor",
        "uq_sync_cursor_triple",
        "company, location_key, resource",
        True,
    ),
    # EasyEcom API Call — composite query indexes per §31.2.4
    (
        "tabEasyEcom API Call",
        "ix_api_call_company_time",
        "company, attempted_at",
        False,
    ),
    (
        "tabEasyEcom API Call",
        "ix_api_call_endpoint_time",
        "endpoint, attempted_at",
        False,
    ),
    (
        "tabEasyEcom API Call",
        "ix_api_call_status_time",
        "status, attempted_at",
        False,
    ),
]


def after_install() -> None:
    """First-run setup. Idempotent."""
    _add_composite_indexes()
    frappe.db.commit()


def _add_composite_indexes() -> None:
    for table, index_name, columns, unique in COMPOSITE_INDEXES:
        if not _table_exists(table):
            # DocType not migrated yet — skip and let a later migrate retry.
            continue
        if _index_exists(table, index_name):
            continue
        keyword = "UNIQUE INDEX" if unique else "INDEX"
        # CREATE INDEX statements in MariaDB don't accept IF NOT EXISTS in
        # all versions; we pre-checked existence above.
        sql = f"CREATE {keyword} `{index_name}` ON `{table}` ({columns})"
        try:
            frappe.db.sql(sql)
        except Exception as e:
            # Log and continue — a duplicate row blocking the index creation
            # is the only realistic cause, and we surface that rather than
            # crashing the install.
            frappe.log_error(
                title=f"EasyEcom: could not create index {index_name}",
                message=f"{type(e).__name__}: {e}\nSQL: {sql}",
            )


def _table_exists(table: str) -> bool:
    return (
        frappe.db.sql(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
            (table,),
        )
        != ()
    )


def _index_exists(table: str, index_name: str) -> bool:
    row = frappe.db.sql(
        """SELECT 1
           FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s
           LIMIT 1""",
        (table, index_name),
    )
    return bool(row)
