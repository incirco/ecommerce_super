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
});
