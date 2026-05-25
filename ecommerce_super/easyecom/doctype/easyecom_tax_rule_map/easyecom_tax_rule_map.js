// Client-side form behaviour for EasyEcom Tax Rule Map.
//
// The taxes child table reuses ERPNext's native "Item Tax" DocType,
// whose item_tax_template Link field has no built-in Company filter.
// Without a set_query, the dropdown shows every template in the site
// — confusing when the parent doc is scoped to one Company (§8.5.3:
// "Holds ONLY this company's Item Tax Templates").
//
// This filter narrows the dropdown to the parent's Company, and to
// non-disabled templates only.

frappe.ui.form.on("EasyEcom Tax Rule Map", {
    setup(frm) {
        frm.set_query("item_tax_template", "taxes", function (doc) {
            return {
                filters: {
                    company: doc.company || "",
                    disabled: 0,
                },
            };
        });
    },

    company(frm) {
        // When the Company changes, any pre-existing rows reference
        // templates that may belong to the wrong Company. Warn the FDE
        // so they re-pick — the server-side validate() will also
        // refuse to save a row whose template's Company doesn't match.
        if (!frm.doc.company || !(frm.doc.taxes || []).length) {
            return;
        }
        frappe.show_alert(
            {
                message: __(
                    "Company changed — re-pick the Item Tax Templates for {0}; templates from the previous Company will be rejected on save.",
                    [frm.doc.company]
                ),
                indicator: "orange",
            },
            10
        );
    },
});
