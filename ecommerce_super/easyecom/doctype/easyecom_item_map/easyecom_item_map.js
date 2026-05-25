// EasyEcom Item Map — restrict the Dynamic Link's erpnext_doctype
// dropdown to Item / Product Bundle. The controller's validate is
// the defensive backstop; this is the UX filter.

frappe.ui.form.on("EasyEcom Item Map", {
    setup(frm) {
        frm.set_query("erpnext_doctype", function () {
            return {
                filters: {name: ["in", ["Item", "Product Bundle"]]},
            };
        });
    },
});
