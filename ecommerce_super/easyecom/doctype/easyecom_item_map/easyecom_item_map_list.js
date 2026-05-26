// EasyEcom Item Map list view — audit fix #4.
//
// - Indicator colors per status (Drift=red, Created-Flagged=orange,
//   Mapped=green, Flagged-Not-Created=grey, Disabled=grey).
// - Custom list columns surface the most-useful fields up front.
// - Sidebar preset filters (FDE worklist quick-jumps).

frappe.listview_settings["EasyEcom Item Map"] = {
    add_fields: [
        "status",
        "erpnext_doctype",
        "erpnext_name",
        "ee_product_id",
        "drift_detected_at",
        "flag_reason",
    ],

    // Status → indicator color + filter set for one-click drill-down.
    // Pattern: [label, color, filter_string].
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
        // FDE-worklist sidebar quick-filters.
        listview.page.add_menu_item(__("Show only Drift"), () => {
            listview.filter_area.add([
                ["EasyEcom Item Map", "status", "=", "Drift"],
            ]);
        });
        listview.page.add_menu_item(__("Show only Created-Flagged"), () => {
            listview.filter_area.add([
                ["EasyEcom Item Map", "status", "=", "Created-Flagged"],
            ]);
        });
        listview.page.add_menu_item(
            __("Show only Flagged-Not-Created"),
            () => {
                listview.filter_area.add([
                    [
                        "EasyEcom Item Map",
                        "status",
                        "=",
                        "Flagged-Not-Created",
                    ],
                ]);
            },
        );
        listview.page.add_menu_item(__("Show only Mapped (clean)"), () => {
            listview.filter_area.add([
                ["EasyEcom Item Map", "status", "=", "Mapped"],
            ]);
        });
    },
};
