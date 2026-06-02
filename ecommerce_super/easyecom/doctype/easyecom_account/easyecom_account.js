// Client-side form behaviour for EasyEcom Account.
//
// Top-bar actions are organised into Frappe button GROUPS via
// frm.add_custom_button(label, callback, group):
//
//   - "Discover" group  → Locations (8a), Channels (8b), … later: Items
//                         (8d), Customers (8e), Suppliers (8f).
//   - "Pull" group      → reserved for manual pulls (Orders, GRNs,
//                         Returns) when those flows ship.
//   - "Push" group      → reserved for manual pushes (POs, SOs, B2B
//                         Invoices) when those flows ship.
//
// Test Connection stays as a standalone field-level button — it is a
// connectivity check, not a Discover/Pull/Push action.
//
// Each Discover action checks its own preconditions (saved doc, default
// location, dependency on Location rows for Channels) before firing, and
// reports the summary inline via msgprint with a deep link to the
// follow-up FDE worklist (To Map / Unclassified / etc.).

function _ensureSaved(frm, message) {
    if (frm.is_new()) {
        frappe.msgprint({
            title: __("Save First"),
            message: __(message),
            indicator: "orange",
        });
        return false;
    }
    return true;
}

function _runLocationDiscovery(frm) {
    if (!_ensureSaved(frm, "Save the Account before running discovery — the EasyEcom client reads credentials and the default location from the persisted record.")) {
        return;
    }
    if (!frm.doc.default_location_key) {
        frappe.msgprint({
            title: __("Default Location Required"),
            message: __(
                "Set Default Location (typically the primary location) before discovering — the foundational /getAllLocation call needs its JWT to authenticate."
            ),
            indicator: "orange",
        });
        return;
    }

    frappe.show_alert({ message: __("Pulling /getAllLocation…"), indicator: "blue" });

    frappe.call({
        method: "ecommerce_super.easyecom.flows.location_discovery.discover_locations",
        freeze: true,
        freeze_message: __("Discovering EasyEcom locations…"),
        callback(r) {
            const result = r.message || {};
            if (!result.ok) {
                frappe.msgprint({
                    title: __("Discovery Failed"),
                    message: result.message || __("Unknown error."),
                    indicator: "red",
                });
                return;
            }
            const lines = [
                __("Total: {0} | New: {1} | Updated: {2} | Failed: {3}", [
                    result.total,
                    result.new_count,
                    result.updated_count,
                    result.failed_count,
                ]),
            ];
            if (result.new_count > 0) {
                const url = "/app/easyecom-location/view/list?workflow_state=To Map";
                lines.push(
                    `<br><a href="${url}">${__("Open {0} new location(s) waiting to map →", [result.new_count])}</a>`
                );
            }
            if (result.failed_count > 0) {
                lines.push(
                    `<br><br><b>${__("Failed rows:")}</b><br>` +
                        (result.failed_locations || [])
                            .map(
                                (f) =>
                                    `<code>${frappe.utils.escape_html(f.location_key)}</code>: ${frappe.utils.escape_html(f.error)}`
                            )
                            .join("<br>")
                );
            }
            frappe.msgprint({
                title: __("Discovery Complete"),
                message: lines.join(""),
                indicator: result.failed_count > 0 ? "orange" : "green",
            });
        },
        error() {
            frappe.msgprint({
                title: __("Discovery Failed"),
                message: __("The Discover Locations call itself failed (network, server, or permission)."),
                indicator: "red",
            });
        },
    });
}

function _runChannelDiscovery(frm) {
    if (!_ensureSaved(frm, "Save the Account before running channel discovery — the sweep needs persisted EasyEcom Location rows to poll against.")) {
        return;
    }
    // Channel discovery sweeps EVERY discovered EasyEcom Location — if
    // there are none, surface the dependency clearly.
    frappe.db.count("EasyEcom Location").then((n) => {
        if (!n) {
            frappe.msgprint({
                title: __("No Locations Discovered Yet"),
                message: __(
                    "Channel discovery is a per-location sweep — it needs EasyEcom Location rows to poll. Run <b>Discover → Locations</b> first."
                ),
                indicator: "orange",
            });
            return;
        }
        frappe.show_alert({
            message: __("Sweeping /current-channel-status across all locations…"),
            indicator: "blue",
        });
        frappe.call({
            method: "ecommerce_super.easyecom.flows.channel_discovery.discover_channels",
            freeze: true,
            freeze_message: __("Discovering EasyEcom channels…"),
            callback(r) {
                const result = r.message || {};
                if (!result.ok) {
                    frappe.msgprint({
                        title: __("Channel Discovery Failed"),
                        message: result.message || __("Unknown error."),
                        indicator: "red",
                    });
                    return;
                }
                const lines = [
                    __(
                        "Locations polled: {0} | Failed: {1} | Channels: {2} ({3} new, {4} existing)",
                        [
                            result.locations_polled,
                            result.locations_failed,
                            result.channels_total,
                            result.channels_new,
                            result.channels_existing,
                        ]
                    ),
                ];
                if (result.channels_new > 0) {
                    const url = "/app/marketplace/view/list?workflow_state=Unclassified";
                    lines.push(
                        `<br><a href="${url}">${__("Open {0} new channel(s) waiting to classify →", [result.channels_new])}</a>`
                    );
                }
                if (result.locations_failed > 0) {
                    lines.push(
                        `<br><br><b>${__("Failed locations:")}</b><br>` +
                            (result.failed_locations || [])
                                .map(
                                    (f) =>
                                        `<code>${frappe.utils.escape_html(f.location_key)}</code>: ${frappe.utils.escape_html(f.error)}`
                                )
                                .join("<br>")
                    );
                }
                frappe.msgprint({
                    title: __("Channel Discovery Complete"),
                    message: lines.join(""),
                    indicator: result.locations_failed > 0 ? "orange" : "green",
                });
            },
            error() {
                frappe.msgprint({
                    title: __("Channel Discovery Failed"),
                    message: __(
                        "The Discover Channels call itself failed (network, server, or permission)."
                    ),
                    indicator: "red",
                });
            },
        });
    });
}

