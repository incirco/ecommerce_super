// EasyEcom Customer Map — form-side behaviour.
//
// Stage 1 (substrate) only ships the set_query filter; drift/push
// handlers (dismiss_drift_action, push_to_ee_action) land in Stage 5/6
// along with their backend flow methods. The button fields are in the
// JSON so the schema is complete; depends_on hides them until status
// becomes "Drift" (only Stage 5 sets that).

frappe.ui.form.on("EasyEcom Customer Map", {
    setup(frm) {
        frm.set_query("erpnext_doctype", function () {
            return {
                filters: {name: ["in", ["Customer"]]},
            };
        });
    },
});
