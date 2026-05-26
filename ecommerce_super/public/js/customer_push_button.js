// §8e Stage 4 — "Push to EasyEcom" button on the Customer form.
// Only shown for customer_type=Company (§8e is wholesale only).

frappe.ui.form.on("Customer", {
    refresh(frm) {
        if (frm.is_new()) return;
        if (frm.doc.customer_type !== "Company") return;

        frm.add_custom_button(
            __("Push to EasyEcom"),
            () => {
                frappe.show_alert({
                    message: __("Pushing to EasyEcom…"),
                    indicator: "blue",
                });
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.customer_push.push_one_customer_now",
                    args: {customer_docname: frm.doc.name},
                    freeze: true,
                    freeze_message: __("Pushing…"),
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
                            __("EE customerId: <b>{0}</b>", [
                                result.ee_customer_id || "—",
                            ]),
                        ];
                        if ((result.flag_reasons || []).length) {
                            lines.push(
                                "<br><b>Flags:</b><br>" +
                                    result.flag_reasons
                                        .map(s => frappe.utils.escape_html(s))
                                        .join("<br>")
                            );
                        }
                        frappe.msgprint({
                            title: __("EasyEcom Push Result"),
                            message: lines.join("<br>"),
                            indicator:
                                result.pushed ? "green" : "orange",
                        });
                    },
                });
            },
            __("EasyEcom"),
        );
    },
});