function _runProductDiscovery(frm) {
    if (!_ensureSaved(frm, "Save the Account before discovering products.")) {
        return;
    }
    const isErpnextMastered = frm.doc.item_master_mode === "erpnext_mastered";
    const verb = isErpnextMastered
        ? __("Enqueuing drift detection (§8d Pull post-flip)…")
        : __("Enqueuing /Products/GetProductMaster pull…");
    frappe.show_alert({message: verb, indicator: "blue"});

    frappe.call({
        method: "ecommerce_super.easyecom.flows.item_pull.discover_products",
        args: {account: frm.doc.name},
        // Async-by-default: the server enqueues and returns immediately,
        // so no freeze needed. The HTTP call itself is fast.
        callback(r) {
            const result = r.message || {};
            if (!result.ok) {
                frappe.msgprint({
                    title: __("Product Discovery Failed"),
                    message: result.message || __("Unknown error."),
                    indicator: "red",
                });
                return;
            }
            // Async response: server enqueued into the long queue.
            if (result.enqueued) {
                frappe.msgprint({
                    title: __("Product Discovery Enqueued"),
                    message: __(
                        "<b>Discovery is running in the background.</b><br><br>" +
                            "The cursor advances page-by-page on the EasyEcom Account — refresh this form to see <code>item_pull_cursor_at</code> update. " +
                            "Created Items + Map rows appear in the Item Map list as the worker pulls them.<br><br>" +
                            "RQ job: <code>{0}</code> (long queue, 3600s timeout)<br><br>" +
                            "<a href='/app/easyecom-item-map'>Open EasyEcom Item Map list →</a> · " +
                            "<a href='/app/error-log?reference_doctype=EasyEcom Account'>Open Error Log →</a>",
                        [frappe.utils.escape_html(result.job_id || "(no id)")]
                    ),
                    indicator: "blue",
                });
                return;
            }
            // Sync (inline=True) response: render the rich result.
            const lines = [
                __(
                    "Reported: {0} | Pages: {1} | Processed: {2}",
                    [result.total_reported || "?", result.pages_walked, result.products_processed]
                ),
            ];
            const byStatus = result.by_status || {};
            const statusBits = Object.entries(byStatus)
                .map(([k, v]) => `${frappe.utils.escape_html(k)}: ${v}`)
                .join(" | ");
            if (statusBits) {
                lines.push(`<br>By status: ${statusBits}`);
            }
            if (result.more_to_walk) {
                lines.push(`<br><b>Resume cursor still set — re-click to continue.</b>`);
            }
            if ((result.page_failures || []).length) {
                lines.push(
                    `<br><br><b>Page failures:</b><br>` +
                    result.page_failures.map(f =>
                        `<code>${frappe.utils.escape_html(f.ee_sku || "?")}</code>: ${frappe.utils.escape_html(f.error || "?")}`
                    ).join("<br>")
                );
            }
            lines.push(
                `<br><br><a href="/app/easyecom-item-map">Open EasyEcom Item Map list →</a>`
            );
            frappe.msgprint({
                title: __("Product Discovery Complete"),
                message: lines.join(""),
                indicator: (result.page_failures || []).length ? "orange" : "green",
            });
        },
        error() {
            frappe.msgprint({
                title: __("Product Discovery Failed"),
                message: __("The Discover Products call itself failed (network or permission)."),
                indicator: "red",
            });
        },
    });
}

function _runPullAllPendingGrns(frm) {
    if (!_ensureSaved(frm, "Save the Account before pulling GRNs.")) {
        return;
    }
    frappe.show_alert({
        message: __("Pulling GRNs from EE…"),
        indicator: "blue",
    });
    frappe.call({
        method: "ecommerce_super.easyecom.flows.grn_pull.scheduled_grn_pull",
        args: {account_name: frm.doc.name},
        freeze: true,
        freeze_message: __("Pulling GRNs from EasyEcom…"),
        callback(r) {
            const result = r.message || {};
            if (!result.ok) {
                frappe.msgprint({
                    title: __("Pull Failed"),
                    message: result.message || __("Unknown error."),
                    indicator: "red",
                });
                return;
            }
            const summaries = result.summaries || [];
            const total_grns = summaries.reduce(
                (sum, s) => sum + (s.grns || 0), 0);
            const lines = [
                __("Pulled {0} GRN(s) across {1} location(s).",
                    [total_grns, summaries.length]),
                "<br><br>",
            ];
            summaries.forEach((s) => {
                if (s.error) {
                    lines.push(
                        `<code>${frappe.utils.escape_html(s.location_key)}</code>: ` +
                        `<span style="color:#dc2626">${frappe.utils.escape_html(s.error)}</span><br>`
                    );
                } else {
                    lines.push(
                        `<code>${frappe.utils.escape_html(s.location_key)}</code>: ` +
                        `${s.grns || 0} GRN(s) on ${s.pages || 0} page(s)<br>`
                    );
                }
            });
            frappe.msgprint({
                title: __("GRN Pull Complete"),
                message: lines.join(""),
                indicator: total_grns > 0 ? "green" : "grey",
            });
        },
        error() {
            frappe.msgprint({
                title: __("GRN Pull Failed"),
                message: __("The scheduled_grn_pull call itself failed."),
                indicator: "red",
            });
        },
    });
}


function _runPushAllPending(frm) {
    if (!_ensureSaved(frm, "Save the Account before running the push sweep.")) {
        return;
    }
    frappe.confirm(
        __(
            "<b>Push All Pending Items?</b><br><br>" +
                "Pushes every ERPNext Item not yet on EasyEcom to <code>CreateMasterProduct</code> " +
                "(bundles dispatch to combo push). The sweep is resumable — items already on EE are skipped. " +
                "<b>This makes real EE writes</b>; ensure your credentials + tax rule maps are correct first."
        ),
        () => {
            frappe.show_alert({
                message: __("Sweeping ERPNext catalogue → EE…"),
                indicator: "blue",
            });
            frappe.call({
                method: "ecommerce_super.easyecom.flows.item_push.push_all_pending_products",
                args: {account: frm.doc.name},
                freeze: true,
                freeze_message: __("Pushing items to EasyEcom…"),
                callback(r) {
                    const result = r.message || {};
                    if (!result.ok) {
                        frappe.msgprint({
                            title: __("Push Sweep Failed"),
                            message: result.message || __("Unknown error."),
                            indicator: "red",
                        });
                        return;
                    }
                    const lines = [
                        __(
                            "Considered: {0} | Created: {1} | Updated: {2} | Skipped: {3} | Flagged: {4}",
                            [result.total_considered, result.create_count, result.update_count,
                                result.skipped_count, result.flagged_count]
                        ),
                    ];
                    if ((result.failed_sample || []).length) {
                        lines.push(
                            `<br><br><b>Flagged sample:</b><br>` +
                            result.failed_sample.map(f =>
                                `<code>${frappe.utils.escape_html(f.item_code)}</code>: ${frappe.utils.escape_html(f.reason)}`
                            ).join("<br>")
                        );
                    }
                    frappe.msgprint({
                        title: __("Push Sweep Complete"),
                        message: lines.join(""),
                        indicator: result.flagged_count > 0 ? "orange" : "green",
                    });
                },
                error() {
                    frappe.msgprint({
                        title: __("Push Sweep Failed"),
                        message: __("The Push All Pending call itself failed."),
                        indicator: "red",
                    });
                },
            });
        }
    );
}

