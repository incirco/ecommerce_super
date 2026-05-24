// EasyEcom Queue Job — detail view (§6.5.1 Retry / Cancel FDE actions)
//
// The server methods queue.retry_job and queue.cancel_job already exist
// (built in §3+§4 foundation). This JS just wires the desk buttons.

frappe.ui.form.on("EasyEcom Queue Job", {
	refresh(frm) {
		if (frm.is_new()) return;

		const state = frm.doc.state;

		// Retry — visible only for Failed or Cancelled (the only states
		// the server method accepts; spec §6.3.3).
		if (state === "Failed" || state === "Cancelled") {
			frm.add_custom_button(
				__("Retry Now"),
				() => retry_action(frm),
				__("Actions"),
			);
		}

		// Cancel — visible for non-terminal states.
		if (["Queued", "Running", "Retrying"].includes(state)) {
			frm.add_custom_button(
				__("Cancel"),
				() => cancel_dialog(frm),
				__("Actions"),
			);
		}
	},
});

function retry_action(frm) {
	frappe.confirm(
		__(
			"Re-enqueue this job? The original correlation_id is preserved so all historical logs link to the same operation (§6.5.1).",
		),
		() => {
			frappe.call({
				method: "ecommerce_super.easyecom.queue.retry_job",
				args: { job_id: frm.doc.name },
				callback: (r) => {
					if (r.message) {
						frappe.show_alert({
							message: __("Job re-enqueued."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	);
}

function cancel_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Cancel Job — {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason"),
				reqd: 1,
				description: __(
					"Captured on the Queue Job. A Running job's current attempt may complete; the next attempt early-exits.",
				),
			},
		],
		primary_action_label: __("Cancel Job"),
		primary_action(values) {
			frappe.call({
				method: "ecommerce_super.easyecom.queue.cancel_job",
				args: { job_id: frm.doc.name, reason: values.reason },
				callback: () => {
					frappe.show_alert({
						message: __("Job cancelled."),
						indicator: "orange",
					});
					d.hide();
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}
