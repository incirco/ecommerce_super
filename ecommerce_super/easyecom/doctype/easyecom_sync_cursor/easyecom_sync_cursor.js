// EasyEcom Sync Cursor — detail view (§6.5.3 Cursor Rewind FDE surface)

frappe.ui.form.on("EasyEcom Sync Cursor", {
	refresh(frm) {
		if (frm.is_new()) return;
		// Show the Rewind button only to System Manager / EasyEcom System Manager.
		// The server method enforces the same gate; this is just UI hygiene.
		const roles = frappe.user_roles || [];
		if (
			!roles.includes("System Manager") &&
			!roles.includes("EasyEcom System Manager")
		) {
			return;
		}
		frm.add_custom_button(
			__("Rewind Cursor"),
			() => rewind_dialog(frm),
			__("Actions"),
		);
	},
});

function rewind_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Rewind Cursor — {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "current",
				fieldtype: "Data",
				label: __("Current value"),
				default: frm.doc.cursor_value,
				read_only: 1,
			},
			{
				fieldname: "to_value",
				fieldtype: "Data",
				label: __("Rewind to"),
				reqd: 1,
				description: __(
					"ISO datetime, Next-Page URL, or opaque token — must match cursor_format ({0}).",
					[frm.doc.cursor_format],
				),
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason"),
				reqd: 1,
				description: __(
					"Captured for audit (§28). Required by §2.7 'no silent divergence'.",
				),
			},
			{
				fieldname: "warning",
				fieldtype: "HTML",
				options:
					"<div class='alert alert-warning'>" +
					__(
						"Rewinding will cause the next polling cycle to pull every record since the rewound value. Persistence-layer idempotency dedupes duplicates.",
					) +
					"</div>",
			},
			{
				fieldname: "confirm",
				fieldtype: "Check",
				label: __("I understand and want to proceed"),
				reqd: 1,
			},
		],
		primary_action_label: __("Rewind"),
		primary_action(values) {
			if (!values.confirm) {
				frappe.msgprint(__("Please confirm the rewind."));
				return;
			}
			frappe.call({
				method:
					"ecommerce_super.easyecom.doctype.easyecom_sync_cursor.easyecom_sync_cursor.rewind_cursor",
				args: {
					cursor_name: frm.doc.name,
					to_value: values.to_value,
					reason: values.reason,
				},
				callback: (r) => {
					if (r.message) {
						frappe.show_alert({
							message: __("Cursor rewound from {0} to {1}", [
								r.message.before_value,
								r.message.after_value,
							]),
							indicator: "green",
						});
						d.hide();
						frm.reload_doc();
					}
				},
			});
		},
	});
	d.show();
}