function _runGoLiveEnableAutoPush(frm) {
    if (!_ensureSaved(frm, "Save the Account before going live on auto-push.")) {
        return;
    }
    // Build the per-entity current-state snapshot so the dialog shows
    // the FDE which entities are already enabled vs OFF (idempotent
    // re-click on already-enabled doesn't surprise anyone).
    const currentItems = !!frm.doc.auto_push_on_save;
    const currentCust = !!frm.doc.auto_push_customers_on_save;
    const currentSupp = !!frm.doc.auto_push_suppliers_on_save;

    const d = new frappe.ui.Dialog({
        title: __("Go Live — Enable Auto-Push"),
        fields: [
            {
                fieldtype: "HTML",
                options: __(
                    "<div style='padding:8px;background:#fef3c7;border-radius:4px;margin-bottom:12px;'>" +
                        "<b>This is the steady-state transition.</b> Every ERPNext-side save on " +
                        "the enabled entities will enqueue an EE push (via the on_update hook). " +
                        "Use this ONCE after onboarding reconciliation is complete; use Pause to " +
                        "roll back at any time.<br><br>" +
                        "<b>Prerequisite (warned but not enforced):</b> each entity should be " +
                        "flipped to <code>erpnext_mastered</code> first — pushing while still in " +
                        "<code>onboarding</code> races the pull's accept-and-create logic."
                    + "</div>"
                ),
            },
            {
                fieldname: "items",
                fieldtype: "Check",
                label: __("Enable Items auto-push (§8d)"),
                default: currentItems ? 0 : 1,
                description: __("Currently: {0}",
                    [currentItems ? "<b>ON</b> — re-enabling is a no-op" : "OFF"]),
            },
            {
                fieldname: "customers",
                fieldtype: "Check",
                label: __("Enable Customers auto-push (§8e)"),
                default: currentCust ? 0 : 1,
                description: __("Currently: {0}",
                    [currentCust ? "<b>ON</b> — re-enabling is a no-op" : "OFF"]),
            },
            {
                fieldname: "suppliers",
                fieldtype: "Check",
                label: __("Enable Suppliers auto-push (§8f)"),
                default: currentSupp ? 0 : 1,
                description: __("Currently: {0}",
                    [currentSupp ? "<b>ON</b> — re-enabling is a no-op" : "OFF"]),
            },
        ],
        primary_action_label: __("Confirm Go Live"),
        primary_action(values) {
            if (!values.items && !values.customers && !values.suppliers) {
                frappe.msgprint({
                    title: __("Nothing Selected"),
                    message: __("Pick at least one entity to enable, or cancel."),
                    indicator: "orange",
                });
                return;
            }
            frappe.call({
                method:
                    "ecommerce_super.easyecom.api.auto_push_controls.go_live_enable_auto_push",
                args: {
                    account: frm.doc.name,
                    items: values.items ? 1 : 0,
                    customers: values.customers ? 1 : 0,
                    suppliers: values.suppliers ? 1 : 0,
                    confirm: 1,
                },
                freeze: true,
                freeze_message: __("Enabling auto-push…"),
                callback(r) {
                    const result = r.message || {};
                    if (!result.ok) {
                        frappe.msgprint({
                            title: __("Go Live Failed"),
                            message: result.message || __("Unknown error."),
                            indicator: "red",
                        });
                        return;
                    }
                    let body = __(
                        "<b>Enabled:</b> {0}<br><br><b>Current state:</b> Items=<code>{1}</code>, Customers=<code>{2}</code>, Suppliers=<code>{3}</code>",
                        [
                            (result.transitioned || []).join(", ") || "(none)",
                            result.state.items, result.state.customers, result.state.suppliers,
                        ]
                    );
                    if ((result.warnings || []).length) {
                        body += "<br><br><b>Warnings:</b><br>" +
                            result.warnings
                                .map(w => "&bull; " + frappe.utils.escape_html(w))
                                .join("<br>");
                    }
                    body += "<br><br><i>Audit Comment added to this Account's Activity log.</i>";
                    frappe.msgprint({
                        title: __("Go Live Complete"),
                        message: body,
                        indicator: (result.warnings || []).length ? "orange" : "green",
                    });
                    d.hide();
                    frm.reload_doc();
                },
            });
        },
    });
    d.show();
}

function _runBootstrapSection10Customer(frm) {
    if (!_ensureSaved(frm, "Save the Account before bootstrapping the §10 Internal Customer.")) {
        return;
    }
    frappe.db
        .get_list("Company", {fields: ["name"], limit: 100, order_by: "name asc"})
        .then((companies) => {
            if (!companies.length) {
                frappe.msgprint({
                    title: __("No Companies"),
                    message: __(
                        "There are no Companies in the system. Create at least one before bootstrapping the §10 Internal Customer."
                    ),
                    indicator: "orange",
                });
                return;
            }
            const opts = companies.map((c) => c.name).join("\n");
            const d = new frappe.ui.Dialog({
                title: __("Bootstrap §10 Internal Customer"),
                fields: [
                    {
                        fieldtype: "HTML",
                        options: __(
                            "<div style='padding:8px;background:#eff6ff;border-radius:4px;margin-bottom:12px;font-size:13px;line-height:1.5;'>" +
                                "<b>One-click §10 onboarding.</b> Creates (or reuses) the Internal Customer that drives §10 transfers, fills <i>Allowed To Transact With</i> with every other Company, links the EE-managed Warehouse addresses (synced from EasyEcom Locations), and pushes the Customer to EE so the <code>ee_customer_id</code> / Customer Map row exist before the first §10 DN fires.<br><br>" +
                                "Idempotent — re-running just refreshes companies + addresses + Map row." +
                                "</div>"
                        ),
                    },
                    {
                        fieldname: "represents_company",
                        fieldtype: "Select",
                        label: __("Represents Company"),
                        reqd: 1,
                        options: opts,
                        default: companies[0].name,
                        description: __(
                            "The Company this Customer represents. Pick your primary entity — §10's loose-match path still routes transfers from any other Company through this Customer."
                        ),
                    },
                    {
                        fieldname: "customer_name",
                        fieldtype: "Data",
                        label: __("Customer Name (optional)"),
                        description: __(
                            "Defaults to <code>Internal — &lt;Company&gt;</code> if left blank."
                        ),
                    },
                    {
                        fieldname: "push_to_ee",
                        fieldtype: "Check",
                        label: __("Push to EasyEcom"),
                        default: 1,
                        description: __(
                            "Triggers <code>/Wholesale/CreateCustomer</code> (or UpdateCustomer if already mapped) so the EE wholesale <code>c_id</code> + Customer Map row land before the first §10 B2B DN."
                        ),
                    },
                ],
                primary_action_label: __("Bootstrap"),
                primary_action(values) {
                    frappe.call({
                        method:
                            "ecommerce_super.easyecom.api.section10_bootstrap.bootstrap_section10_internal_customer",
                        args: values,
                        freeze: true,
                        freeze_message: __("Bootstrapping §10 Internal Customer…"),
                        callback(r) {
                            const result = r.message || {};
                            if (!result.ok) {
                                frappe.msgprint({
                                    title: __("Bootstrap Failed"),
                                    message: result.message || __("Unknown error."),
                                    indicator: "red",
                                });
                                return;
                            }
                            const push = result.push_to_ee || {};
                            const lines = [
                                __("Customer: <code>{0}</code> ({1})", [
                                    frappe.utils.escape_html(result.customer),
                                    result.customer_created
                                        ? "<b>created</b>"
                                        : "reused",
                                ]),
                                __(
                                    "Represents Company: <code>{0}</code> · Allowed companies: <b>{1}</b> ({2} newly added)",
                                    [
                                        frappe.utils.escape_html(result.represents_company),
                                        result.companies_total,
                                        result.companies_added,
                                    ]
                                ),
                                __("Addresses linked: <b>{0}</b>", [
                                    (result.addresses_linked || []).length,
                                ]),
                            ];
                            if (push.ee_customer_id) {
                                lines.push(
                                    __(
                                        "EE Customer ID: <code>{0}</code> · push operation: <code>{1}</code>",
                                        [
                                            frappe.utils.escape_html(
                                                String(push.ee_customer_id)
                                            ),
                                            frappe.utils.escape_html(
                                                push.operation || "—"
                                            ),
                                        ]
                                    )
                                );
                            }
                            if ((push.flag_reasons || []).length) {
                                lines.push(
                                    "<br><b>Push flags:</b><br>" +
                                        push.flag_reasons
                                            .map((r) =>
                                                "&bull; " + frappe.utils.escape_html(r)
                                            )
                                            .join("<br>")
                                );
                            }
                            lines.push(
                                "<br><a href='/app/customer/" +
                                    encodeURIComponent(result.customer) +
                                    "'>Open Customer →</a>"
                            );
                            frappe.msgprint({
                                title: __("§10 Internal Customer Ready"),
                                message: lines.join("<br>"),
                                indicator: push.ee_customer_id
                                    ? "green"
                                    : "orange",
                            });
                            d.hide();
                        },
                        error() {
                            frappe.msgprint({
                                title: __("Bootstrap Failed"),
                                message: __(
                                    "The bootstrap call itself failed (network or permission)."
                                ),
                                indicator: "red",
                            });
                        },
                    });
                },
            });
            d.show();
        });
}


