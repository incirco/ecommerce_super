// EasyEcom GRN Map list view — §9 Stage 4 — 7-state enum, distinct
// colours: Receipted = green (clean), Held-Pre-QC = amber (waiting),
// STN-Routed = blue (§9↔§10 boundary), Failed = red, Discrepancy =
// orange (FDE worklist), Deleted-Post-Receipt = grey (incident-only),
// Pending = muted (substrate-pre-pull).

frappe.listview_settings["EasyEcom GRN Map"] = {
    add_fields: [
        "status",
        "ee_grn_id",
        "po_ref_num",
        "ee_po_id",
        "vendor_c_id",
        "purchase_receipt",
        "routed_to_stn",
        "grn_status_id",
    ],

    get_indicator(doc) {
        const map = {
            "Receipted": ["Receipted", "green", "status,=,Receipted"],
            "Held-Pre-QC": [
                "Held-Pre-QC", "yellow", "status,=,Held-Pre-QC",
            ],
            "STN-Routed": ["STN-Routed", "blue", "status,=,STN-Routed"],
            "Failed": ["Failed", "red", "status,=,Failed"],
            "Discrepancy": [
                "Discrepancy", "orange", "status,=,Discrepancy",
            ],
            "Deleted-Post-Receipt": [
                "Deleted-Post-Receipt",
                "darkgrey",
                "status,=,Deleted-Post-Receipt",
            ],
            "Pending": ["Pending", "grey", "status,=,Pending"],
        };
        return (
            map[doc.status] || ["Unknown", "grey", `status,=,${doc.status}`]
        );
    },

    onload(listview) {
        listview.page.add_menu_item(
            __("Show only Failed"),
            () => {
                listview.filter_area.add([
                    ["EasyEcom GRN Map", "status", "=", "Failed"],
                ]);
            },
        );
        listview.page.add_menu_item(
            __("Show only Discrepancy"),
            () => {
                listview.filter_area.add([
                    ["EasyEcom GRN Map", "status", "=", "Discrepancy"],
                ]);
            },
        );
        listview.page.add_menu_item(
            __("Show only Held-Pre-QC"),
            () => {
                listview.filter_area.add([
                    ["EasyEcom GRN Map", "status", "=", "Held-Pre-QC"],
                ]);
            },
        );
        listview.page.add_menu_item(
            __("Show only STN-Routed (pending §10 pickup)"),
            () => {
                listview.filter_area.add([
                    ["EasyEcom GRN Map", "status", "=", "STN-Routed"],
                ]);
            },
        );
    },
};
