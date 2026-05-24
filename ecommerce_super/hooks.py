"""ecommerce_super Frappe hooks registry.

See SPEC §31.8 for the canonical list. doc_events for flows (§31.8.1)
will be filled in as each flow packet (§8-§13) is built; the foundation
packet wires only scheduler events, permission hooks, fixtures, and the
after_install hook.
"""

from __future__ import annotations

app_name = "ecommerce_super"
app_title = "EasyEcom"
app_publisher = "Incirco"
app_description = "ERPNext-native EasyEcom integration"
app_email = "nikhil@incirco.com"
app_license = "mit"
app_color = "blue"
app_home = "/app/easyecom"
app_logo_url = "/assets/ecommerce_super/images/easyecom.svg"


# ============================================================
# v16 apps screen (the launcher at /desk)
# ============================================================
#
# The icon shown on the apps screen. Permission check returns True for
# any user with at least one of the five EasyEcom custom roles; this is
# the same gate as the Workspace's `roles` list so the launcher icon and
# the workspace visibility stay aligned.

add_to_apps_screen = [
    {
        "name": "ecommerce_super",
        "logo": "/assets/ecommerce_super/images/easyecom.svg",
        "title": "EasyEcom",
        "route": "/app/easyecom",
        "has_permission": "ecommerce_super.easyecom.permissions.has_app_screen_permission",
    }
]


# ============================================================
# Installation
# ============================================================

after_install = "ecommerce_super.install.after_install"


# ============================================================
# Fixtures
# ============================================================
#
# Subset of §31.8.4 that ships in the foundation packet. Field Mapping,
# Error Translation, SLA Budget, etc. ship with their owning flows.

fixtures = [
    {
        "dt": "Role",
        "filters": [
            [
                "role_name",
                "in",
                [
                    "EasyEcom Operator",
                    "EasyEcom FDE",
                    "EasyEcom Replay Approver",
                    "EasyEcom System Manager",
                    "EasyEcom Auditor",
                ],
            ]
        ],
    },
    {
        "dt": "Custom Field",
        "filters": [["fieldname", "like", "ecs_%"]],
    },
    "Marketplace",
    "Accounting Dimension",
    # Field Mapping library (§5.11) — child rulesets first, then parents.
    # Load order is honoured by the JSON file's element order; compose
    # references require the child ruleset to already be in the DB at
    # save time (the compiler's compose-target-exists check fires).
    "EasyEcom Field Mapping",
    # Desktop Icon ships at ecommerce_super/desktop_icon/easyecom.json —
    # Frappe auto-syncs from the per-app desktop_icon/ directory. Fixture
    # entries get orphan-deleted on migrate; same pattern as Workspace.
]


# ============================================================
# Document events (§31.8.1)
# ============================================================
#
# Flow handlers (Sales Order on_submit, Purchase Order on_submit, etc.)
# are wired here when their packets ship. The EasyEcom Configuration
# Audit DocType ships in §26, so the audit-on-save hooks for Account
# and Field Mapping are deferred to that packet. Listed empty here to
# make the slot visible.

doc_events: dict[str, dict[str, str]] = {
    # "Sales Order": {
    #     "validate": "ecommerce_super.easyecom.flows.b2b_sales.validate_pre_push",
    #     "on_submit": "ecommerce_super.easyecom.flows.b2b_sales.on_submit_push",
    # },
}


# ============================================================
# Scheduler events (§31.8.2)
# ============================================================

scheduler_events = {
    "cron": {
        # Day-85 JWT renewal — runs daily at 02:00 IST (jittered internally
        # so accounts with many locations don't fan out a token-call herd).
        "0 2 * * *": [
            "ecommerce_super.easyecom.client.auth.renew_aging_jwts",
        ],
        # Connection health rollup — every 5 minutes.
        "*/5 * * * *": [
            "ecommerce_super.easyecom.operational.connection_health.update_account_connection_status",
        ],
        # Reclaim Queue Job rows in state=Running with no live RQ job (§6.3.9).
        "0 */1 * * *": [
            "ecommerce_super.easyecom.queue.workers.reclaim_orphaned_jobs",
        ],
    },
}


# ============================================================
# Permission hooks (§31.7 / §31.8.3)
# ============================================================
#
# Company-scoped DocTypes get the company_scope filter applied to every
# list query. Append-only DocTypes (API Call, Webhook Event, and the
# Configuration Audit when it lands) get a has_permission hook that
# refuses write/delete for every role.

_COMPANY_SCOPED_DOCTYPES = [
    "EasyEcom Company Settings",
    "EasyEcom Sync Record",
    "EasyEcom API Call",
    "EasyEcom Webhook Event",
    "EasyEcom Queue Job",
    "EasyEcom Sync Cursor",
    # The following ship with later packets; listed in advance so the
    # contract is visible. Frappe ignores entries for DocTypes that don't
    # exist yet at hook-resolution time.
    "EasyEcom Replay Plan",
    "EasyEcom SLA Budget",
    "EasyEcom SLA Breach",
    "EasyEcom Configuration Audit",
    "EasyEcom Morning Brief Snapshot",
    "Integration Discrepancy",
    "Marketplace Account",
    "Marketplace Order Map",
    "Source-of-Truth Map",
]

permission_query_conditions = {
    dt: "ecommerce_super.easyecom.permissions.company_scope"
    for dt in _COMPANY_SCOPED_DOCTYPES
}

has_permission = {
    "EasyEcom API Call": "ecommerce_super.easyecom.permissions.append_only",
    "EasyEcom Webhook Event": "ecommerce_super.easyecom.permissions.append_only",
    "EasyEcom Configuration Audit": "ecommerce_super.easyecom.permissions.audit_no_modify",
}