function _runBootstrapSection10Supplier(frm) {
    if (!_ensureSaved(frm, "Save the Account before bootstrapping the §10 Internal Supplier.")) {
        return;
    }
    frappe.db
        .get_list("Company", {fields: ["name"], limit: 100, order_by: "name asc"})
        .then((companies) => {
            if (!companies.length) {
                frappe.msgprint({
                    title: __("No Companies"),
                    message: __(
                        "There are no Companies in the system. Create at least one before bootstrapping the §10 Internal Supplier."
                    ),
                    indicator: "orange",
                });
                return;
            }
            const opts = companies.map((c) => c.name).join("\n");
            const d = new frappe.ui.Dialog({
                title: __("Bootstrap §10 Internal Supplier"),
                fields: [
                    {
                        fieldtype: "HTML",
                        options: __(
                            "<div style='padding:8px;background:#eff6ff;border-radius:4px;margin-bottom:12px;font-size:13px;line-height:1.5;'>" +
                                "<b>§10 PO branch setup.</b> The PO branch fires when source warehouse is NOT EE-mapped but target IS — EE needs a vendor identity. This action creates (or reuses) the Internal Supplier representing the source Company, fills <i>Allowed To Transact With</i> with every other Company, links the EE-managed warehouse addresses for the represented Company, and pushes the Supplier to EE so <code>ee_vendor_id</code> / Supplier Map land before the first §10 PO DN fires.<br><br>" +
                                "Strict-match model: one Internal Supplier per source Company. Run once per source Company you'll transfer FROM via the PO branch." +
                                "</div>"
                        ),
                    },
                    {
                        fieldname: "represents_company",
                        fieldtype: "Select",
                        label: __("Represents Company (the source)"),
                        reqd: 1,
                        options: opts,
                        default: companies[0].name,
                        description: __(
                            "The source Company this Supplier represents — the one whose warehouses EE will see goods leaving from in PO-branch transfers."
                        ),
                    },
                    {
                        fieldname: "supplier_name",
                        fieldtype: "Data",
                        label: __("Supplier Name (optional)"),
                        description: __(
                            "Defaults to <code>Internal — &lt;Company&gt; (Vendor)</code> if left blank."
                        ),
                    },
                    {
                        fieldname: "push_to_ee",
                        fieldtype: "Check",
                        label: __("Push to EasyEcom"),
                        default: 1,
                        description: __(
                            "Triggers <code>/wms/CreateVendor</code> (or UpdateVendor if already mapped) so the EE <code>ee_vendor_id</code> + Supplier Map row land before the first §10 PO DN."
                        ),
                    },
                ],
                primary_action_label: __("Bootstrap"),
                primary_action(values) {
                    frappe.call({
                        method:
                            "ecommerce_super.easyecom.api.section10_bootstrap.bootstrap_section10_internal_supplier",
                        args: values,
                        freeze: true,
                        freeze_message: __("Bootstrapping §10 Internal Supplier…"),
                        callback(r) {
                            const result = r.message || {};
                            if (!result.ok) {
                                frappe.msgprint({
                                    title: __("Bootstrap Failed"),
                                    message: result.message || __("Unknown error."),
                                    indicator: "red",
                                });
                                return;
                            }
                            const push = result.push_to_ee || {};
                            const lines = [
                                __("Supplier: <code>{0}</code> ({1})", [
                                    frappe.utils.escape_html(result.supplier),
                                    result.supplier_created
                                        ? "<b>created</b>"
                                        : "reused",
                                ]),
                                __(
                                    "Represents Company: <code>{0}</code> · Allowed companies: <b>{1}</b> ({2} newly added)",
                                    [
                                        frappe.utils.escape_html(result.represents_company),
                                        result.companies_total,
                                        result.companies_added,
                                    ]
                                ),
                                __("Addresses linked: <b>{0}</b>", [
                                    (result.addresses_linked || []).length,
                                ]),
                            ];
                            if (push.ee_vendor_id) {
                                lines.push(
                                    __(
                                        "EE Vendor ID: <code>{0}</code> · push operation: <code>{1}</code>",
                                        [
                                            frappe.utils.escape_html(
                                                String(push.ee_vendor_id)
                                            ),
                                            frappe.utils.escape_html(
                                                push.operation || "—"
                                            ),
                                        ]
                                    )
                                );
                            }
                            if ((push.flag_reasons || []).length) {
                                lines.push(
                                    "<br><b>Push flags:</b><br>" +
                                        push.flag_reasons
                                            .map((r) =>
                                                "&bull; " + frappe.utils.escape_html(r)
                                            )
                                            .join("<br>")
                                );
                            }
                            lines.push(
                                "<br><a href='/app/supplier/" +
                                    encodeURIComponent(result.supplier) +
                                    "'>Open Supplier →</a>"
                            );
                            frappe.msgprint({
                                title: __("§10 Internal Supplier Ready"),
                                message: lines.join("<br>"),
                                indicator: push.ee_vendor_id
                                    ? "green"
                                    : "orange",
                            });
                            d.hide();
                        },
                        error() {
                            frappe.msgprint({
                                title: __("Bootstrap Failed"),
                                message: __(
                                    "The bootstrap call itself failed (network or permission)."
                                ),
                                indicator: "red",
                            });
                        },
                    });
                },
            });
            d.show();
        });
}


