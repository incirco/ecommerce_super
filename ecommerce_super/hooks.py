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
# DocType JS overrides (form-script add-ons for stock DocTypes)
# ============================================================
#
# Stock ERPNext / Frappe DocTypes that need EasyEcom buttons get their
# JS injected here. Our own DocTypes ship their JS alongside their
# JSON in the doctype/ folder — those don't need to be listed here.

doctype_js = {
    # §8d Stage 6: "Push to EasyEcom" + "Sync Lifecycle" buttons on
    # the Item form. Auto-dispatches to bundle path when the Item is
    # a Product Bundle wrapper.
    "Item": "public/js/item_push_button.js",
}


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
    # §17 operational layer — workspace Number Cards. Three Item-worklist
    # cards seeded by the §8d audit fix #5; three sibling worklist cards
    # (Locations / Channels / Tax Rules) added by the §17 packet so the
    # FDE Worklist row covers every master uniformly; three LIVE KPI tiles
    # (Sync Records Failed / API Calls 1h / Queue Job Depth) for the
    # operational KPI strip. Pending tiles (Webhook Events / Order-GRN
    # Cursor Lag / Open Integration Discrepancies / Partial Jobs) ship as
    # workspace paragraph blocks — deliberately NOT number cards — so
    # their "no feeder yet" state can't be misread as a live zero (§2.7).
    {
        "dt": "Number Card",
        "filters": [
            ["name", "in", [
                "Items in Drift",
                "Items Created-Flagged",
                "Items Flagged-Not-Created",
                "Locations - To Map",
                "Channels - Unclassified",
                "Tax Rules - To Configure",
                "Open Sync Records (Failed)",
                "API Calls (last hour)",
                "Queue Job Depth",
            ]],
        ],
    },
    # §17 — Dashboard Charts.
    {
        "dt": "Dashboard Chart",
        "filters": [
            ["name", "in", [
                "EasyEcom API Call Volume (7d)",
                "Sync Record Status (Item-only currently)",
            ]],
        ],
    },
    # §17.2.1 Top Strip — Custom HTML Block (env badge / connection
    # status / pause-all toggle).
    {
        "dt": "Custom HTML Block",
        "filters": [
            ["name", "=", "EasyEcom Top Strip"],
        ],
    },
    # Field Mapping library (§5.11) — child rulesets first, then parents.
    # Load order is honoured by the JSON file's element order; compose
    # references require the child ruleset to already be in the DB at
    # save time (the compiler's compose-target-exists check fires).
    "EasyEcom Field Mapping",
    # Workflow fixtures (§8.4.1 / §8.6.3 — 8a Location established the
    # pattern; 8b Channel reuses it for the Marketplace classification
    # workflow). Order matters: Workflow State and Workflow Action
    # Master must exist before Workflow tries to reference them.
    # Filters scope each fixture to the names this app owns so we don't
    # accidentally export every state/action Frappe core ships.
    {
        "dt": "Workflow State",
        "filters": [
            [
                "name",
                "in",
                [
                    # 8a Location
                    "To Map",
                    "Mapped but not Live",
                    "Live",
                    "Skipped",
                    # 8b Marketplace
                    "Unclassified",
                    "Classified",
                    "Active",
                    "Ignored",
                    # 8c Tax Rule Map
                    "To Configure",
                    "Configured",
                ],
            ]
        ],
    },
    {
        "dt": "Workflow Action Master",
        "filters": [
            [
                "name",
                "in",
                [
                    # 8a Location
                    "Map",
                    "Go Live",
                    "Mark Not Relevant",
                    "Pause",
                    "Reconsider",
                    # 8b Marketplace
                    "Classify",
                    "Activate",
                    "Deactivate",
                    "Reclassify",
                    # 8c Tax Rule Map
                    "Configure",
                    "Reconfigure",
                ],
            ]
        ],
    },
    {
        "dt": "Workflow",
        "filters": [
            [
                "name",
                "in",
                [
                    "EasyEcom Location Workflow",
                    "Marketplace Classification Workflow",
                    "Tax Rule Map Workflow",
                ],
            ]
        ],
    },
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
    # §8d Stage 6: auto-push hook. Fires on every Item / Product Bundle
    # save when EasyEcom Account.auto_push_on_save=1 (default 0 — safe
    # by default). Handlers are gated by:
    #   - account flag enabled
    #   - frappe.flags.in_easyecom_pull is FALSE (avoid pull→push
    #     ping-pong when the Stage-2 pull saves an Item)
    #   - Item is not a variant template (has_variants=0)
    # Failures are queued / logged, never block the save itself.
    "Item": {
        "after_insert": "ecommerce_super.easyecom.flows.item_push.enqueue_on_item_change",
        "on_update": "ecommerce_super.easyecom.flows.item_push.enqueue_on_item_change",
    },
    "Product Bundle": {
        "after_insert": "ecommerce_super.easyecom.flows.item_push.enqueue_on_bundle_change",
        "on_update": "ecommerce_super.easyecom.flows.item_push.enqueue_on_bundle_change",
    },
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
        # Daily EasyEcom location discovery (§8.4.3). Runs at 03:30 IST so
        # the JWT renewal at 02:00 is already settled. Writes a
        # Notification Log entry to EasyEcom FDE users when new
        # locations appear; quiet on no-change ticks.
        "30 3 * * *": [
            "ecommerce_super.easyecom.flows.location_discovery.scheduled_discover_locations",
        ],
        # Daily EasyEcom channel sweep (§8.6.3). Runs at 04:00 IST, AFTER
        # location discovery (03:30) — a freshly-discovered location
        # gets included in the same day's channel sweep. Wraps each
        # per-location call in the 8a savepoint helper so one
        # location's JWT failure doesn't abort the whole sweep.
        "0 4 * * *": [
            "ecommerce_super.easyecom.flows.channel_discovery.scheduled_discover_channels",
        ],
        # Daily §8d product master delta pull (audit fix #3). Runs at
        # 05:00 IST, AFTER channels (04:00) — the catalogue depends on
        # Location + Channel being mapped before tax stamping can land.
        # Uses item_pull_last_updated_at as the updated_after delta
        # cursor. Mode-aware: post-flip (erpnext_mastered) this same
        # scheduled call runs drift detection instead of accept-and-
        # create, per process_one_product's phase gate.
        "0 5 * * *": [
            "ecommerce_super.easyecom.flows.item_pull.scheduled_discover_products",
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
    # 8c — Tax Rule Map (per (tax_rule_name, company); FDE config)
    "EasyEcom Tax Rule Map",
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
