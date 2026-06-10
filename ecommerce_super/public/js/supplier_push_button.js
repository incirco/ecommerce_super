// §8f Stage 4 — "Push to EasyEcom" button on the Supplier form (gh#36).
// Mirrors the Customer form's button (customer_push_button.js); only
// shown for supplier_type=Company because §8f is wholesale only —
// individual / proprietorship Suppliers aren't in scope for the
// /wms/CreateVendor + /wms/UpdateVendor wires this endpoint drives.

frappe.ui.form.on("Supplier", {
    refresh(frm) {
        if (frm.is_new()) return;
        if (frm.doc.supplier_type !== "Company") return;
        if (frm.doc.disabled) return;

        frm.add_custom_button(
            __("Push to EasyEcom"),
            () => {
                frappe.show_alert({
                    message: __("Pushing to EasyEcom…"),
                    indicator: "blue",
                });
                frappe.call({
                    method:
                        "ecommerce_super.easyecom.api.supplier_push.push_one_supplier_now",
                    args: {supplier_docname: frm.doc.name},
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
                            __("EE vendor_id (write key): <b>{0}</b>", [
                                result.ee_vendor_id || "—",
                            ]),
                            __("EE vendor_c_id (read key): <b>{0}</b>", [
                                result.ee_vendor_c_id || "—",
                            ]),
                        ];
                        if ((result.flag_reasons || []).length) {
                            lines.push(
                                "<br><b>Flags:</b><br>" +
                                    result.flag_reasons
                                        .map((s) => frappe.utils.escape_html(s))
                                        .join("<br>")
                            );
                        }
                        frappe.msgprint({
                            title: __("EasyEcom Push Result"),
                            message: lines.join("<br>"),
                            indicator: result.pushed ? "green" : "orange",
                        });
                    },
                });
            },
            __("EasyEcom"),
        );
    },
});
