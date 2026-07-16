// gh#149 — Dry-Run diagnostic buttons on the B2B Order Map form.
//
// Calls the whitelisted dry_run_einvoice / dry_run_ewaybill endpoints
// and renders the returned checklist inline in a msgprint dialog.
// Read-only; no side effects. Useful for support-triage: FDE clicks,
// sees exactly which step of /einvoice/update would fail if EE fired
// against this SO right now.

frappe.ui.form.on("EasyEcom B2B Order Map", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }
        if (!frm.doc.sales_order) {
            return;  // dry-run + lifecycle both need a source SO
        }
        frm.add_custom_button(
            __("Dry-Run /einvoice/update"),
            () => _runDryRun(frm, "dry_run_einvoice"),
            __("Diagnostics")
        );
        frm.add_custom_button(
            __("Dry-Run /ewaybill/update"),
            () => _runDryRun(frm, "dry_run_ewaybill"),
            __("Diagnostics")
        );
        // Re-fire (real, not dry-run) — MTTR relief for post-code-fix
        // remediation. Fetches fresh EE data, runs the same handler
        // chain the /einvoice/update endpoint uses. Idempotent — safe
        // to click multiple times.
        frm.add_custom_button(
            __("Re-fire /einvoice/update"),
            () => _confirmAndRefire(frm),
            __("Diagnostics")
        );
        // Render the "one place, whole story" lifecycle view.
        _renderLifecycle(frm);
    },
});

function _confirmAndRefire(frm) {
    frappe.confirm(
        __(
            "Re-fire will actually create/update the Sales Invoice + mint " +
            "IRN (if the toggle is on). This is NOT a dry-run.<br><br>" +
            "Use this when you've fixed the underlying issue that caused " +
            "the last /einvoice/update to fail. Idempotent — safe to run " +
            "even if unsure.<br><br>" +
            "Proceed?"
        ),
        () => _runRefire(frm),
    );
}

function _runRefire(frm) {
    frappe.call({
        method: "ecommerce_super.easyecom.api.gsp_refire.refire_einvoice",
        args: { map_name: frm.doc.name },
        freeze: true,
        freeze_message: __("Re-firing /einvoice/update…"),
        callback(r) {
            const result = r.message || {};
            const indicator = result.ok ? "green" : "red";
            const lines = [
                __("<b>Outcome:</b> {0}", [
                    frappe.utils.escape_html(result.message || "no message"),
                ]),
            ];
            if (result.sales_invoice) {
                const url = `/app/sales-invoice/${encodeURIComponent(result.sales_invoice)}`;
                lines.push(
                    __("<b>Sales Invoice:</b> <a href='{0}' target='_blank'>{1}</a>", [
                        url, frappe.utils.escape_html(result.sales_invoice),
                    ])
                );
            }
            if (result.irn) {
                lines.push(
                    __("<b>IRN:</b> {0}", [frappe.utils.escape_html(result.irn)])
                );
            }
            lines.push(
                "<br><small>A Comment with this attempt + outcome has been added to the Map timeline.</small>"
            );
            frappe.msgprint({
                title: __("Re-fire /einvoice/update"),
                message: lines.join("<br>"),
                indicator,
            });
            frm.reload_doc();  // pull in the new Comment
        },
    });
}

function _runDryRun(frm, method_name) {
    frappe.call({
        method: `ecommerce_super.easyecom.api.gsp_dry_run.${method_name}`,
        args: { reference_code: frm.doc.sales_order },
        freeze: true,
        freeze_message: __("Running dry-run…"),
        callback(r) {
            const result = r.message || {};
            _renderDryRunResult(result, method_name);
        },
    });
}

