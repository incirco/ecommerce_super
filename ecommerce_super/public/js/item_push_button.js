// §8d Stage 6 — "Push to EasyEcom" button on the ERPNext Item form.
//
// Adds a top-bar button that calls push_one_product for the current
// Item. push_one_product auto-dispatches to the bundle path when the
// Item is a Product Bundle wrapper — same button covers both.
//
// Also adds a "Sync Lifecycle to EasyEcom" button when the Item is
// disabled (or was previously disabled) — sends ActivateDeactivateProduct
// without touching content. Useful for re-toggling without bumping
// other fields.
//
// Both buttons appear only when an enabled EasyEcom Account exists
// AND the Item has an EasyEcom Item Map row (or is eligible — has
// HSN, is a stock item OR a bundle wrapper).

frappe.ui.form.on("Item", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }
        // Only attach for items that could plausibly be pushed.
        // Cheap client-side gate; the server-side whitelist re-checks
        // everything including role.
        const eligible = frm.doc.gst_hsn_code && !frm.doc.has_variants;
        if (!eligible) {
            return;
        }

        frm.add_custom_button(
            __("Push to EasyEcom"),
            () => _pushItemToEasyEcom(frm),
            __("EasyEcom")
        );
        frm.add_custom_button(
            __("Sync Lifecycle to EasyEcom"),
            () => _pushItemLifecycleToEasyEcom(frm),
            __("EasyEcom")
        );
    },
});

function _pushItemToEasyEcom(frm) {
    frappe.confirm(
        __(
            "<b>Push <code>{0}</code> to EasyEcom?</b><br><br>" +
                "If the Item is already mapped → Update. Otherwise → Create. " +
                "Bundle wrappers dispatch to combo push (itemType=1 with subProducts). " +
                "Failing-mandatory items (missing dimensions, unresolvable TaxRate, " +
                "unpushed bundle component) → flagged on the map row, no broken " +
                "payload sent to EE.",
            [frappe.utils.escape_html(frm.doc.item_code)]
        ),
        () => {
            frappe.show_alert({
                message: __("Pushing to EasyEcom…"),
                indicator: "blue",
            });
            frappe.call({
                method: "ecommerce_super.easyecom.flows.item_push.push_one_product",
                args: {item_code: frm.doc.item_code},
                freeze: true,
                freeze_message: __("Pushing {0}…", [frm.doc.item_code]),
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
                        __(
                            "Operation: <b>{0}</b>{1}",
                            [
                                result.operation,
                                result.ee_product_id
                                    ? ` &middot; ee_product_id=<code>${frappe.utils.escape_html(result.ee_product_id)}</code>`
                                    : "",
                            ]
                        ),
                    ];
                    if ((result.flag_reasons || []).length) {
                        lines.push(
                            `<br><br><b>Flag reasons:</b><br>` +
                            result.flag_reasons.map(r => frappe.utils.escape_html(r)).join("<br>")
                        );
                    }
                    const indicator =
                        result.operation === "create" || result.operation === "update"
                            ? "green"
                            : "orange";
                    frappe.msgprint({
                        title: __("Push Result"),
                        message: lines.join(""),
                        indicator,
                    });
                },
                error() {
                    frappe.msgprint({
                        title: __("Push Failed"),
                        message: __("The push call itself failed (network or permission)."),
                        indicator: "red",
                    });
                },
            });
        }
    );
}

function _pushItemLifecycleToEasyEcom(frm) {
    const targetStatus = frm.doc.disabled ? 0 : 1;
    frappe.confirm(
        __(
            "<b>Send lifecycle to EasyEcom?</b><br><br>" +
                "Calls <code>ActivateDeactivateProduct</code> with " +
                "status=<b>{0}</b> ({1}). No-op if the Item has never been " +
                "pushed (no ee_product_id).",
            [targetStatus, targetStatus === 0 ? __("deactivate") : __("activate")]
        ),
        () => {
            frappe.show_alert({
                message: __("Sending lifecycle to EE…"),
                indicator: "blue",
            });
            frappe.call({
                method: "ecommerce_super.easyecom.flows.item_push.push_lifecycle_product",
                args: {item_code: frm.doc.item_code},
                freeze: true,
                freeze_message: __("Calling ActivateDeactivateProduct…"),
                callback(r) {
                    const result = r.message || {};
                    if (!result.ok) {
                        frappe.msgprint({
                            title: __("Lifecycle Sync Failed"),
                            message: result.message || __("Unknown error."),
                            indicator: "red",
                        });
                        return;
                    }
                    const msg = result.pushed
                        ? __("Lifecycle synced (operation: {0})", [result.operation])
                        : __("No-op: {0}", [(result.flag_reasons || []).join("; ")]);
                    frappe.show_alert(
                        {message: msg, indicator: result.pushed ? "green" : "grey"},
                        7
                    );
                },
            });
        }
    );
}