function _runPauseAllAutoPush(frm) {
    if (!_ensureSaved(frm, "Save the Account before pausing auto-push.")) {
        return;
    }
    const d = new frappe.ui.Dialog({
        title: __("Pause All Auto-Push (Kill-Switch)"),
        fields: [
            {
                fieldtype: "HTML",
                options: __(
                    "<div style='padding:8px;background:#fee2e2;border-radius:4px;margin-bottom:12px;'>" +
                        "<b>Emergency kill-switch.</b> Sets ALL three auto-push toggles to OFF " +
                        "in one transaction. Use during incidents to stop on_update hooks from " +
                        "firing. Manual pushes via the FDE buttons still work. Re-enable via " +
                        "Go Live action."
                    + "</div>"
                ),
            },
            {
                fieldname: "reason",
                fieldtype: "Small Text",
                label: __("Reason (recorded in audit Comment)"),
                reqd: 0,
                description: __(
                    "Optional but strongly encouraged. Post-incident review reads this."
                ),
            },
        ],
        primary_action_label: __("Pause Now"),
        primary_action(values) {
            frappe.call({
                method:
                    "ecommerce_super.easyecom.api.auto_push_controls.pause_all_auto_push",
                args: {
                    account: frm.doc.name,
                    reason: values.reason || "",
                    confirm: 1,
                },
                freeze: true,
                freeze_message: __("Pausing auto-push…"),
                callback(r) {
                    const result = r.message || {};
                    if (!result.ok) {
                        frappe.msgprint({
                            title: __("Pause Failed"),
                            message: result.message || __("Unknown error."),
                            indicator: "red",
                        });
                        return;
                    }
                    frappe.msgprint({
                        title: __("Auto-Push Paused"),
                        message: __(
                            "{0}<br><br><i>Audit Comment added to this Account's Activity log.</i>",
                            [frappe.utils.escape_html(result.message || "")]
                        ),
                        indicator: "green",
                    });
                    d.hide();
                    frm.reload_doc();
                },
            });
        },
    });
    d.show();
}

// gh#11 — instant in-page popup when a Discover RQ job finishes. The
// worker calls frappe.publish_realtime("easyecom:discover_done", ...,
// user=triggered_by); we subscribe ONCE per page load (not per form
// refresh — refresh fires every time the user re-opens the form, and
// stacking listeners would multiply popups). The Notification Log row
// the worker also writes is the durable bell-icon trail; this listener
// is the "you're still in the desk, here's the result now" channel.
if (!window.__ecs_discover_listener_attached) {
    window.__ecs_discover_listener_attached = true;
    frappe.realtime.on("easyecom:discover_done", (data) => {
        if (!data || typeof data !== "object") {
            return;
        }
        const kind = data.kind || "?";
        const ok = !!data.ok;
        const summary = data.summary || "";
        const route = data.list_route || "";
        const message =
            `<b>Discover ${frappe.utils.escape_html(kind)} ` +
            (ok ? "complete" : "failed") + `</b><br>` +
            frappe.utils.escape_html(summary) +
            (route
                ? `<br><a href="${frappe.utils.escape_html(route)}">Open list →</a>`
                : "");
        frappe.show_alert(
            { message: message, indicator: ok ? "green" : "red" },
            ok ? 10 : 20
        );
    });
}

