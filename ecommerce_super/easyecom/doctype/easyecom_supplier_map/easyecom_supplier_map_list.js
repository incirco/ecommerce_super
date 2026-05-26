// EasyEcom Supplier Map list view — parity with §8d/§8e.
//
// - Indicator colors per status (Drift=red, Created-Flagged=orange,
//   Mapped=green, Flagged-Not-Created=grey, Disabled=darkgrey).
// - Sidebar preset filters (FDE worklist quick-jumps).

frappe.listview_settings["EasyEcom Supplier Map"] = {
    add_fields: [
        "status",
        "erpnext_doctype",
        "erpnext_name",
        "ee_vendor_id",
        "drift_detected_at",
        "flag_reason",
    ],

    get_indicator(doc) {
        const map = {
            "Mapped": ["Mapped", "green", "status,=,Mapped"],
            "Created-Flagged": [
                "Created-Flagged", "orange", "status,=,Created-Flagged",
            ],
            "Flagged-Not-Created": [
                "Flagged-Not-Created", "grey", "status,=,Flagged-Not-Created",
            ],
            "Drift": ["Drift", "red", "status,=,Drift"],
            "Disabled": ["Disabled", "darkgrey", "status,=,Disabled"],
        };
        return map[doc.status] || ["Unknown", "grey", `status,=,${doc.status}`];
    },

    onload(listview) {
        listview.page.add_menu_item(__("Show only Drift"), () => {
            listview.filter_area.add([
                ["EasyEcom Supplier Map", "status", "=", "Drift"],
            ]);
        });
        listview.page.add_menu_item(__("Show only Created-Flagged"), () => {
            listview.filter_area.add([
                ["EasyEcom Supplier Map", "status", "=", "Created-Flagged"],
            ]);
        });
        listview.page.add_menu_item(
            __("Show only Flagged-Not-Created"),
            () => {
                listview.filter_area.add([
                    [
                        "EasyEcom Supplier Map",
                        "status",
                        "=",
                        "Flagged-Not-Created",
                    ],
                ]);
            },
        );
        listview.page.add_menu_item(__("Show only Mapped (clean)"), () => {
            listview.filter_area.add([
                ["EasyEcom Supplier Map", "status", "=", "Mapped"],
            ]);
        });
    },
};
