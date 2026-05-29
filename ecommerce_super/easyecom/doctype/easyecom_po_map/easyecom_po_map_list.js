// EasyEcom PO Map list view — §9 Stage 4 — parity with §8 master
// maps. Status colours mirror Supplier / Customer / Item Map; the
// Disabled state is reserved for future PO-lifecycle work.

frappe.listview_settings["EasyEcom PO Map"] = {
    add_fields: [
        "status",
        "purchase_order",
        "ee_po_id",
        "last_pushed_po_status",
        "ee_observed_po_status",
        "flag_reason",
    ],

    get_indicator(doc) {
        const map = {
            "Mapped": ["Mapped", "green", "status,=,Mapped"],
            "Created-Flagged": [
                "Created-Flagged",
                "orange",
                "status,=,Created-Flagged",
            ],
            "Flagged-Not-Created": [
                "Flagged-Not-Created",
                "grey",
                "status,=,Flagged-Not-Created",
            ],
            "Drift": ["Drift", "red", "status,=,Drift"],
            "Disabled": ["Disabled", "darkgrey", "status,=,Disabled"],
        };
        return (
            map[doc.status] || ["Unknown", "grey", `status,=,${doc.status}`]
        );
    },

    onload(listview) {
        listview.page.add_menu_item(
            __("Show only Drift"),
            () => {
                listview.filter_area.add([
                    ["EasyEcom PO Map", "status", "=", "Drift"],
                ]);
            },
        );
        listview.page.add_menu_item(
            __("Show only Flagged-Not-Created"),
            () => {
                listview.filter_area.add([
                    [
                        "EasyEcom PO Map",
                        "status",
                        "=",
                        "Flagged-Not-Created",
                    ],
                ]);
            },
        );
        listview.page.add_menu_item(
            __("Show only Mapped (clean)"),
            () => {
                listview.filter_area.add([
                    ["EasyEcom PO Map", "status", "=", "Mapped"],
                ]);
            },
        );
    },
};
