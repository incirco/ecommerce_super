// EasyEcom Sync Record — detail view (§6.5.1 Retry Now action)

frappe.ui.form.on("EasyEcom Sync Record", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (!["Failed", "Cancelled"].includes(frm.doc.status)) return;
		frm.add_custom_button(
			__("Retry Now"),
			() => retry_action(frm),
			__("Actions"),
		);
	},
});

function retry_action(frm) {
	frappe.confirm(
		__(
			"Reset this Sync Record to Pending? The original idempotency_key and correlation_id are preserved per §6.1 / §6.5.1 — no duplicate processing on the EasyEcom side. The flow handler picks Pending records up on its next dispatch.",
		),
		() => {
			frappe.call({
				method:
					"ecommerce_super.easyecom.doctype.easyecom_sync_record.easyecom_sync_record.retry_now",
				args: { sync_record_name: frm.doc.name },
				callback: (r) => {
					if (r.message) {
						frappe.show_alert({
							message: __(
								"Sync Record set to Pending (attempts preserved: {0}).",
								[r.message.attempts_preserved],
							),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
	);
}