frappe.ui.form.on("EasyEcom Account", {
    refresh(frm) {
        frm.trigger("update_connection_indicator");

        // Top-bar action groups. Skip on a new (unsaved) doc — the
        // _ensureSaved guards in the handlers also catch this, but
        // hiding the buttons before save keeps the UI honest.
        if (frm.is_new()) {
            return;
        }

        // Discover group — every entity master that ships a pull
        // surface. Each top-button delegates to the same section
        // button handler (frm.events.X_action), so the UX in the
        // dropdown is byte-equivalent to clicking the in-section
        // button — same confirm dialog, same async-enqueue path,
        // same result rendering. The dropdown just saves the FDE
        // a scroll on a tall Account form.
        frm.add_custom_button(
            __("Locations (§8a)"),
            () => _runLocationDiscovery(frm),
            __("Discover")
        );
        frm.add_custom_button(
            __("Channels (§8b)"),
            () => _runChannelDiscovery(frm),
            __("Discover")
        );
        frm.add_custom_button(
            __("Products (§8d)"),
            () => _runProductDiscovery(frm),
            __("Discover")
        );
        frm.add_custom_button(
            __("Customers (§8e)"),
            () => frm.events.discover_customers_action(frm),
            __("Discover")
        );
        frm.add_custom_button(
            __("Suppliers (§8f)"),
            () => frm.events.discover_suppliers_action(frm),
            __("Discover")
        );
        // Shared foundational refresh — gates §8e and §8f.
        frm.add_custom_button(
            __("States / Countries"),
            () => frm.events.refresh_states_countries_action(frm),
            __("Discover")
        );

        // Push group — batch sweeps across §8d / §8e / §8f / §9.
        frm.add_custom_button(
            __("Items (§8d)"),
            () => _runPushAllPending(frm),
            __("Push All Pending")
        );
        frm.add_custom_button(
            __("Customers (§8e)"),
            () => frm.events.push_all_pending_customers_action(frm),
            __("Push All Pending")
        );
        frm.add_custom_button(
            __("Suppliers (§8f)"),
            () => frm.events.push_all_pending_suppliers_action(frm),
            __("Push All Pending")
        );
        frm.add_custom_button(
            __("POs (§9)"),
            () => frm.events.push_all_pending_pos_action(frm),
            __("Push All Pending")
        );

        // Pull group — manual triggers for pull-side flows. Mirrors
        // the in-section buttons so the FDE doesn't have to hunt
        // through tall sections to find them.
        frm.add_custom_button(
            __("GRNs (§9 inbound)"),
            () => _runPullAllPendingGrns(frm),
            __("Pull")
        );

        // Master Mode group — one-way flips per master (Items / Customers /
        // Suppliers). Each delegates to its existing field-event handler so
        // the confirm dialog, already-flipped guard, and audit log behave
        // identically whether triggered from the section button or here.
        frm.add_custom_button(
            __("Flip Items → ERPNext-Mastered"),
            () => frm.events.flip_to_erpnext_mastered_action(frm),
            __("Master Mode")
        );
        frm.add_custom_button(
            __("Flip Customers → ERPNext-Mastered"),
            () => frm.events.flip_to_erpnext_mastered_customers_action(frm),
            __("Master Mode")
        );
        frm.add_custom_button(
            __("Flip Suppliers → ERPNext-Mastered"),
            () => frm.events.flip_to_erpnext_mastered_suppliers_action(frm),
            __("Master Mode")
        );

        // Auto-Push group — steady-state lifecycle controls.
        frm.add_custom_button(
            __("Go Live (Enable Auto-Push)"),
            () => _runGoLiveEnableAutoPush(frm),
            __("Auto-Push")
        );
        frm.add_custom_button(
            __("Pause All (Kill-Switch)"),
            () => _runPauseAllAutoPush(frm),
            __("Auto-Push")
        );

        // §10 Setup group — one-time onboarding actions for the
        // Internal Customer / Internal Supplier fabric that §10 needs.
        frm.add_custom_button(
            __("Bootstrap Internal Customer"),
            () => _runBootstrapSection10Customer(frm),
            __("§10 Setup")
        );
        frm.add_custom_button(
            __("Bootstrap Internal Supplier"),
            () => _runBootstrapSection10Supplier(frm),
            __("§10 Setup")
        );
    },

    discover_products_action(frm) {
        _runProductDiscovery(frm);
    },

    push_all_pending_action(frm) {
        _runPushAllPending(frm);
    },

    pull_all_pending_grns_action(frm) {
        _runPullAllPendingGrns(frm);
    },

    push_all_pending_pos_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before running the PO push sweep.")) {
            return;
        }
        frappe.confirm(
            __(
                "<b>Push all pending POs to EasyEcom?</b><br><br>" +
                    "Enqueues one /WMS/Cart/CreatePurchaseOrder call per candidate. " +
                    "Candidates: PO.docstatus=1, target warehouse EE-mapped, " +
                    "no PO Map row yet OR Map.status=Mapped. Returns immediately; " +
                    "per-PO progress lands in Queue Jobs / PO Map row status."
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.flows.po_push.push_all_pending_pos",
                    args: {account: frm.doc.name},
                    freeze: true,
                    freeze_message: __("Enqueuing PO pushes…"),
                    callback(r) {
                        const result = r.message || {};
                        if (!result.ok) {
                            frappe.msgprint({
                                title: __("PO Push Sweep Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                            return;
                        }
                        frappe.msgprint({
                            title: __("PO Push Enqueued"),
                            message: __(
                                "Considered: <b>{0}</b> | Enqueued: <b>{1}</b><br>" +
                                    "Sample PO names: {2}",
                                [
                                    result.total_considered,
                                    result.enqueued_count,
                                    (result.queue_job_names_sample || []).join(", ") || "—",
                                ]
                            ),
                            indicator: "green",
                        });
                    },
                    error() {
                        frappe.msgprint({
                            title: __("PO Push Sweep Failed"),
                            message: __("The Push All Pending POs call itself failed."),
                            indicator: "red",
                        });
                    },
                });
            }
        );
    },

    go_live_enable_auto_push_action(frm) {
        _runGoLiveEnableAutoPush(frm);
    },

    pause_all_auto_push_action(frm) {
        _runPauseAllAutoPush(frm);
    },

    flip_to_erpnext_mastered_action(frm) {
        if (frm.is_new()) {
            frappe.msgprint({
                title: __("Save First"),
                message: __(
                    "Save the Account before flipping Item Master Mode."
                ),
                indicator: "orange",
            });
            return;
        }
        if (frm.doc.item_master_mode === "erpnext_mastered") {
            frappe.msgprint({
                title: __("Already Flipped"),
                message: __(
                    "Account is already in <b>erpnext_mastered</b> mode (flipped at {0}).",
                    [frappe.datetime.str_to_user(frm.doc.item_master_flipped_at) || "—"]
                ),
                indicator: "grey",
            });
            return;
        }

        frappe.confirm(
            __(
                "<b>Flip to ERPNext-Mastered?</b><br><br>" +
                    "After the flip:<br>" +
                    "&bull; The §8d push (ERPNext → EasyEcom) becomes the authoritative item flow.<br>" +
                    "&bull; The pull becomes <b>drift detection only</b> — EE-side new products and edits to mapped items will flag as <code>Drift</code> for the FDE, NOT auto-create or auto-overwrite.<br><br>" +
                    "Reverse flips require manual intervention (set the field via Console). Make sure onboarding reconciliation is complete before confirming."
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.item_master_mode.flip_to_erpnext_mastered",
                    args: {account: frm.doc.name, confirm: true},
                    freeze: true,
                    freeze_message: __("Flipping…"),
                    callback(r) {
                        const result = r.message || {};
                        if (result.ok) {
                            frappe.show_alert(
                                {
                                    message: __("Flipped to erpnext_mastered."),
                                    indicator: "green",
                                },
                                7
                            );
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __("Flip Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                        }
                    },
                    error() {
                        frappe.msgprint({
                            title: __("Flip Failed"),
                            message: __(
                                "The flip call itself failed (network or permission)."
                            ),
                            indicator: "red",
                        });
                    },
                });
            }
        );
    },

    flip_to_erpnext_mastered_customers_action(frm) {
        if (frm.is_new()) {
            frappe.msgprint({
                title: __("Save First"),
                message: __(
                    "Save the Account before flipping Customer Master Mode."
                ),
                indicator: "orange",
            });
            return;
        }
        if (frm.doc.customer_master_mode === "erpnext_mastered") {
            frappe.msgprint({
                title: __("Already Flipped"),
                message: __(
                    "Account is already in <b>erpnext_mastered</b> mode for Customer master (flipped at {0}).",
                    [frappe.datetime.str_to_user(frm.doc.customer_master_flipped_at) || "—"]
                ),
                indicator: "grey",
            });
            return;
        }

        frappe.confirm(
            __(
                "<b>Flip Customers to ERPNext-Mastered?</b><br><br>" +
                    "After the flip:<br>" +
                    "&bull; The §8.2 push (ERPNext → EasyEcom) becomes the authoritative customer flow.<br>" +
                    "&bull; The pull becomes <b>drift detection only</b> — EE-side new customers and edits to mapped customers will flag as <code>Drift</code> for the FDE, NOT auto-create or auto-overwrite.<br><br>" +
                    "Reverse flips require manual intervention (set the field via Console). Make sure customer reconciliation is complete before confirming. (Independent of the Item flip — this only affects Customer master.)"
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.customer_master_mode.flip_to_erpnext_mastered_customers",
                    args: {account: frm.doc.name, confirm: true},
                    freeze: true,
                    freeze_message: __("Flipping…"),
                    callback(r) {
                        const result = r.message || {};
                        if (result.ok) {
                            frappe.show_alert(
                                {
                                    message: __("Customer master flipped to erpnext_mastered."),
                                    indicator: "green",
                                },
                                7
                            );
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __("Flip Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                        }
                    },
                    error() {
                        frappe.msgprint({
                            title: __("Flip Failed"),
                            message: __(
                                "The flip call itself failed (network or permission)."
                            ),
                            indicator: "red",
                        });
                    },
                });
            }
        );
    },

    flip_to_erpnext_mastered_suppliers_action(frm) {
        if (frm.is_new()) {
            frappe.msgprint({
                title: __("Save First"),
                message: __(
                    "Save the Account before flipping Supplier Master Mode."
                ),
                indicator: "orange",
            });
            return;
        }
        if (frm.doc.supplier_master_mode === "erpnext_mastered") {
            frappe.msgprint({
                title: __("Already Flipped"),
                message: __(
                    "Account is already in <b>erpnext_mastered</b> mode for Supplier master (flipped at {0}).",
                    [frappe.datetime.str_to_user(frm.doc.supplier_master_flipped_at) || "—"]
                ),
                indicator: "grey",
            });
            return;
        }

        frappe.confirm(
            __(
                "<b>Flip Suppliers to ERPNext-Mastered?</b><br><br>" +
                    "After the flip:<br>" +
                    "&bull; The §8.3 push (ERPNext → EasyEcom) becomes the authoritative supplier flow.<br>" +
                    "&bull; The pull becomes <b>drift detection only</b> — EE-side new suppliers and edits to mapped suppliers will flag as <code>Drift</code> for the FDE, NOT auto-create or auto-overwrite.<br><br>" +
                    "Reverse flips require manual intervention (set the field via Console). Make sure supplier reconciliation is complete before confirming. (Independent of the Item/Customer flips — this only affects Supplier master.)"
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.supplier_master_mode.flip_to_erpnext_mastered_suppliers",
                    args: {account: frm.doc.name, confirm: true},
                    freeze: true,
                    freeze_message: __("Flipping…"),
                    callback(r) {
                        const result = r.message || {};
                        if (result.ok) {
                            frappe.show_alert(
                                {
                                    message: __("Supplier master flipped to erpnext_mastered."),
                                    indicator: "green",
                                },
                                7
                            );
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __("Flip Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                        }
                    },
                    error() {
                        frappe.msgprint({
                            title: __("Flip Failed"),
                            message: __(
                                "The flip call itself failed (network or permission)."
                            ),
                            indicator: "red",
                        });
                    },
                });
            }
        );
    },

    refresh_states_countries_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before refreshing reference data.")) {
            return;
        }
        frappe.show_alert({
            message: __("Refreshing /getCountries + /getStates…"),
            indicator: "blue",
        });
        frappe.call({
            method:
                "ecommerce_super.easyecom.api.customer_lookups.refresh_countries_and_states",
            freeze: true,
            freeze_message: __("Refreshing reference data…"),
            callback(r) {
                const result = r.message || {};
                if (!result.ok) {
                    frappe.msgprint({
                        title: __("Refresh Failed"),
                        message: result.message || __("Unknown error."),
                        indicator: "red",
                    });
                    return;
                }
                const lines = [
                    __(
                        "Countries: <b>{0}</b> seen ({1} new, {2} updated, {3} skipped, {4} failed)",
                        [
                            result.countries_total,
                            result.countries_new,
                            result.countries_updated,
                            result.countries_skipped,
                            result.countries_failed_count,
                        ]
                    ),
                    __(
                        "States: <b>{0}</b> seen ({1} new, {2} updated, {3} skipped, {4} failed)",
                        [
                            result.states_total,
                            result.states_new,
                            result.states_updated,
                            result.states_skipped,
                            result.states_failed_count,
                        ]
                    ),
                ];
                if ((result.countries_failed_sample || []).length) {
                    lines.push(
                        "<br><b>Country failures (sample):</b><br>" +
                            result.countries_failed_sample
                                .map(f =>
                                    frappe.utils.escape_html(
                                        `${f.country || "?"} (id=${f.country_id ?? "?"}): ${f.error}`
                                    )
                                )
                                .join("<br>")
                    );
                }
                if ((result.states_failed_sample || []).length) {
                    lines.push(
                        "<br><b>State failures (sample):</b><br>" +
                            result.states_failed_sample
                                .map(f =>
                                    frappe.utils.escape_html(
                                        `${f.state_name || "?"} (id=${f.state_id ?? "?"}, country_id=${f.country_id ?? "?"}): ${f.error}`
                                    )
                                )
                                .join("<br>")
                    );
                }
                const overallOk =
                    result.countries_failed_count === 0 &&
                    result.states_failed_count === 0;
                frappe.msgprint({
                    title: __("Reference Data Refreshed"),
                    message: lines.join("<br>"),
                    indicator: overallOk ? "green" : "orange",
                });
            },
            error() {
                frappe.msgprint({
                    title: __("Refresh Failed"),
                    message: __(
                        "The refresh call itself failed (network or permission)."
                    ),
                    indicator: "red",
                });
            },
        });
    },

    push_all_pending_customers_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before pushing customers.")) {
            return;
        }
        frappe.confirm(
            __(
                "<b>Push all pending Customers to EasyEcom?</b><br><br>" +
                    "Enqueues one /Wholesale/CreateCustomer call per candidate. " +
                    "Candidates: Customer.customer_type=Company, enabled, has email_id, " +
                    "no existing EasyEcom Customer Map row. Returns immediately; " +
                    "per-Customer progress lands in Queue Jobs / Sync Records."
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.customer_push.push_all_pending_customers",
                    args: {account: frm.doc.name},
                    freeze: true,
                    freeze_message: __("Enqueuing customer pushes…"),
                    callback(r) {
                        const result = r.message || {};
                        if (!result.ok) {
                            frappe.msgprint({
                                title: __("Push Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                            return;
                        }
                        frappe.msgprint({
                            title: __("Customer Push Enqueued"),
                            message: __(
                                "Considered: <b>{0}</b> | Enqueued: <b>{1}</b><br>" +
                                    "Sample Queue Jobs: {2}",
                                [
                                    result.total_considered,
                                    result.enqueued_count,
                                    (result.queue_job_names_sample || []).join(", ") || "—",
                                ]
                            ),
                            indicator: "green",
                        });
                    },
                });
            }
        );
    },

    push_all_pending_suppliers_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before pushing suppliers.")) {
            return;
        }
        frappe.confirm(
            __(
                "<b>Push all pending Suppliers to EasyEcom?</b><br><br>" +
                    "Enqueues one /wms/CreateVendor call per candidate. " +
                    "Candidates: Supplier.supplier_type=Company, enabled, " +
                    "no existing EasyEcom Supplier Map row. Returns immediately; " +
                    "per-Supplier progress lands in Queue Jobs / Sync Records."
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.supplier_push.push_all_pending_suppliers",
                    args: {account: frm.doc.name},
                    freeze: true,
                    freeze_message: __("Enqueuing supplier pushes…"),
                    callback(r) {
                        const result = r.message || {};
                        if (!result.ok) {
                            frappe.msgprint({
                                title: __("Push Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                            return;
                        }
                        frappe.msgprint({
                            title: __("Supplier Push Enqueued"),
                            message: __(
                                "Considered: <b>{0}</b> | Enqueued: <b>{1}</b><br>" +
                                    "Sample Queue Jobs: {2}",
                                [
                                    result.total_considered,
                                    result.enqueued_count,
                                    (result.queue_job_names_sample || []).join(", ") || "—",
                                ]
                            ),
                            indicator: "green",
                        });
                    },
                });
            }
        );
    },

    discover_customers_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before discovering customers.")) {
            return;
        }
        frappe.show_alert({
            message: __("Enqueuing /Wholesale/v2/UserManagement pull…"),
            indicator: "blue",
        });
        frappe.call({
            method: "ecommerce_super.easyecom.api.customer_pull.discover_customers",
            // Async-by-default: server enqueues and returns immediately.
            callback(r) {
                const result = r.message || {};
                if (!result.ok) {
                    frappe.msgprint({
                        title: __("Discover Customers Failed"),
                        message: result.message || __("Unknown error."),
                        indicator: "red",
                    });
                    return;
                }
                if (result.enqueued) {
                    frappe.msgprint({
                        title: __("Customer Discovery Enqueued"),
                        message: __(
                            "<b>Discovery is running in the background.</b><br><br>" +
                                "Created Customers + Map rows appear in the Customer Map list as the worker pulls them.<br><br>" +
                                "RQ job: <code>{0}</code> (long queue, 3600s timeout)<br><br>" +
                                "<a href='/app/easyecom-customer-map'>Open Customer Map list →</a> · " +
                                "<a href='/app/error-log'>Open Error Log →</a>",
                            [frappe.utils.escape_html(result.job_id || "(no id)")]
                        ),
                        indicator: "blue",
                    });
                    return;
                }
                // Sync (inline=True) path
                const lines = [
                    __(
                        "Total: <b>{0}</b> | Created: {1} | Skipped (mapped): {2} | Created-Flagged: {3} | FNC: {4} | Failed: {5}",
                        [
                            result.total,
                            result.created,
                            result.skipped,
                            result.created_flagged,
                            result.flagged_not_created,
                            result.failed,
                        ]
                    ),
                ];
                if ((result.failures_sample || []).length) {
                    lines.push(
                        "<br><b>Failure sample:</b><br>" +
                            result.failures_sample
                                .map(f =>
                                    frappe.utils.escape_html(
                                        `c_id=${f.ee_c_id} (${f.companyname || "?"}): ${f.error}`
                                    )
                                )
                                .join("<br>")
                    );
                }
                const overallOk = result.failed === 0;
                frappe.msgprint({
                    title: __("Customer Pull Result"),
                    message: lines.join("<br>"),
                    indicator: overallOk ? "green" : "orange",
                });
            },
            error() {
                frappe.msgprint({
                    title: __("Discover Customers Failed"),
                    message: __(
                        "The pull call itself failed (network or permission)."
                    ),
                    indicator: "red",
                });
            },
        });
    },

    discover_suppliers_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before discovering suppliers.")) {
            return;
        }
        const startFresh = frm.doc.supplier_pull_cursor
            ? false  // resumable when a cursor's saved
            : true;
        frappe.show_alert({
            message: __(
                startFresh
                    ? "Enqueuing /wms/V2/getVendors pull from the top…"
                    : "Enqueuing supplier pull resume from saved cursor…"
            ),
            indicator: "blue",
        });
        frappe.call({
            method: "ecommerce_super.easyecom.api.supplier_pull.discover_suppliers",
            args: {start_fresh: startFresh ? 1 : 0},
            // Async-by-default: server enqueues and returns immediately.
            callback(r) {
                const result = r.message || {};
                if (!result.ok) {
                    frappe.msgprint({
                        title: __("Discover Suppliers Failed"),
                        message: result.message || __("Unknown error."),
                        indicator: "red",
                    });
                    return;
                }
                if (result.enqueued) {
                    frappe.msgprint({
                        title: __("Supplier Discovery Enqueued"),
                        message: __(
                            "<b>Discovery is running in the background.</b><br><br>" +
                                "The cursor advances page-by-page on the EasyEcom Account — refresh this form to see <code>supplier_pull_cursor_at</code> update. " +
                                "Created Suppliers + Map rows appear in the Supplier Map list as the worker pulls them.<br><br>" +
                                "RQ job: <code>{0}</code> (long queue, 3600s timeout)<br><br>" +
                                "<a href='/app/easyecom-supplier-map'>Open Supplier Map list →</a> · " +
                                "<a href='/app/error-log'>Open Error Log →</a>",
                            [frappe.utils.escape_html(result.job_id || "(no id)")]
                        ),
                        indicator: "blue",
                    });
                    return;
                }
                // Sync (inline=True) path
                const lines = [
                    __(
                        "Pages: {0} | Total: <b>{1}</b> | Created: {2} | Skipped (mapped): {3} | Disabled: {4} | Created-Flagged: {5} | FNC: {6} | Failed: {7}",
                        [
                            result.pages_walked,
                            result.total,
                            result.created,
                            result.skipped,
                            result.disabled,
                            result.created_flagged,
                            result.flagged_not_created,
                            result.failed,
                        ]
                    ),
                ];
                if (result.final_cursor_present) {
                    lines.push(
                        __(
                            "<b>Partial walk</b> — saved cursor preserved; click Discover Suppliers again to resume."
                        )
                    );
                }
                if ((result.failures_sample || []).length) {
                    lines.push(
                        "<br><b>Failure sample:</b><br>" +
                            result.failures_sample
                                .map(f =>
                                    frappe.utils.escape_html(
                                        `vendor_c_id=${f.ee_vendor_c_id} (${f.vendor_name || "?"}): ${f.error}`
                                    )
                                )
                                .join("<br>")
                    );
                }
                const overallOk =
                    result.failed === 0 && !result.final_cursor_present;
                frappe.msgprint({
                    title: __("Supplier Pull Result"),
                    message: lines.join("<br>"),
                    indicator: overallOk ? "green" : "orange",
                });
                frm.reload_doc();
            },
            error() {
                frappe.msgprint({
                    title: __("Discover Suppliers Failed"),
                    message: __(
                        "The pull call itself failed (network or permission)."
                    ),
                    indicator: "red",
                });
            },
        });
    },

    test_connection_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before testing the connection — credentials must be persisted (encrypted) before the test can read them back transiently.")) {
            return;
        }
        if (!frm.doc.default_location_key) {
            frappe.msgprint({
                title: __("Default Location Required"),
                message: __(
                    "Set Default Location (typically the primary location) before testing."
                ),
                indicator: "orange",
            });
            return;
        }

        frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });

        frappe.call({
            method: "ecommerce_super.easyecom.api.test_connection.test_connection",
            args: { account: frm.doc.name },
            freeze: true,
            freeze_message: __("Acquiring JWT…"),
            callback(r) {
                const result = r.message || {};
                if (result.ok) {
                    frappe.show_alert(
                        {
                            message: __("Connected — JWT acquired for {0}", [
                                result.location_key,
                            ]),
                            indicator: "green",
                        },
                        7
                    );
                    frm.reload_doc();
                } else {
                    frappe.msgprint({
                        title: __("Connection Failed"),
                        message: __("{0}{1}", [
                            result.message || __("Unknown error."),
                            result.error_code
                                ? `<br><br><small>Code: <code>${result.error_code}</code></small>`
                                : "",
                        ]),
                        indicator: "red",
                    });
                }
            },
            error() {
                frappe.msgprint({
                    title: __("Connection Failed"),
                    message: __(
                        "The Test Connection call itself failed (network, server, or permission)."
                    ),
                    indicator: "red",
                });
            },
        });
    },

    update_connection_indicator(frm) {
        const status = frm.doc.connection_status;
        const color =
            {
                Connected: "green",
                Degraded: "orange",
                Down: "red",
                Disabled: "grey",
            }[status] || "grey";
        frm.dashboard.set_headline_alert(
            `<span class="indicator ${color}">${__(status || "Unknown")}</span>`
        );
    },
});
