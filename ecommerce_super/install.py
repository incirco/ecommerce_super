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
    # Source-of-Truth Map — UNIQUE (company, warehouse) per §31.2.23
    (
        "tabSource-of-Truth Map",
        "uq_sot_map_company_warehouse",
        "company, warehouse",
        True,
    ),
    # EasyEcom Tax Rule Map — UNIQUE (tax_rule_name, company) per §8.5.3.
    # The natural key for the 8c FDE-config mapping.
    (
        "tabEasyEcom Tax Rule Map",
        "uq_tax_rule_map_rule_company",
        "tax_rule_name, company",
        True,
    ),
]


def after_install() -> None:
    """First-run setup. Idempotent.

    gh#48: at the end of after_install, run the Custom Field audit so
    a fresh install that race-condition'd its way to missing fields
    self-heals before the FDE first touches the desk. Audit results
    land in the bench console + Error Log when anything needed rescue.
    """
    _add_composite_indexes()
    _run_custom_field_audit()
    frappe.db.commit()


def _run_custom_field_audit() -> None:
    """gh#48 — verify + rescue every Custom Field the integration ships."""
    from ecommerce_super.easyecom.install.custom_field_verify import run_audit

    summary = run_audit()
    needs_rescue = summary["total"] - summary["ok"] - summary["doctype_missing"]
    if needs_rescue == 0:
        return
    print(
        f"[ecommerce_super] after_install: rescued "
        f"{summary['rescued']}/{summary['total']} Custom Fields "
        f"(gh#48 audit)"
    )
    if summary["rescued"] > 0:
        frappe.log_error(
            title=(
                f"gh#48 (after_install): rescued {summary['rescued']} "
                "Custom Field(s)"
            ),
            message=str(summary),
        )


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
