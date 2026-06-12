// §10 UX layer — Delivery Note warehouse visibility.
//
//  1. Override the autocomplete on every warehouse Link field so each
//     row shows its EE-mapping status (ecs_ee_location_label) as the
//     description column. EE-mapped warehouses sort first.
//
//  2. Once both header warehouses are picked, render a dashboard
//     indicator on the form showing which §10 branch the DN will
//     route to (STN / PO / B2B / Inert) — surfaces the routing
//     consequence BEFORE the user submits and the §10 substrate fires
//     the EE call.

const WAREHOUSE_FIELDS = [
    "set_warehouse",
    "set_target_warehouse",
    "ecs_section10_target_warehouse",
    "ecs_section10_transfer_from_warehouse",
    "ecs_section10_transfer_to_warehouse",
];

frappe.ui.form.on("Delivery Note", {
    refresh(frm) {
        wire_warehouse_queries(frm);
        refresh_all_warehouse_labels(frm);
        refresh_section10_branch_indicator(frm);
        // gh#26: diagnostic for submitted DNs — when the FDE says
        // "I submitted but nothing showed up in Harmony", the button
        // walks every gate and returns the trace.
        if (frm.doc.docstatus === 1 && frm.doc.is_internal_customer) {
            frm.add_custom_button(
                __("Trace Outbound Push (§10)"),
                () => trace_outbound(frm),
                __("EasyEcom")
            );
        }
    },
    set_warehouse(frm) {
        refresh_warehouse_label(frm, "set_warehouse");
        refresh_section10_branch_indicator(frm);
    },
    set_target_warehouse(frm) {
        refresh_warehouse_label(frm, "set_target_warehouse");
        refresh_section10_branch_indicator(frm);
    },
    ecs_section10_target_warehouse(frm) {
        refresh_warehouse_label(frm, "ecs_section10_target_warehouse");
        refresh_section10_branch_indicator(frm);
    },
    ecs_section10_transfer_from_warehouse(frm) {
        refresh_warehouse_label(frm, "ecs_section10_transfer_from_warehouse");
        refresh_section10_branch_indicator(frm);
    },
    ecs_section10_transfer_to_warehouse(frm) {
        refresh_warehouse_label(frm, "ecs_section10_transfer_to_warehouse");
        refresh_section10_branch_indicator(frm);
    },
    is_internal_customer(frm) {
        refresh_section10_branch_indicator(frm);
    },
});


