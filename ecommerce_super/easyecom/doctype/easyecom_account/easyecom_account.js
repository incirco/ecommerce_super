// Client-side form behaviour for EasyEcom Account.
//
// The Test Connection button calls the whitelisted test_connection method
// (ecommerce_super.easyecom.api.test_connection.test_connection) which
// forces a fresh /access/token call via EasyEcomClient.refresh_jwt() and
// reports inline. SPEC §3.11 bar 1.

frappe.ui.form.on("EasyEcom Account", {
    refresh(frm) {
        frm.trigger("update_connection_indicator");
    },

    test_connection_action(frm) {
        if (frm.is_new()) {
            frappe.msgprint({
                title: __("Save First"),
                message: __(
                    "Save the Account before testing the connection — credentials must be persisted (encrypted) before the test can read them back transiently."
                ),
                indicator: "orange",
            });
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
                    // Refresh the form so connection_status / last_successful_sync_at
                    // pick up the values written server-side.
                    frm.reload_doc();
                } else {
                    frappe.msgprint({
                        title: __("Connection Failed"),
                        message: __(
                            "{0}{1}",
                            [
                                result.message || __("Unknown error."),
                                result.error_code
                                    ? `<br><br><small>Code: <code>${result.error_code}</code></small>`
                                    : "",
                            ]
                        ),
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

    discover_locations_action(frm) {
        if (frm.is_new()) {
            frappe.msgprint({
                title: __("Save First"),
                message: __(
                    "Save the Account before running discovery — the EasyEcom client reads credentials and the default location from the persisted record."
                ),
                indicator: "orange",
            });
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

        frappe.show_alert({
            message: __("Pulling /getAllLocation…"),
            indicator: "blue",
        });

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
                    __(
                        "Total: {0} | New: {1} | Updated: {2} | Failed: {3}",
                        [
                            result.total,
                            result.new_count,
                            result.updated_count,
                            result.failed_count,
                        ]
                    ),
                ];
                if (result.new_count > 0) {
                    const tomap_url =
                        "/app/easyecom-location/view/list?workflow_state=To Map";
                    lines.push(
                        `<br><a href="${tomap_url}">${__(
                            "Open {0} new location(s) waiting to map →",
                            [result.new_count]
                        )}</a>`
                    );
                }
                if (result.failed_count > 0) {
                    lines.push(
                        `<br><br><b>${__("Failed rows:")}</b><br>` +
                            (result.failed_locations || [])
                                .map(
                                    (f) =>
                                        `<code>${frappe.utils.escape_html(
                                            f.location_key
                                        )}</code>: ${frappe.utils.escape_html(
                                            f.error
                                        )}`
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
                    message: __(
                        "The Discover Locations call itself failed (network, server, or permission)."
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
