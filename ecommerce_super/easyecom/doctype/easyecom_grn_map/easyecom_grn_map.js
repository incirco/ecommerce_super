// EasyEcom GRN Map — form view (§9 Stage 3 FDE actions).
//
// gh#230: "Re-check QC / Force Re-sweep" button for Held-Pre-QC rows.
// Backend entrypoint: flows.grn_pull.resweep_one_held_grn. Calls the
// same code path as the hourly cron (`resweep_held_pre_qc_grns`) but
// scoped to this single Map row, so an FDE debugging a stuck GRN can
// resolve it in-session instead of waiting up to 1h for the cron.

frappe.ui.form.on("EasyEcom GRN Map", {
    refresh(frm) {
        if (frm.is_new()) return;

        const status = frm.doc.status;

        // Only show the re-sweep button when it's actionable.
        if (status === "Held-Pre-QC") {
            frm.add_custom_button(
                __("Re-check QC / Force Re-sweep"),
                () => resweep_action(frm),
                __("Actions"),
            );
        }
    },
});

function resweep_action(frm) {
    frappe.dom.freeze(__("Re-fetching from EasyEcom..."));
    frappe.call({
        method:
            "ecommerce_super.easyecom.flows.grn_pull.resweep_one_held_grn",
        args: { grn_map_name: frm.doc.name },
        callback: (r) => {
            frappe.dom.unfreeze();
            const result = r.message || {};
            if (!result.ok) {
                frappe.msgprint({
                    title: __("Re-sweep failed"),
                    message: result.message || __("Unknown error"),
                    indicator: "red",
                });
                return;
            }
            const new_status = result.new_status;
            const indicator =
                new_status === "Receipted"
                    ? "green"
                    : new_status === "Held-Pre-QC"
                    ? "orange"
                    : "blue";
            frappe.show_alert({
                message: __(
                    "Re-sweep done: {0} → {1}. {2}",
                    [
                        result.old_status || "?",
                        new_status || "?",
                        result.message || "",
                    ],
                ),
                indicator,
            });
            frm.reload_doc();
        },
        error: () => {
            frappe.dom.unfreeze();
        },
    });
}
