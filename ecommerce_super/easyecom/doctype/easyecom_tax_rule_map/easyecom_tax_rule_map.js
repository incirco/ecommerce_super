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

function _renderTestResolveResult(result) {
    if (!result || !result.ok) {
        return (
            '<div class="indicator red" style="margin: 8px 0;">' +
            frappe.utils.escape_html(
                (result && result.message) || __("Resolve call failed.")
            ) +
            "</div>"
        );
    }

    const lines = [];
    lines.push(
        '<div style="margin: 8px 0;"><b>' +
            __("Map") +
            ":</b> " +
            frappe.utils.escape_html(result.map_name) +
            " &middot; <b>" +
            __("Rule") +
            ":</b> <code>" +
            frappe.utils.escape_html(result.tax_rule_name) +
            "</code> &middot; <b>" +
            __("Company") +
            ":</b> " +
            frappe.utils.escape_html(result.company) +
            " &middot; <b>" +
            __("State") +
            ":</b> " +
            frappe.utils.escape_html(result.workflow_state || "") +
            "</div>"
    );

    // Rows-to-stamp table.
    if (result.stamped_count > 0) {
        lines.push(
            "<div style='margin: 12px 0 6px;'><b>" +
                __("Would stamp {0} row(s) onto the item:", [result.stamped_count]) +
                "</b></div>"
        );
        let table =
            "<table class='table table-bordered' style='font-size: 12px;'>" +
            "<thead><tr>" +
            "<th>" + __("Item Tax Template") + "</th>" +
            "<th>" + __("Effective rate") + "</th>" +
            "<th>" + __("Tax Category") + "</th>" +
            "<th>" + __("Min Net Rate") + "</th>" +
            "<th>" + __("Max Net Rate") + "</th>" +
            "</tr></thead><tbody>";
        for (const row of result.rows_to_stamp) {
            const eff =
                row.effective_rate !== null && row.effective_rate !== undefined
                    ? (row.effective_rate * 100).toFixed(2) + "%"
                    : "<i>unknown</i>";
            table +=
                "<tr>" +
                "<td><code>" + frappe.utils.escape_html(row.item_tax_template || "") + "</code></td>" +
                "<td>" + eff + "</td>" +
                "<td>" + frappe.utils.escape_html(row.tax_category || "") + "</td>" +
                "<td>" + (row.minimum_net_rate || "") + "</td>" +
                "<td>" + (row.maximum_net_rate || "") + "</td>" +
                "</tr>";
        }
        table += "</tbody></table>";
        lines.push(table);
    } else {
        lines.push(
            "<div class='indicator orange' style='margin: 12px 0;'>" +
                __("0 rows would be stamped — the map is empty.") +
                (result.empty_reason
                    ? "<br><small>" +
                      frappe.utils.escape_html(result.empty_reason) +
                      "</small>"
                    : "") +
                "</div>"
        );
    }

    // Reconciliation verdict.
    if (result.sample_tax_rate === null) {
        lines.push(
            "<div class='indicator grey' style='margin: 8px 0;'>" +
                __("No sample tax_rate provided — reconciliation skipped.") +
                "</div>"
        );
    } else if (result.reconciled) {
        lines.push(
            "<div class='indicator green' style='margin: 8px 0;'>" +
                __("Reconciled — sample tax_rate {0} matches a mapped template rate.", [
                    (result.sample_tax_rate * 100).toFixed(2) + "%",
                ]) +
                "</div>"
        );
    } else {
        lines.push(
            "<div class='indicator red' style='margin: 8px 0;'><b>" +
                __("Discrepancy") +
                ":</b><ul style='margin: 4px 0 0 16px;'>" +
                (result.discrepancies || [])
                    .map((d) => "<li>" + frappe.utils.escape_html(d) + "</li>")
                    .join("") +
                "</ul></div>"
        );
    }

    // Cess.
    lines.push(
        "<div style='margin: 8px 0;'><b>" +
            __("Cess that would be applied to item.ecs_cess:") +
            "</b> " +
            frappe.format(result.cess || 0, {fieldtype: "Currency"}) +
            "</div>"
    );

    return lines.join("");
}

function _openTestResolveDialog(frm) {
    const d = new frappe.ui.Dialog({
        title: __("Test Resolve — {0}", [frm.doc.name]),
        size: "large",
        fields: [
            {
                fieldname: "intro",
                fieldtype: "HTML",
                options:
                    "<div style='margin-bottom: 8px;'>" +
                    __(
                        "Dry-run the resolver against a sample tax_rate (and optional cess). Nothing persists — no item is stamped, no map is auto-created. Uses the same preview + reconciliation code the production §8d Item sync will run, so the verdict here matches what a real sync would produce."
                    ) +
                    "</div>",
            },
            {
                fieldname: "sample_tax_rate",
                fieldtype: "Float",
                label: __("Sample tax_rate"),
                description: __(
                    "EasyEcom's resolved decimal rate, e.g. 0.18 for 18%. Leave blank to skip reconciliation (preview only)."
                ),
                precision: 6,
            },
            {
                fieldname: "sample_cess",
                fieldtype: "Currency",
                label: __("Sample cess (optional)"),
                description: __(
                    "EasyEcom per-product cess. Pass-through — does not affect reconciliation."
                ),
                default: 0,
            },
            {fieldname: "results_section", fieldtype: "Section Break", label: __("Result")},
            {fieldname: "results", fieldtype: "HTML"},
        ],
        primary_action_label: __("Resolve"),
        primary_action(values) {
            frappe.call({
                method:
                    "ecommerce_super.easyecom.doctype.easyecom_tax_rule_map.easyecom_tax_rule_map.test_resolve",
                args: {
                    map_name: frm.doc.name,
                    sample_tax_rate: values.sample_tax_rate,
                    sample_cess: values.sample_cess,
                },
                freeze: true,
                freeze_message: __("Resolving…"),
                callback(r) {
                    d.fields_dict.results.$wrapper.html(
                        _renderTestResolveResult(r.message)
                    );
                },
                error() {
                    d.fields_dict.results.$wrapper.html(
                        '<div class="indicator red">' +
                            __("Resolve call failed (network or permission).") +
                            "</div>"
                    );
                },
            });
        },
    });
    d.show();
}

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

    refresh(frm) {
        if (frm.is_new()) {
            return;
        }
        frm.add_custom_button(__("Test Resolve"), () => _openTestResolveDialog(frm));
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
