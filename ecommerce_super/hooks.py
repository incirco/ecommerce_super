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

# §8e Stage 6 — full-width layout override for the EasyEcom workspace.
# The default Frappe desk centers workspace content with side gutters
# which wastes horizontal space on a dashboard with many cards. Scoped
# to data-page-route="easyecom" so other workspaces are unaffected.
app_include_css = "/assets/ecommerce_super/css/easyecom_workspace.css"


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
# before_request — pre-auth header normalisation (gh#1)
# ============================================================
#
# Frappe's validate_auth() (frappe/auth.py) raises AuthenticationError
# whenever the request carries a 2-part `Authorization` header AND the
# session user is still Guest at the end of its checks — even for
# @whitelist(allow_guest=True) endpoints. This collides with SPEC §3.8
# which mandates accepting webhook bearer tokens in either
# `Access-token` OR `Authorization: Bearer` form on the EasyEcom
# webhook receiver.
#
# `before_request` runs BEFORE validate_auth (frappe/app.py — init_request
# fires before_request hooks, then application() calls validate_auth).
# We use that window to MOVE a Bearer token from Authorization to
# Access-token for the webhook URL only, so Frappe's auth middleware
# sees no Authorization header and skips the final raise. Our
# webhook.receive() then reads the token from Access-token and runs the
# real constant-time webhook_token comparison — the auth contract is
# unchanged, only the header carrier shifts.

