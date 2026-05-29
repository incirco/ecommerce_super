// EasyEcom Transfer Map form — §10 Stage 4. Surface multi-GRN
// receipt accumulation + quick links to the downstream financial
// documents (draft Debit Note, Internal Purchase Invoice). All
// read-only display polish — no new behaviour, no new schema fields.

frappe.ui.form.on("EasyEcom Transfer Map", {
    refresh(frm) {
        _addDownstreamButtons(frm);
        _renderCumulativeReceiptSummary(frm);
    },
});

function _addDownstreamButtons(frm) {
    if (frm.doc.draft_debit_note) {
        frm.add_custom_button(
            __("View Draft Debit Note"),
            () => {
                frappe.set_route(
                    "Form",
                    "Purchase Invoice",
                    frm.doc.draft_debit_note,
                );
            },
            __("Open"),
        );
    }
    if (frm.doc.internal_purchase_invoice) {
        frm.add_custom_button(
            __("View Internal Purchase Invoice"),
            () => {
                frappe.set_route(
                    "Form",
                    "Purchase Invoice",
                    frm.doc.internal_purchase_invoice,
                );
            },
            __("Open"),
        );
    }
    if (frm.doc.sales_invoice) {
        frm.add_custom_button(
            __("View Sales Invoice"),
            () => {
                frappe.set_route(
                    "Form",
                    "Sales Invoice",
                    frm.doc.sales_invoice,
                );
            },
            __("Open"),
        );
    }
}

function _renderCumulativeReceiptSummary(frm) {
    // Read-only dashboard chip showing per-Item dispatched vs
    // cumulative-received + open gap. Hits a server-side method so
    // the math stays server-authoritative (the same _cumulative_received
    // helper inbound uses for the IPI/DN gap math).
    if (frm.is_new()) return;
    if (!frm.doc.delivery_note) return;
    frappe.call({
        method:
            "ecommerce_super.easyecom.doctype.easyecom_transfer_map.easyecom_transfer_map.get_cumulative_receipt_summary",
        args: { transfer_map: frm.doc.name },
        callback(r) {
            if (!r.message || !r.message.rows) return;
            const rows = r.message.rows;
            if (!rows.length) return;
            // Render as a Frappe dashboard card with one line per Item.
            const html = `
                <div class="form-dashboard-section">
                    <div class="section-head">${__(
                        "§10 Cumulative Receipt",
                    )}</div>
                    <div class="row" style="margin: 10px 0;">
                        <div class="col-sm-12">
                            <table class="table table-bordered" style="margin: 0;">
                                <thead>
                                    <tr>
                                        <th>${__("Item")}</th>
                                        <th class="text-right">${__("Dispatched")}</th>
                                        <th class="text-right">${__("Received")}</th>
                                        <th class="text-right">${__("Open Gap")}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                ${rows
                                    .map((row) => {
                                        const gap = row.dispatched - row.received;
                                        const colour =
                                            gap > 0 ? "text-danger" : "text-success";
                                        return `
                                            <tr>
                                                <td>${frappe.utils.escape_html(row.item_code)}</td>
                                                <td class="text-right">${row.dispatched}</td>
                                                <td class="text-right">${row.received}</td>
                                                <td class="text-right ${colour}">
                                                    ${gap > 0 ? gap : 0}
                                                </td>
                                            </tr>
                                        `;
                                    })
                                    .join("")}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            `;
            frm.dashboard.add_section(html);
        },
    });
}