function _renderDryRunResult(result, method_name) {
    const checks = result.checks || [];
    const lines = [];
    lines.push(
        __("<b>SO:</b> {0} — overall: {1}", [
            frappe.utils.escape_html(result.reference_code || "(none)"),
            result.ok ? "✓ PASS" : "✗ FAIL",
        ])
    );
    lines.push("<br>");
    lines.push("<table style='width:100%;border-collapse:collapse;'>");
    lines.push(
        "<tr><th style='text-align:left;padding:4px;'>Step</th>" +
        "<th style='text-align:left;padding:4px;'>Result</th>" +
        "<th style='text-align:left;padding:4px;'>Detail</th></tr>"
    );
    for (const c of checks) {
        const status = c.ok
            ? "<span style='color:green;'>✓</span>"
            : "<span style='color:red;'>✗</span>";
        const detail = c.ok
            ? _formatOkDetail(c)
            : frappe.utils.escape_html(c.reason || "");
        lines.push(
            `<tr>
                <td style='padding:4px;vertical-align:top;'>${frappe.utils.escape_html(c.step)}</td>
                <td style='padding:4px;vertical-align:top;'>${status}</td>
                <td style='padding:4px;vertical-align:top;'>${detail}</td>
            </tr>`
        );
    }
    lines.push("</table>");
    frappe.msgprint({
        title: __("Dry-Run: {0}", [method_name]),
        message: lines.join(""),
        indicator: result.ok ? "green" : "red",
        wide: true,
    });
}

// Lifecycle view — quick foundation for the #150 dashboard. Renders a
// compact stage table above the field grid so an FDE can see the whole
// story of one B2B SO (SO → Push → EE ID → SI → IRN/Eway → API Calls)
// at a glance instead of clicking across 4-6 pages.
function _renderLifecycle(frm) {
    frappe.call({
        method: "ecommerce_super.easyecom.doctype.easyecom_b2b_order_map.easyecom_b2b_order_map.get_lifecycle",
        args: { map_name: frm.doc.name },
        callback(r) {
            const stages = r.message || [];
            if (!stages.length) return;
            const html = _lifecycleHtml(stages);
            frm.dashboard.add_section(html, __("Lifecycle"));
        },
    });
}

function _lifecycleHtml(stages) {
    const rows = stages.map((s) => {
        const status = s.ok
            ? "<span style='color:green;font-weight:bold;'>✓</span>"
            : "<span style='color:#c00;font-weight:bold;'>—</span>";
        const stage = frappe.utils.escape_html(s.stage);
        const when = s.timestamp
            ? `<small style='color:#666;'>${frappe.datetime.prettyDate(s.timestamp)}</small>`
            : "<small style='color:#999;'>—</small>";
        let detail = frappe.utils.escape_html(s.detail || "");
        if (s.link_doctype && s.link_name) {
            const url = `/app/${frappe.router.slug(s.link_doctype)}/${encodeURIComponent(s.link_name)}`;
            detail += ` &nbsp; <a href="${url}" target="_blank">${frappe.utils.escape_html(s.link_name)}</a>`;
        }
        return `
            <tr>
                <td style='padding:6px;vertical-align:top;width:32px;text-align:center;'>${status}</td>
                <td style='padding:6px;vertical-align:top;width:210px;font-weight:500;'>${stage}</td>
                <td style='padding:6px;vertical-align:top;width:120px;'>${when}</td>
                <td style='padding:6px;vertical-align:top;'>${detail}</td>
            </tr>
        `;
    }).join("");
    return `
        <div style='margin-bottom:12px;'>
            <table style='width:100%;border-collapse:collapse;font-size:13px;'>
                <thead>
                    <tr style='border-bottom:1px solid #eee;color:#666;'>
                        <th style='padding:4px;text-align:center;'></th>
                        <th style='padding:4px;text-align:left;'>Stage</th>
                        <th style='padding:4px;text-align:left;'>When</th>
                        <th style='padding:4px;text-align:left;'>Detail</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

function _formatOkDetail(check) {
    // Show helpful details for each successful step: notes, resolved
    // IDs, counts, etc. Strip the boilerplate {step, ok}.
    const detail = { ...check };
    delete detail.step;
    delete detail.ok;
    if (Object.keys(detail).length === 0) {
        return "<em>ok</em>";
    }
    // If there's a note, prefer that for readability
    if (detail.note) {
        return frappe.utils.escape_html(detail.note);
    }
    const parts = [];
    for (const [k, v] of Object.entries(detail)) {
        parts.push(
            `<code>${frappe.utils.escape_html(k)}</code>: ${
                frappe.utils.escape_html(String(v))
            }`
        );
    }
    return parts.join("<br>");
}
