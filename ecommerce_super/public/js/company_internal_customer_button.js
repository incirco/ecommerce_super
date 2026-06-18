// §10 onboarding helper — "Bootstrap Internal Customer" button on
// the Company form. One click on the target Company (the one that
// receives inventory in a stock-transfer pair), pick the source
// Company in the dialog, and the integration creates a Customer
// with everything §10 routing AND §8e CreateCustomer push need:
//
//   - is_internal_customer = 1, represents_company = current Company
//   - Allowed To Transact With containing the source Company
//   - placeholder email + mobile so §8e doesn't flag on contacts
//   - default_currency, gst_category, gstin mirrored from this Company
//   - Billing + Shipping Addresses mirrored from this Company's
//     primary Address (or a fallback if none exists)
//
// Idempotent: re-clicking for the same (source, target) pair returns
// the existing Customer name and only patches what's missing.

frappe.ui.form.on("Company", {
    refresh(frm) {
        if (frm.is_new()) return;

        frm.add_custom_button(
            __("Bootstrap Internal Customer"),
            () => _show_dialog(frm),
            __("EasyEcom"),
        );
    },
});

function _show_dialog(frm) {
    const d = new frappe.ui.Dialog({
        title: __("Bootstrap Internal Customer"),
        fields: [
            {
                fieldname: "intro",
                fieldtype: "HTML",
                options: `
                    <div class="text-muted small" style="margin-bottom: 10px;">
                        ${__("Creates an Internal Customer representing")}
                        <b>${frappe.utils.escape_html(frm.doc.name)}</b>
                        ${__("(target). The chosen source Company will be added to")}
                        <i>${__("Allowed To Transact With")}</i>.
                        ${__("Safe to re-run — only missing pieces are added.")}
                    </div>
                `,
            },
            {
                fieldname: "source_company",
                fieldtype: "Link",
                options: "Company",
                label: __("Source Company (sender)"),
                reqd: 1,
                get_query: () => ({
                    filters: {name: ["!=", frm.doc.name]},
                }),
            },
        ],
        primary_action_label: __("Bootstrap"),
        primary_action(values) {
            d.hide();
            frappe.show_alert({
                message: __("Bootstrapping Internal Customer…"),
                indicator: "blue",
            });
            frappe.call({
                method:
                    "ecommerce_super.easyecom.customer.internal_customer_bootstrap.bootstrap_internal_customer",
                args: {
                    source_company: values.source_company,
                    target_company: frm.doc.name,
                },
                freeze: true,
                freeze_message: __("Bootstrapping…"),
                callback(r) {
                    _render_result(r.message || {});
                },
            });
        },
    });
    d.show();
}

function _render_result(result) {
    const safe = s => frappe.utils.escape_html(String(s || "—"));
    const details = result.details || {};
    const addresses = (details.addresses || [])
        .map(
            a => `${safe(a.address_type)}: ${safe(a.state)}, ${safe(a.pincode)}, ${safe(a.country)}`,
        )
        .join("<br>") || "—";
    const lines = [
        __("Customer: <b>{0}</b>", [safe(result.customer_name)]),
        __("Created: <b>{0}</b>", [result.created ? __("yes") : __("no — already existed")]),
        __("Added Allowed-To-Transact-With row: <b>{0}</b>", [
            result.added_atw_row ? __("yes") : __("no — already present"),
        ]),
        __("Added Billing Address: <b>{0}</b>", [
            result.added_billing_address ? __("yes") : __("no — already present"),
        ]),
        __("Added Shipping Address: <b>{0}</b>", [
            result.added_shipping_address ? __("yes") : __("no — already present"),
        ]),
        "<br>",
        __("Represents Company: {0}", [safe(details.represents_company)]),
        __("Allowed To Transact With: {0}", [
            (details.allowed_to_transact_with || []).map(safe).join(", ") || "—",
        ]),
        __("Email: {0}", [safe(details.email_id)]),
        __("Mobile: {0}", [safe(details.mobile_no)]),
        __("Currency: {0}", [safe(details.default_currency)]),
        __("GST Category: {0}", [safe(details.gst_category)]),
        __("GSTIN: {0}", [safe(details.gstin)]),
        __("Addresses:<br>{0}", [addresses]),
    ];
    frappe.msgprint({
        title: __("Internal Customer Ready"),
        message: lines.join("<br>"),
        indicator: "green",
        primary_action: {
            label: __("Open Customer"),
            action() {
                frappe.set_route("Form", "Customer", result.customer_name);
            },
        },
    });
}
