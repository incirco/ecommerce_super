// §11 Phase 1 — Sales Order EE B2B form UX.
//
//  1. "Cancel on EasyEcom" button — visible only when:
//       - SO has an ecs_b2b_order_map link populated, AND
//       - The linked Map's status is in {Pushed, Queued}.
//     Posts to /orders/cancelOrder via the whitelisted endpoint;
//     refreshes the form on success.
//
//  2. "Trace B2B Push" button — visible for any submitted SO. Calls
//     the trace_so diagnostic; renders gate-by-gate trace + Map state
//     + recent Discrepancies in a wide msgprint.

frappe.ui.form.on("Sales Order", {
    refresh(frm) {
        if (frm.doc.docstatus !== 1) return;
        maybe_add_cancel_button(frm);
        maybe_add_trace_button(frm);
    },
});


function maybe_add_cancel_button(frm) {
    const map_name = frm.doc.ecs_b2b_order_map;
    if (!map_name) return;
    // Fetch the map status to decide button visibility. Skip the
    // button entirely if status isn't in the cancellable set.
    frappe.db
        .get_value("EasyEcom B2B Order Map", map_name, "status")
        .then((r) => {
            const status = (r && r.message && r.message.status) || "";
            if (!["Pushed", "Queued"].includes(status)) return;
            frm.add_custom_button(
                __("Cancel on EasyEcom"),
                () => confirm_and_cancel(frm),
                __("EasyEcom")
            );
        });
}


function confirm_and_cancel(frm) {
    frappe.confirm(
        __(
            "Cancel this B2B order on EasyEcom? The Sales Order in " +
                "ERPNext will NOT be cancelled — that's a separate " +
                "decision."
        ),
        () => {
            frappe.call({
                method:
                    "ecommerce_super.easyecom.flows.b2b_sales.cancel." +
                    "cancel_b2b_order_from_erpnext",
                args: { sales_order: frm.doc.name },
                freeze: true,
                freeze_message: __("Cancelling on EasyEcom…"),
                callback(r) {
                    if (r.message && r.message.ok) {
                        frappe.show_alert({
                            message: __(
                                "EE: {0}",
                                [r.message.ee_message || "Cancelled."]
                            ),
                            indicator: "green",
                        });
                        frm.reload_doc();
                    }
                },
            });
        }
    );
}


function maybe_add_trace_button(frm) {
    frm.add_custom_button(
        __("Trace B2B Push (§11)"),
        () => trace_b2b(frm),
        __("EasyEcom")
    );
}


function trace_b2b(frm) {
    frappe.show_alert({
        message: __("Tracing §11 B2B push…"),
        indicator: "blue",
    });
    frappe.call({
        method:
            "ecommerce_super.easyecom.api.trace_b2b_so.trace_so",
        args: { so_name: frm.doc.name },
        freeze: true,
        freeze_message: __("Tracing…"),
        callback(r) {
            const t = r.message || {};
            const gates = t.gates || [];
            const dn = t.downstream || {};

            const gate_rows = gates
                .map((g) => {
                    const icon =
                        g.passed === true
                            ? "✓"
                            : g.passed === false
                            ? "✗"
                            : "·";
                    const color =
                        g.passed === true
                            ? "#16a34a"
                            : g.passed === false
                            ? "#dc2626"
                            : "#888";
                    return (
                        `<tr><td style="color:${color};width:24px;">${icon}</td>` +
                        `<td><code>${frappe.utils.escape_html(g.gate)}</code></td>` +
                        `<td>${frappe.utils.escape_html(g.detail || "")}</td></tr>`
                    );
                })
                .join("");

            const map = dn.b2b_order_map;
            const map_block = map
                ? `<b>B2B Order Map:</b> <code>${frappe.utils.escape_html(map.name)}</code> ` +
                  `· status=<code>${frappe.utils.escape_html(map.status || "—")}</code> ` +
                  `· ee_order_id=<code>${frappe.utils.escape_html(map.ee_order_id || "—")}</code><br>`
                : "<b>B2B Order Map:</b> <i>none</i><br>";

            const disc = dn.discrepancies || [];
            const disc_block = disc.length
                ? `<b>Discrepancies (${disc.length}):</b><br>` +
                  disc
                      .map(
                          (d) =>
                              `&nbsp;&nbsp;<code>${frappe.utils.escape_html(d.name)}</code> ` +
                              `[${frappe.utils.escape_html(d.kind)}] ` +
                              `${frappe.utils.escape_html(d.status)}`
                      )
                      .join("<br>")
                : "<b>Discrepancies:</b> <i>none</i>";

            const body =
                `<b>Verdict:</b> ${frappe.utils.escape_html(t.verdict || "—")}<br><br>` +
                `<table style="font-size:12px;width:100%;">${gate_rows}</table><br>` +
                map_block +
                disc_block;

            const failed = (t.gates || []).some(
                (g) => g.passed === false
            );
            frappe.msgprint({
                title: __("B2B Push Trace — {0}", [frm.doc.name]),
                message: body,
                indicator: failed ? "orange" : "green",
                wide: true,
            });
        },
        error() {
            frappe.msgprint({
                title: __("Trace Failed"),
                message: __(
                    "Could not run the trace (network, server, or " +
                        "permission). Check Error Log."
                ),
                indicator: "red",
            });
        },
    });
}