before_request = [
    "ecommerce_super.easyecom.api.webhook.normalise_webhook_auth_header",
    # gh#123: identical pattern to the webhook hook above but for the
    # §11.5.1 Custom GSP endpoints (/gettoken, /einvoice/update,
    # /ewaybill/update). Frappe's validate_auth() consumes the
    # Authorization header (Basic → api_key lookup fails → AuthenticationError;
    # or generic 2-part-header-with-Guest-session tail check) BEFORE our
    # whitelisted method runs. This hook shifts the header out of
    # HTTP_AUTHORIZATION into a stash environ key so validate_auth skips
    # its check; the GSP handlers then read from the stash.
    "ecommerce_super.easyecom.api.gsp.normalise_gsp_auth_header",
]


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
    # §8e Stage 4: "Push to EasyEcom" button on the Customer form.
    # Only visible for customer_type=Company (§8e wholesale scope).
    "Customer": "public/js/customer_push_button.js",
    # §8f Stage 4 (gh#36): "Push to EasyEcom" button on the Supplier
    # form. Mirrors Customer; only visible for supplier_type=Company
    # and enabled (§8f wholesale scope; /wms/CreateVendor +
    # /wms/UpdateVendor only handle Company vendors).
    "Supplier": "public/js/supplier_push_button.js",
    # §10 UX: warehouse autocomplete carries EE-mapping label; once
    # both header warehouses picked, predicts the §10 branch (STN /
    # PO / B2B / Inert) so the FDE sees the consequence before submit.
    "Delivery Note": "public/js/delivery_note_ee_visibility.js",
    # §11 Phase 1: Sales Order form — "Cancel on EasyEcom" button
    # (visible when Map status ∈ {Pushed, Queued}) + "Trace B2B Push"
    # button (visible for any submitted SO with an EE-mapped
    # set_warehouse).
    "Sales Order": "public/js/sales_order_b2b_visibility.js",
    # §10 UX: lock Address fields when mirrored from an EasyEcom
    # Location (ecs_ee_location set). Renders a banner directing the
    # FDE to edit on the Location side — single source of truth.
    "Address": "public/js/address_ee_lock.js",
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
                # §8e Stage 6: Customer Map worklist cards — mirror the Item
                # three for the §17 FDE Worklist row.
                "Customers in Drift",
                "Customers Created-Flagged",
                "Customers Flagged-Not-Created",
                "Locations - To Map",
                "Channels - Unclassified",
                "Tax Rules - To Configure",
                "Open Sync Records (Failed)",
                "API Calls (last hour)",
                "Queue Job Depth",
                # §8f — Supplier Map worklist cards.
                "Suppliers in Drift",
                "Suppliers Created-Flagged",
                "Suppliers Flagged-Not-Created",
                # §9 Stage 4 — Buying worklist cards.
                "POs Flagged-Not-Created",
                "POs in Drift",
                "GRNs Failed",
                "GRNs Discrepancy",
                "GRNs Held-Pre-QC",
                "GRNs STN-Routed (pending §10 pickup)",
                # §10 Stage 4 — Stock Transfer worklist cards
                # (integration-health only per packet rule).
                "Transfers in Drift",
                "EE-originated Transfers (open)",
                "Late GRN after submitted DN (open)",
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
    # §8e Stage 4: Customer auto-push hook. Gated by:
    #   - auto_push_customers_on_save=1 on the enabled EasyEcom Account
    #     (default 0 — safe by default; opt-in once onboarding stabilises)
    #   - customer_type=Company (§8e is wholesale only)
    #   - not currently in the customer_pull flow (ping-pong guard via
    #     frappe.flags.easyecom_customer_pull_in_flight)
    "Customer": {
        "after_insert": "ecommerce_super.easyecom.flows.customer_push.enqueue_on_customer_change",
        "on_update": "ecommerce_super.easyecom.flows.customer_push.enqueue_on_customer_change",
    },
    # §8f Stage 4: Supplier auto-push hook. Gated by:
    #   - auto_push_suppliers_on_save=1 on the enabled EasyEcom Account
    #     (default 0 — safe by default; opt-in once onboarding stabilises)
    #   - supplier_type=Company (§8f is wholesale only)
    #   - not currently in the supplier_pull flow (ping-pong guard via
    #     frappe.flags.easyecom_supplier_pull_in_flight)
    "Supplier": {
        "after_insert": "ecommerce_super.easyecom.flows.supplier_push.enqueue_on_supplier_change",
        "on_update": "ecommerce_super.easyecom.flows.supplier_push.enqueue_on_supplier_change",
    },
    # §11 Phase 1 Stage 2: B2B sales push hooks.
    #   - validate: §11.2 preconditions (mixed warehouses, GSTIN strict
    #     for Old B2B, customer/item synced, HSN, billing address, etc.).
    #     Throws block the SO save before any persisted state exists.
    #   - on_submit: Gate 0 + enqueue the async createOrder push.
    #     Non-EE-mapped set_warehouse → silently inert (pure ERPNext).
    "Sales Order": {
        "validate": "ecommerce_super.easyecom.flows.b2b_sales.push.validate_pre_push",
        "on_submit": "ecommerce_super.easyecom.flows.b2b_sales.push.on_submit_push",
        # §11 Phase 1 (this packet): synchronous block-on-refusal
        # cancel propagation. before_cancel runs BEFORE Frappe flips
        # docstatus to 2 so a throw here leaves the SO submitted —
        # exactly the semantics needed for refusal / infra-failure
        # paths to avoid an EE-cancelled / SO-submitted divergence.
        # Scope-guard inside the wrapper ensures non-EE SOs are
        # untouched (vanilla cancel still works).
        "before_cancel": "ecommerce_super.easyecom.flows.b2b_sales.cancel.on_before_cancel_dispatch",
    },
    # §9 Stage 2: PO push hooks.
    #   - validate: mixed-warehouse refusal + warehouse-flip refusal on
    #     amend (BLOCKING — these are user-correctable invariants).
    #   - on_submit: gated by auto_push_pos_on_save; Gate-0 short-circuit
    #     (non-EE-warehouse → silently inert). Enqueues content + status
    #     push to po_status=3 (Approved).
    #   - on_cancel: fires updatePoStatus=7 (Cancelled) when the PO has
    #     ee_po_id. NOT gated on auto_push — cancels must propagate even
    #     when auto-push is paused.
    #   - after_rename: fallback-flag (PO Map → Drift) per packet; auto-
    #     re-push would orphan the EE-side row since referenceCode is
    #     the join key.
    "Purchase Order": {
        "validate": "ecommerce_super.easyecom.flows.po_push.validate_pre_push",
        "on_submit": "ecommerce_super.easyecom.flows.po_push.enqueue_on_po_submit",
        "on_cancel": "ecommerce_super.easyecom.flows.po_push.enqueue_on_po_cancel",
        "on_update_after_submit": "ecommerce_super.easyecom.flows.grn_pull.enqueue_on_po_close",
        "after_rename": "ecommerce_super.easyecom.flows.po_push.after_rename_po",
    },
    # §10 Stage 2 — Delivery Note outbound flow.
    #   - validate: refuses DNs with multiple distinct (source, target)
    #     warehouse pairs on internal-customer transfers (BLOCKING —
    #     same "split, don't auto-multiplex" rule as §9).
    #   - on_submit: Gate-0 (internal-customer + at-least-one EE WH);
    #     enqueues §10 outbound (Transfer Map row, SI draft if
    #     different-GSTIN, STN or PO branch to EE).
    #   - on_cancel: stub-blocker for DNs with an EE-pushed Transfer
    #     Map (EE cancelOrder payload UNGROUNDED per §10.G). DNs
    #     not yet pushed pass through.
    #   - on_update_after_submit: same stub-blocker for amends.
    "Delivery Note": {
        # before_validate runs before ERPNext's own validate — it sets
        # items[].warehouse + target_warehouse from the §10 header
        # fields so ERPNext's stock-item-needs-warehouse rule passes.
        "before_validate": "ecommerce_super.easyecom.flows.transfer_push.section10_before_save",
        "validate": "ecommerce_super.easyecom.flows.transfer_push.validate_pre_submit",
        "on_submit": "ecommerce_super.easyecom.flows.transfer_push.enqueue_on_dn_submit",
        "on_cancel": "ecommerce_super.easyecom.flows.transfer_push.block_dn_cancel",
        "on_update_after_submit": "ecommerce_super.easyecom.flows.transfer_push.block_dn_amend_after_submit",
    },
    # §10 Stage 3 — Sales Invoice on_submit: auto-retry §10 drafted
    # IPRs whose source-side SI just crystallised. No-op for non-§10
    # SIs (the ecs_section10_transfer_map back-ref empty check guards).
    "Sales Invoice": {
        "on_submit": "ecommerce_super.easyecom.flows.transfer_inbound.on_sales_invoice_submit",
    },
    # §10 Stage 3 — Purchase Invoice on_submit: when a draft Debit Note
    # becomes Submitted, transition Transfer Map to DN-Submitted-Locked
    # so subsequent GRNs hit the §7 late-GRN block. No-op for non-return
    # PIs.
    "Purchase Invoice": {
        "on_submit": "ecommerce_super.easyecom.flows.transfer_inbound.on_purchase_invoice_submit",
    },
    # §10 UX: keep Warehouse.ecs_ee_location_label in sync with EE
    # Location mapping. The label is what surfaces in DN / PO / SI
    # warehouse autocompletes so users can see EE-mapping at a glance
    # before they pick a warehouse.
    "EasyEcom Location": {
        # Frappe accepts a list per event-slot — runs handlers in order.
        # warehouse_label_sync keeps Warehouse.ecs_ee_location_label
        # current; warehouse_address_sync mirrors the Location's
        # address fields onto a Warehouse-linked Address.
        "after_save": [
            "ecommerce_super.easyecom.flows.warehouse_label_sync.sync_on_location_save",
            "ecommerce_super.easyecom.flows.warehouse_address_sync.sync_on_location_save",
        ],
        "on_trash": "ecommerce_super.easyecom.flows.warehouse_label_sync.sync_on_location_trash",
    },
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
        # §8e Stage 6 daily customer pull. Runs at 05:30 IST (after
        # Items at 05:00 — the customer master doesn't depend on the
        # product master, but staggering keeps the EE-side per-minute
        # API budget unburdened on a single tick).
        # FULL pull every run — /Wholesale/v2/UserManagement exposes
        # NO updated_after / high-water field (verified live against
        # the captured Harmony fixture). Acceptable given the
        # wholesale-master cardinality (Harmony: 23 customers).
        # Phase-aware: process_one_customer reads customer_master_mode
        # and branches to drift detection in steady state.
        "30 5 * * *": [
            "ecommerce_super.easyecom.flows.customer_pull.scheduled_discover_customers",
        ],
        # §8f Stage 6 daily supplier pull. Runs at 06:00 IST (after
        # Items 05:00 + Customers 05:30 — staggered so the per-minute
        # EE API budget isn't hammered on a single tick).
        # DELTA pull — /wms/V2/getVendors accepts an `updated_after`
        # YYYY-MM-DD filter (verified live 2026-05-27 against
        # Harmony — a future date returns "No Data Found"; an old
        # date filters on EE's internal last-updated timestamp). The
        # scheduler reads supplier_pull_last_updated_at from the
        # Account (set by _set_clean_completion on a clean walk) and
        # passes it as updated_after. First-ever scheduled run with a
        # blank high-water falls through to a full pull.
        # Phase-aware: process_one_supplier reads supplier_master_mode
        # and branches to drift detection in steady state.
        "0 6 * * *": [
            "ecommerce_super.easyecom.flows.supplier_pull.scheduled_discover_suppliers",
        ],
        # Connection health rollup — every 5 minutes.
        # §11 Phase 1 Stage 3 — B2B Order Map polling reconciliation.
        # Scheduler tick is fixed at 5 min; per-Account
        # ecs_polling_cadence_minutes Custom Field (default 15) gates
        # which Maps qualify per tick. Per-Map probe via
        # /orders/V2/getOrderDetails?reference_code=<SO.name> — no
        # cursor, no watermark, no date constraint.
        "*/5 * * * *": [
            "ecommerce_super.easyecom.operational.connection_health.update_account_connection_status",
            "ecommerce_super.easyecom.flows.b2b_sales.polling.reconcile_all_pending_b2b_orders",
            # §12 B2C — per-Marketplace-Account walker over
            # /orders/V2/getAllOrders?status=Manifested. Per-Account
            # polling_cadence_minutes (default 5) gates which Accounts
            # qualify per tick. Cursor on Marketplace Account.last_pull_orders.
            "ecommerce_super.easyecom.flows.b2c_sales.polling.reconcile_all_marketplace_accounts",
        ],
        # Reclaim Queue Job rows in state=Running with no live RQ job (§6.3.9).
        # gh#120 also uses this hourly slot: Held-Pre-QC GRN re-sweep.
        # The forward-only created_after watermark can leave GRNs stranded
        # when their QC completes after the watermark advances; this
        # catches them independently by walking the Held-Pre-QC subset.
        # Bounded by _HELD_PREQC_STALENESS_DAYS (30d) so we don't hammer
        # EE for GRNs that will never QC-complete. Idempotent — no-op
        # when there are no held rows.
        "0 */1 * * *": [
            "ecommerce_super.easyecom.queue.workers.reclaim_orphaned_jobs",
            "ecommerce_super.easyecom.flows.grn_pull.resweep_held_pre_qc_grns",
        ],
        # §10 Stage 4 — daily aged-GIT scan. Runs at 06:30 IST (after
        # the §8f supplier pull at 06:00, before business hours start).
        # Safe to wire by default: scan_all_aged_git only READS Transfer
        # Map state + WRITES ToDo / Comment rows, no EE-side writes,
        # no cold-start backstop risk (unlike §9 GRN-pull's high-
        # watermark concern). Pause-respect baked in: scan skips
        # paused accounts (ToDo creation IS an integration-driven
        # write).
        "30 6 * * *": [
            "ecommerce_super.easyecom.flows.transfer_aged_git.scan_all_aged_git",
        ],
        # §9 Stage 4 — GRN-pull delta cron is INTENTIONALLY NOT YET
        # WIRED. The packet (line 94) authorises a 30-min cron, but
        # auto-firing scheduled_grn_pull on an existing Account is
        # unsafe by default: NULL grn_pull_high_watermark falls back
        # to EE's 7-day backstop, so the first cron tick on Harmony
        # would create PRs for already-manually-receipted historical
        # GRNs. Pull is not gated on auto_push_pos_on_save (push
        # flag, doesn't apply to pull), and scheduled_grn_pull
        # currently doesn't gate on sync_enabled_grn either. Wiring
        # is held until the cold-start safety gates land in the §9
        # closeout (separate packet) — gate on sync_enabled_grn,
        # refuse on NULL watermark, and require an explicit FDE
        # kickoff action that primes the watermark. The handler
        # `scheduled_grn_pull` ships and is manually invokable per
        # the test suite; only the cron auto-fire is deferred.
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

