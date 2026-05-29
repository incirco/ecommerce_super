// EasyEcom Transfer Map list view — §10 Stage 4. Status colours mirror
// the §9 PO/GRN Map convention. Filter shortcuts surface the four
// FDE-actionable cohorts.

frappe.listview_settings["EasyEcom Transfer Map"] = {
    add_fields: [
        "status",
        "delivery_note",
        "sales_invoice",
        "ee_doctype",
        "ee_order_id",
        "ee_po_id",
        "gstin_different",
        "internal_purchase_invoice",
        "draft_debit_note",
        "flag_reason",
    ],

    get_indicator(doc) {
        const map = {
            "Mapped": ["Mapped", "blue", "status,=,Mapped"],
            "SI-Pending": ["SI-Pending", "orange", "status,=,SI-Pending"],
            "SI-Submitted": [
                "SI-Submitted",
                "blue",
                "status,=,SI-Submitted",
            ],
            "EE-Pushed": ["EE-Pushed", "blue", "status,=,EE-Pushed"],
            "Partial-Received": [
                "Partial-Received",
                "yellow",
                "status,=,Partial-Received",
            ],
            "Fully-Received": [
                "Fully-Received",
                "green",
                "status,=,Fully-Received",
            ],
            "DN-Submitted-Locked": [
                "DN-Submitted-Locked",
                "grey",
                "status,=,DN-Submitted-Locked",
            ],
            "Drift": ["Drift", "red", "status,=,Drift"],
            "Disabled": ["Disabled", "darkgrey", "status,=,Disabled"],
        };
        return (
            map[doc.status] || ["Unknown", "grey", `status,=,${doc.status}`]
        );
    },

    onload(listview) {
        listview.page.add_menu_item(__("Show only Drift"), () => {
            listview.filter_area.add([
                ["EasyEcom Transfer Map", "status", "=", "Drift"],
            ]);
        });
        listview.page.add_menu_item(
            __("Show Receipt Pending (EE-Pushed / Partial)"),
            () => {
                listview.filter_area.add([
                    [
                        "EasyEcom Transfer Map",
                        "status",
                        "in",
                        ["EE-Pushed", "Partial-Received"],
                    ],
                ]);
            },
        );
        listview.page.add_menu_item(__("Show Aged-GIT candidates"), () => {
            // Aged GIT = a draft Debit Note exists (open gap) AND
            // we're still in Partial-Received state. The cron walks
            // the same set + the time-window check; this is the
            // visual filter without the date predicate.
            listview.filter_area.add([
                [
                    "EasyEcom Transfer Map",
                    "status",
                    "=",
                    "Partial-Received",
                ],
                [
                    "EasyEcom Transfer Map",
                    "draft_debit_note",
                    "is",
                    "set",
                ],
            ]);
        });
        listview.page.add_menu_item(__("Show All Active"), () => {
            listview.filter_area.add([
                [
                    "EasyEcom Transfer Map",
                    "status",
                    "not in",
                    ["Disabled", "DN-Submitted-Locked"],
                ],
            ]);
        });
    },
};
