// EasyEcom Supplier Map — form-side behaviour.
//
// Stage 1 ships the set_query filter + (now in Stage 1) the drift /
// push button handlers. Mirrors §8e Customer Map exactly. The
// dismiss_drift / push_to_ee_for_drift flow methods land in Stage 5;
// the buttons are visible only when status='Drift' (depends_on in
// the JSON) so they're inert until Stage 5 lights them up.

frappe.ui.form.on("EasyEcom Supplier Map", {
    setup(frm) {
        frm.set_query("erpnext_doctype", function () {
            return {
                filters: {name: ["in", ["Supplier"]]},
            };
        });
    },
});
