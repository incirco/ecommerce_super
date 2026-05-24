// EasyEcom Field Mapping — list view (§5.10.1)
//
// Adds bulk actions: Activate, Deactivate, Export to JSON.
// Import from JSON is deferred (the bulk import surface is part of the
// generic Frappe data-import flow; the §5.11 fixtures cover the
// initial-shipping case).

frappe.listview_settings["EasyEcom Field Mapping"] = {
	add_fields: ["active", "version", "entity_type", "direction"],

	get_indicator(doc) {
		if (doc.active) {
			return [__("Active"), "green", "active,=,1"];
		}
		return [__("Inactive"), "gray", "active,=,0"];
	},

	onload(listview) {
		listview.page.add_action_item(__("Activate Selected"), () =>
			bulk_set_active(listview, 1),
		);
		listview.page.add_action_item(__("Deactivate Selected"), () =>
			bulk_set_active(listview, 0),
		);
		listview.page.add_action_item(__("Export Selected to JSON"), () =>
			export_selected(listview),
		);
	},
};

function bulk_set_active(listview, active) {
	const docnames = listview.get_checked_items(true);
	if (!docnames || !docnames.length) {
		frappe.msgprint(__("Select at least one mapping."));
		return;
	}
	frappe.call({
		method:
			"ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping.bulk_set_active",
		args: { names: JSON.stringify(docnames), active: active },
		callback: (r) => {
			if (r.message !== undefined) {
				frappe.show_alert({
					message: __("{0} mapping(s) updated.", [r.message]),
					indicator: "green",
				});
				listview.refresh();
			}
		},
	});
}

function export_selected(listview) {
	const docnames = listview.get_checked_items(true);
	if (!docnames || !docnames.length) {
		frappe.msgprint(__("Select at least one mapping."));
		return;
	}
	frappe.call({
		method:
			"ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping.export_to_json",
		args: { names: JSON.stringify(docnames) },
		callback: (r) => {
			if (!r.message) return;
			const d = new frappe.ui.Dialog({
				title: __("Export — {0} mapping(s)", [docnames.length]),
				size: "large",
				fields: [
					{
						fieldname: "exported",
						fieldtype: "Code",
						options: "JSON",
						label: __("JSON (copy to clipboard)"),
						default: r.message,
					},
				],
			});
			d.show();
		},
	});
}
