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
        // gh#37: diagnostic for "I updated this Item but EE didn't see
        // it" — walks every gate (auto-push toggle, master mode,
        // has_variants, disabled) AND surfaces the existing Map row,
        // recent Sync Records, Queue Jobs, and API Calls. Read-only —
        // does NOT re-fire any sync.
        frm.add_custom_button(
            __("Trace Sync (§8d)"),
            () => _traceItemSync(frm),
            __("EasyEcom")
        );
    },
});

function _traceItemSync(frm) {
    frappe.show_alert({
        message: __("Tracing §8d Item sync…"),
        indicator: "blue",
    });
    frappe.call({
        method: "ecommerce_super.easyecom.api.item_sync_diagnostic.trace_item",
        args: {item_code: frm.doc.name},
        freeze: true,
        freeze_message: __("Tracing…"),
        callback(r) {
            const t = r.message || {};
            const push_gates = t.push_gates || [];
            const ps = t.pull_state || {};
            const dn = t.downstream || {};

            const gate_rows = push_gates
                .map((g) => {
                    const icon = g.passed ? "✓" : "✗";
                    const color = g.passed ? "#16a34a" : "#dc2626";
                    return (
                        `<tr><td style="color:${color};width:24px;">${icon}</td>` +
                        `<td><code>${frappe.utils.escape_html(g.gate)}</code></td>` +
                        `<td>${frappe.utils.escape_html(g.detail || "")}</td></tr>`
                    );
                })
                .join("");

            const pull_block =
                `<b>Pull (EE → ERPNext):</b> ${frappe.utils.escape_html(ps.detail || "—")}` +
                (ps.last_pull_at
                    ? `<br><i>last item_pull_cursor_at:</i> ${frappe.utils.escape_html(String(ps.last_pull_at))}`
                    : "");

            const im = dn.item_map;
            const im_block = im
                ? `<b>Item Map:</b> <code>${frappe.utils.escape_html(im.name)}</code> ` +
                  `· status=<code>${frappe.utils.escape_html(im.status || "—")}</code> ` +
                  `· ee_product_id=<code>${frappe.utils.escape_html(im.ee_product_id || "—")}</code> ` +
                  `· ee_cp_id=<code>${frappe.utils.escape_html(im.ee_cp_id || "—")}</code>` +
                  (im.flag_reason
                      ? `<br>&nbsp;&nbsp;<i>flag_reason:</i> ${frappe.utils.escape_html(im.flag_reason)}`
                      : "")
                : "<b>Item Map:</b> <i>none — Item never reached EE</i>";

            const sr = dn.sync_records || [];
            const sr_block = sr.length
                ? `<b>Sync Records (${sr.length}):</b><br>` +
                  sr
                      .map(
                          (s) =>
                              `&nbsp;&nbsp;<code>${frappe.utils.escape_html(s.name)}</code> ` +
                              `[${frappe.utils.escape_html(s.status)} / ${frappe.utils.escape_html(s.direction)}] ` +
                              (s.last_error
                                  ? frappe.utils.escape_html(s.last_error.slice(0, 120))
                                  : "—")
                      )
                      .join("<br>")
                : "<b>Sync Records:</b> <i>none</i>";

            const qj = dn.queue_jobs || [];
            const qj_block = qj.length
                ? `<br><b>Queue Jobs (${qj.length}):</b><br>` +
                  qj
                      .map(
                          (j) =>
                              `&nbsp;&nbsp;<code>${frappe.utils.escape_html(j.name)}</code> ` +
                              `[${frappe.utils.escape_html(j.state)}] attempts=${j.attempts || 0}`
                      )
                      .join("<br>")
                : "<br><b>Queue Jobs:</b> <i>none</i>";

            const ac = dn.api_calls || [];
            const ac_block = ac.length
                ? `<br><b>API Calls (${ac.length}):</b><br>` +
                  ac
                      .map(
                          (a) =>
                              `&nbsp;&nbsp;<code>${frappe.utils.escape_html(a.endpoint)}</code> ` +
                              `[${frappe.utils.escape_html(a.status)} ${a.response_status_code || ""}]`
                      )
                      .join("<br>")
                : "<br><b>API Calls:</b> <i>none</i>";

            const body =
                `<b>Verdict:</b> ${frappe.utils.escape_html(t.verdict || "—")}<br><br>` +
                `<b>Push (ERPNext → EE) gates:</b>` +
                `<table style="font-size:12px;width:100%;">${gate_rows}</table>` +
                pull_block +
                "<br><br>" +
                im_block +
                "<br><br>" +
                sr_block +
                qj_block +
                ac_block;

            const failed = push_gates.some((g) => !g.passed);
            frappe.msgprint({
                title: __("Item Sync Trace — {0}", [frm.doc.name]),
                message: body,
                indicator: failed ? "orange" : "green",
                wide: true,
            });
        },
        error() {
            frappe.msgprint({
                title: __("Trace Failed"),
                message: __(
                    "Could not run the trace (network, server, or permission). " +
                        "Check Error Log."
                ),
                indicator: "red",
            });
        },
    });
}

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