# gh#14 follow-up — per-doc Company scoping. permission_query_conditions
# above only filters list views; without a has_permission hook, direct
# URL access (/app/easyecom-company-settings/<name>) lets a user open
# any doc regardless of User Permission. _COMPANY_SCOPED_DOCTYPES that
# don't already have a specialized has_permission hook (the three
# append-only / audit_no_modify entries below) get company_scope_doc.
_HAS_PERMISSION_OVERRIDES = {
    "EasyEcom API Call": "ecommerce_super.easyecom.permissions.append_only",
    "EasyEcom Webhook Event": "ecommerce_super.easyecom.permissions.append_only",
    "EasyEcom Configuration Audit": "ecommerce_super.easyecom.permissions.audit_no_modify",
    # gh#14 follow-up #2 — restrict write/delete on EasyEcom Account to
    # System Manager + EasyEcom System Manager. DocPerm already encodes
    # this (FDE role only has `read: 1`), but the reporter found that
    # the form's Actions menu / Bulk Edit UI surfaces still rendered for
    # FDE users, letting them proceed toward modification before the
    # server-side reject. has_permission hooks fire at perm-check time,
    # so the form layer reads them and hides the edit affordances.
    "EasyEcom Account": (
        "ecommerce_super.easyecom.permissions.restrict_account_write"
    ),
}

has_permission = {
    **_HAS_PERMISSION_OVERRIDES,
    **{
        dt: "ecommerce_super.easyecom.permissions.company_scope_doc"
        for dt in _COMPANY_SCOPED_DOCTYPES
        if dt not in _HAS_PERMISSION_OVERRIDES
    },
}
