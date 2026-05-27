// EasyEcom Item Map — form-side behaviour.
//
// - Restrict the Dynamic Link's erpnext_doctype dropdown to Item /
//   Product Bundle (the controller's validate is the defensive
//   backstop; this is the UX filter).
// - Drift resolution buttons (audit fix #7): Dismiss / Push ERPNext
//   → EE, only when status=Drift.

frappe.ui.form.on("EasyEcom Item Map", {
    setup(frm) {
        frm.set_query("erpnext_doctype", function () {
            return {
                filters: {name: ["in", ["Item", "Product Bundle"]]},
            };
        });
    },

    dismiss_drift_action(frm) {
        if (frm.doc.status !== "Drift") {
            frappe.msgprint({
                title: __("Not in Drift"),
                message: __(
                    "This row is in <b>{0}</b> status, not Drift; nothing to dismiss.",
                    [frm.doc.status]
                ),
                indicator: "grey",
            });
            return;
        }
        frappe.confirm(
            __(
                "<b>Dismiss this drift?</b><br><br>" +
                    "The EE-side change(s) listed in the Drift Fields table will be " +
                    "<b>ignored</b> — the underlying ERPNext doc is NOT touched. " +
                    "The map row returns to <b>Mapped</b> status.<br><br>" +
                    "If the divergence is intentional (e.g. you renamed item_name in " +
                    "ERPNext on purpose), add the field to <b>Excluded Fields</b> below " +
                    "instead — otherwise the next nightly pull will re-flag the same drift."
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.flows.item_pull.dismiss_drift",
                    args: {item_map_name: frm.doc.name},
                    freeze: true,
                    freeze_message: __("Dismissing drift…"),
                    callback(r) {
                        const result = r.message || {};
                        if (!result.ok) {
                            frappe.msgprint({
                                title: __("Dismiss Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                            return;
                        }
                        frappe.show_alert(
                            {
                                message: __("Drift dismissed; row is Mapped."),
                                indicator: "green",
                            },
                            7
                        );
                        frm.reload_doc();
                    },
                });
            }
        );
    },

    push_to_ee_action(frm) {
        if (frm.doc.status !== "Drift") {
            frappe.msgprint({
                title: __("Not in Drift"),
                message: __(
                    "This row is in <b>{0}</b> status, not Drift; use the Item form's " +
                        "Push button for non-drift pushes.",
                    [frm.doc.status]
                ),
                indicator: "grey",
            });
            return;
        }
        if (!frm.doc.erpnext_name) {
            frappe.msgprint({
                title: __("No Linked Item"),
                message: __(
                    "This map row has no linked ERPNext doc; nothing to push."
                ),
                indicator: "orange",
            });
            return;
        }
        frappe.confirm(
            __(
                "<b>Push ERPNext → EE?</b><br><br>" +
                    "Re-assert ERPNext as SoT by pushing <code>{0}</code> to EasyEcom " +
                    "via the §8d push (overwriting the EE-side divergence). " +
                    "Bundle wrappers dispatch to combo push (<code>itemType=1</code>). " +
                    "Real EE write.",
                [frappe.utils.escape_html(frm.doc.erpnext_name)]
            ),
            () => {
                frappe.show_alert({
                    message: __("Pushing to EasyEcom…"),
                    indicator: "blue",
                });
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.flows.item_push.push_one_product",
                    args: {item_code: frm.doc.erpnext_name},
                    freeze: true,
                    freeze_message: __("Pushing {0}…", [frm.doc.erpnext_name]),
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
                        const lines = [
                            __("Operation: <b>{0}</b>", [result.operation]),
                        ];
                        if ((result.flag_reasons || []).length) {
                            lines.push(
                                `<br><br><b>Flags:</b><br>` +
                                    result.flag_reasons
                                        .map(r => frappe.utils.escape_html(r))
                                        .join("<br>")
                            );
                        }
                        frappe.msgprint({
                            title: __("Push Result"),
                            message: lines.join(""),
                            indicator:
                                result.operation === "create" ||
                                result.operation === "update"
                                    ? "green"
                                    : "orange",
                        });
                        frm.reload_doc();
                    },
                });
            }
        );
    },

    re_evaluate_from_ee_action(frm) {
        if (frm.doc.status !== "Created-Flagged") {
            frappe.msgprint({
                title: __("Not in Created-Flagged"),
                message: __(
                    "Re-evaluate only applies to Created-Flagged rows. " +
                        "This row is in <b>{0}</b>.",
                    [frm.doc.status]
                ),
                indicator: "grey",
            });
            return;
        }
        frappe.show_alert({
            message: __("Walking GetProductMaster for SKU {0}…", [
                frm.doc.ee_sku,
            ]),
            indicator: "blue",
        });
        frappe.call({
            method:
                "ecommerce_super.easyecom.flows.item_pull.re_evaluate_one_product",
            args: {item_map_name: frm.doc.name},
            freeze: true,
            freeze_message: __("Re-evaluating {0} from EE…", [frm.doc.ee_sku]),
            callback(r) {
                const result = r.message || {};
                if (!result.ok) {
                    frappe.msgprint({
                        title: __("Re-evaluate Failed"),
                        message: result.message || __("Unknown error."),
                        indicator: "red",
                    });
                    return;
                }
                const lines = [
                    __("Status: <b>{0}</b>", [result.status]),
                ];
                if ((result.flag_reasons || []).length) {
                    lines.push(
                        "<br><br><b>Flags still set:</b><br>" +
                            result.flag_reasons
                                .map(s => frappe.utils.escape_html(s))
                                .join("<br>")
                    );
                } else if (result.status === "Mapped") {
                    lines.push(
                        "<br><i>All flags cleared. Row is now Mapped.</i>"
                    );
                }
                frappe.msgprint({
                    title: __("Re-evaluate Complete"),
                    message: lines.join(""),
                    indicator: result.status === "Mapped" ? "green" : "orange",
                });
                frm.reload_doc();
            },
        });
    },

    mark_mapped_override_action(frm) {
        if (frm.doc.status !== "Created-Flagged") {
            frappe.msgprint({
                title: __("Not in Created-Flagged"),
                message: __(
                    "This row is in <b>{0}</b> status. Override only " +
                        "applies to Created-Flagged rows.",
                    [frm.doc.status]
                ),
                indicator: "grey",
            });
            return;
        }
        const flagText = frm.doc.flag_reason || "(no flag_reason recorded)";
        frappe.confirm(
            __(
                "<b>Mark this row Mapped without fixing the flag(s)?</b><br><br>" +
                    "Suppressing this flag_reason:<br>" +
                    "<code style='display:block;padding:8px;background:#fef3c7;border-radius:4px;margin:8px 0;'>{0}</code>" +
                    "<b>Important — this is a one-pull-cycle ack, not a permanent mute.</b><br>" +
                    "The next Discover Products run will re-evaluate from scratch. " +
                    "If the same flag fires again, the row flips back to Created-Flagged. " +
                    "To suppress permanently, fix the source data.<br><br>" +
                    "Each override is recorded as a Comment on this row for audit. " +
                    "Continue?",
                [frappe.utils.escape_html(flagText)]
            ),
            () => {
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.flows.item_pull.mark_mapped_override",
                    args: {item_map_name: frm.doc.name},
                    freeze: true,
                    freeze_message: __("Applying override…"),
                    callback(r) {
                        const result = r.message || {};
                        if (!result.ok) {
                            frappe.msgprint({
                                title: __("Override Failed"),
                                message: result.message || __("Unknown error."),
                                indicator: "red",
                            });
                            return;
                        }
                        frappe.show_alert(
                            {
                                message: __(
                                    "Marked Mapped. Audit Comment added. " +
                                        "Next pull may re-flag if source isn't fixed."
                                ),
                                indicator: "green",
                            },
                            7
                        );
                        frm.reload_doc();
                    },
                });
            }
        );
    },
});
