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
        ? __("Drift Detection (§8d Pull post-flip)")
        : __("Pulling /Products/GetProductMaster…");
    frappe.show_alert({message: verb, indicator: "blue"});

    frappe.call({
        method: "ecommerce_super.easyecom.flows.item_pull.discover_products",
        args: {account: frm.doc.name},
        freeze: true,
        freeze_message: verb,
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
            // Worklist deep-links.
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

frappe.ui.form.on("EasyEcom Account", {
    refresh(frm) {
        frm.trigger("update_connection_indicator");

        // Top-bar action groups. Skip on a new (unsaved) doc — the
        // _ensureSaved guards in the handlers also catch this, but
        // hiding the buttons before save keeps the UI honest.
        if (frm.is_new()) {
            return;
        }

        // Discover group — 8a Locations, 8b Channels, 8d Products.
        frm.add_custom_button(
            __("Locations"),
            () => _runLocationDiscovery(frm),
            __("Discover")
        );
        frm.add_custom_button(
            __("Channels"),
            () => _runChannelDiscovery(frm),
            __("Discover")
        );
        frm.add_custom_button(
            __("Products (§8d)"),
            () => _runProductDiscovery(frm),
            __("Discover")
        );

        // Push group — 8d batch sweep (Stage 6).
        frm.add_custom_button(
            __("Push All Pending Items"),
            () => _runPushAllPending(frm),
            __("Push")
        );
    },

    discover_products_action(frm) {
        _runProductDiscovery(frm);
    },

    push_all_pending_action(frm) {
        _runPushAllPending(frm);
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

    discover_customers_action(frm) {
        if (!_ensureSaved(frm, "Save the Account before discovering customers.")) {
            return;
        }
        frappe.show_alert({
            message: __("Pulling /Wholesale/v2/UserManagement…"),
            indicator: "blue",
        });
        frappe.call({
            method: "ecommerce_super.easyecom.api.customer_pull.discover_customers",
            freeze: true,
            freeze_message: __("Pulling wholesale customers…"),
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