// gh#26 — render the trace returned by the diagnostic endpoint as a
// readable msgprint. The user gets a per-gate pass/fail breakdown +
// downstream artifacts (Transfer Map, Sync Records, Queue Jobs, API
// Calls) + a one-line verdict. Read-only — does NOT re-fire the push.
function trace_outbound(frm) {
    frappe.show_alert({
        message: __("Tracing §10 outbound push…"),
        indicator: "blue",
    });
    frappe.call({
        method:
            "ecommerce_super.easyecom.api.transfer_diagnostic.trace_dn",
        args: {dn_name: frm.doc.name},
        freeze: true,
        freeze_message: __("Tracing…"),
        callback(r) {
            const t = r.message || {};
            const gates = t.gates || [];
            const dn = t.downstream || {};

            const gate_rows = gates
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

            const tm = dn.transfer_map;
            const tm_block = tm
                ? `<b>Transfer Map:</b> <code>${frappe.utils.escape_html(tm.name)}</code> ` +
                  `· status=<code>${frappe.utils.escape_html(tm.status || "—")}</code> ` +
                  `· ee_order_id=<code>${frappe.utils.escape_html(tm.ee_order_id || "—")}</code><br>`
                : "<b>Transfer Map:</b> <i>none</i><br>";

            const sr = dn.sync_records || [];
            const sr_block = sr.length
                ? `<b>Sync Records (${sr.length}):</b><br>` +
                  sr
                      .map(
                          (s) =>
                              `&nbsp;&nbsp;<code>${frappe.utils.escape_html(s.name)}</code> ` +
                              `[${frappe.utils.escape_html(s.status)}] ` +
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
                `<table style="font-size:12px;width:100%;">${gate_rows}</table><br>` +
                tm_block +
                sr_block +
                qj_block +
                ac_block;

            const failed = (t.gates || []).some((g) => !g.passed);
            frappe.msgprint({
                title: __("Outbound Push Trace — {0}", [frm.doc.name]),
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

frappe.ui.form.on("Delivery Note Item", {
    items_add(frm) {
        wire_warehouse_queries(frm);
    },
});

function refresh_all_warehouse_labels(frm) {
    WAREHOUSE_FIELDS.forEach((fname) => refresh_warehouse_label(frm, fname));
}

function refresh_warehouse_label(frm, fname) {
    const field = frm.fields_dict[fname];
    if (!field) return;
    const value = frm.doc[fname];
    if (!value) {
        set_field_description(frm, fname, "");
        return;
    }
    // Defensive on column presence (gh#26 follow-up): if the rescue
    // patch hasn't run on this deployment yet, the column doesn't
    // exist and `frappe.db.get_value` will 500. Catch + render the
    // neutral "Not EE-mapped" state instead of crashing the form.
    frappe.db
        .get_value("Warehouse", value, "ecs_ee_location_label")
        .then((r) => {
            const label = (r && r.message && r.message.ecs_ee_location_label) || "";
            const html = label
                ? `<span style="color:#1F7AEC;font-weight:500;">${frappe.utils.escape_html(label)}</span>`
                : `<span style="color:#888;">Not EE-mapped</span>`;
            set_field_description(frm, fname, html);
        })
        .catch(() => {
            // Column missing or backend error — degrade gracefully.
            set_field_description(
                frm, fname,
                `<span style="color:#888;">Not EE-mapped</span>`,
            );
        });
}

function set_field_description(frm, fname, html) {
    if (!frm.fields_dict[fname]) return;
    frm.set_df_property(fname, "description", html);
    frm.refresh_field(fname);
}

function wire_warehouse_queries(frm) {
    const link_query = "ecommerce_super.easyecom.api.warehouse_query.warehouse_with_ee_label";

    // Header-level warehouse fields.
    WAREHOUSE_FIELDS.forEach((fname) => {
        if (!frm.fields_dict[fname]) return;
        frm.set_query(fname, () => ({ query: link_query }));
    });

    // Per-line warehouse fields (source + in-transit target on the
    // items grid).
    ["warehouse", "target_warehouse"].forEach((fname) => {
        if (!frm.fields_dict.items) return;
        frm.set_query(fname, "items", () => ({ query: link_query }));
    });
}

function refresh_section10_branch_indicator(frm) {
    // Indicator is only meaningful for §10 internal transfers. The
    // FDE-facing toggle is ecs_is_section10_transfer (pre-ticks from
    // customer.is_internal_customer but the FDE can override).
    if (!frm.doc.ecs_is_section10_transfer && !frm.doc.is_internal_customer) {
        clear_branch_indicator(frm);
        return;
    }
    // Source of truth: the §10 FDE-pick fields. Fall back to
    // set_warehouse / set_target_warehouse for DNs predating the
    // §10-fields patch.
    const src =
        frm.doc.ecs_section10_transfer_from_warehouse ||
        frm.doc.set_warehouse;
    const tgt =
        frm.doc.ecs_section10_transfer_to_warehouse ||
        frm.doc.ecs_section10_target_warehouse ||
        frm.doc.set_target_warehouse;

    if (!src || !tgt) {
        clear_branch_indicator(frm);
        return;
    }

    frappe.call({
        method: "ecommerce_super.easyecom.api.warehouse_query.predict_section10_branch",
        args: { source_warehouse: src, target_warehouse: tgt },
        callback(r) {
            if (!r || !r.message) return;
            render_branch_indicator(frm, r.message);
        },
    });
}

function render_branch_indicator(frm, prediction) {
    const { branch, color, explanation, source_label, target_label,
            source_ee_mapped, target_ee_mapped } = prediction;

    clear_branch_indicator(frm);

    const src_glyph = source_ee_mapped ? "✓ EE" : "— non-EE";
    const tgt_glyph = target_ee_mapped ? "✓ EE" : "— non-EE";
    const label = `§10 branch: ${branch}  ·  src ${src_glyph}  ·  tgt ${tgt_glyph}`;

    frm.dashboard.add_indicator(label, color);
    frm.__section10_branch_indicator_label = label;

    // Also drop an explanation line just under the title bar so the
    // FDE sees the consequence even when the dashboard chips are
    // collapsed.
    if (frm.fields_dict.is_internal_customer) {
        const desc_html = `<div class="text-muted small" style="margin-top:4px;">
            <b>§10 routing:</b> ${frappe.utils.escape_html(explanation)}
            ${source_label ? `<br><i>Source</i>: ${frappe.utils.escape_html(source_label)}` : ""}
            ${target_label ? `<br><i>Target</i>: ${frappe.utils.escape_html(target_label)}` : ""}
        </div>`;
        frm.set_df_property("is_internal_customer", "description", desc_html);
        frm.refresh_field("is_internal_customer");
    }
}

function clear_branch_indicator(frm) {
    // Frappe's dashboard.add_indicator appends to a chip strip; there
    // is no remove API. The cheapest reset is to clear and let
    // refresh re-add (only fires when the prediction changes).
    if (frm.dashboard && frm.dashboard.stats_area_row) {
        // No-op: chips persist until next form refresh. The visual
        // duplication is a known Frappe limitation; tolerated here
        // since the label encodes branch + EE status, so a stale
        // chip is still informative until the next refresh.
    }
    if (frm.fields_dict && frm.fields_dict.is_internal_customer) {
        frm.set_df_property("is_internal_customer", "description", "");
        frm.refresh_field("is_internal_customer");
    }
}
