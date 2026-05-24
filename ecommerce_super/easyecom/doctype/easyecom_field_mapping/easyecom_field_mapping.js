// EasyEcom Field Mapping — detail view (§5.10.2)
//
// Adds the FDE actions named in §5.10.2:
//   - Show Computed Mapping  — expand identity defaults inline
//   - Test Mapping            — paste sample, run ruleset, see output + trace
//   - Diff Against Version    — compare current with a prior snapshot
//   - Rollback to Version     — restore a prior snapshot (creates a new version)

frappe.ui.form.on("EasyEcom Field Mapping", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(
			__("Show Computed Mapping"),
			() => show_computed_mapping(frm),
			__("Actions"),
		);
		frm.add_custom_button(
			__("Test Mapping"),
			() => test_mapping_dialog(frm),
			__("Actions"),
		);
		frm.add_custom_button(
			__("Diff Against Version"),
			() => diff_dialog(frm),
			__("Actions"),
		);
		frm.add_custom_button(
			__("Rollback to Version"),
			() => rollback_dialog(frm),
			__("Actions"),
		);
	},
});

function show_computed_mapping(frm) {
	frappe.call({
		method:
			"ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping.show_computed_mapping",
		args: { mapping_name: frm.doc.name },
		callback: (r) => {
			if (!r.message) return;
			const d = new frappe.ui.Dialog({
				title: __("Computed Mapping — {0}", [frm.doc.name]),
				size: "large",
				fields: [
					{
						fieldname: "computed",
						fieldtype: "Code",
						options: "JSON",
						read_only: 1,
						default: JSON.stringify(r.message, null, 2),
					},
				],
			});
			d.show();
		},
	});
}

function test_mapping_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Test Mapping — {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldname: "direction",
				fieldtype: "Select",
				label: __("Direction"),
				options: ["push", "pull"],
				default: "push",
				reqd: 1,
			},
			{
				fieldname: "sample",
				fieldtype: "Code",
				options: "JSON",
				label: __("Sample (paste JSON)"),
				reqd: 1,
			},
			{
				fieldname: "result_section",
				fieldtype: "Section Break",
				label: __("Result"),
				depends_on: "eval:doc.output",
			},
			{
				fieldname: "output",
				fieldtype: "Code",
				options: "JSON",
				label: __("Output"),
				read_only: 1,
			},
			{
				fieldname: "trace",
				fieldtype: "Code",
				options: "JSON",
				label: __("Trace"),
				read_only: 1,
			},
			{
				fieldname: "errors",
				fieldtype: "Code",
				options: "JSON",
				label: __("Errors"),
				read_only: 1,
			},
		],
		primary_action_label: __("Run"),
		primary_action(values) {
			frappe.call({
				method:
					"ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping.test_mapping",
				args: {
					mapping_name: frm.doc.name,
					sample: values.sample,
					direction: values.direction,
				},
				callback: (r) => {
					if (!r.message) return;
					d.set_value("output", JSON.stringify(r.message.output, null, 2));
					d.set_value("trace", JSON.stringify(r.message.trace, null, 2));
					d.set_value("errors", JSON.stringify(r.message.errors, null, 2));
				},
			});
		},
	});
	d.show();
}

function diff_dialog(frm) {
	frappe.db
		.get_list("EasyEcom Field Mapping Version", {
			filters: { parent_mapping: frm.doc.name },
			fields: ["version", "created_at", "created_by", "change_reason"],
			order_by: "version desc",
			limit: 50,
		})
		.then((versions) => {
			if (!versions || !versions.length) {
				frappe.msgprint(__("No prior versions to diff against."));
				return;
			}
			const options = versions
				.map(
					(v) =>
						`${v.version}: ${v.created_at} by ${v.created_by} — ${v.change_reason}`,
				)
				.join("\n");
			const d = new frappe.ui.Dialog({
				title: __("Diff Against Version — {0}", [frm.doc.name]),
				size: "large",
				fields: [
					{
						fieldname: "version_choice",
						fieldtype: "Select",
						label: __("Compare current with version"),
						options: options,
						reqd: 1,
					},
					{
						fieldname: "diff",
						fieldtype: "Code",
						options: "JSON",
						label: __("Diff"),
						read_only: 1,
					},
				],
				primary_action_label: __("Compute Diff"),
				primary_action(values) {
					const version = parseInt(values.version_choice.split(":")[0]);
					frappe.call({
						method:
							"ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping.diff_against_version",
						args: { mapping_name: frm.doc.name, version: version },
						callback: (r) => {
							if (!r.message) return;
							d.set_value("diff", JSON.stringify(r.message, null, 2));
						},
					});
				},
			});
			d.show();
		});
}

function rollback_dialog(frm) {
	frappe.db
		.get_list("EasyEcom Field Mapping Version", {
			filters: { parent_mapping: frm.doc.name },
			fields: ["version", "created_at", "created_by", "change_reason"],
			order_by: "version desc",
			limit: 50,
		})
		.then((versions) => {
			if (!versions || !versions.length) {
				frappe.msgprint(__("No prior versions to roll back to."));
				return;
			}
			const options = versions
				.map(
					(v) =>
						`${v.version}: ${v.created_at} by ${v.created_by} — ${v.change_reason}`,
				)
				.join("\n");
			const d = new frappe.ui.Dialog({
				title: __("Rollback — {0}", [frm.doc.name]),
				fields: [
					{
						fieldname: "version_choice",
						fieldtype: "Select",
						label: __("Restore to version"),
						options: options,
						reqd: 1,
					},
					{
						fieldname: "confirm",
						fieldtype: "Check",
						label: __("I understand this creates a new version with the restored content."),
						reqd: 1,
					},
				],
				primary_action_label: __("Rollback"),
				primary_action(values) {
					if (!values.confirm) {
						frappe.msgprint(__("Please confirm the rollback."));
						return;
					}
					const version = parseInt(values.version_choice.split(":")[0]);
					frappe.call({
						method:
							"ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping.rollback_to_version",
						args: { parent_mapping: frm.doc.name, version: version },
						callback: (r) => {
							if (r.message) {
								frappe.show_alert({
									message: __("Rolled back. New version: {0}", [r.message]),
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
		});
}
