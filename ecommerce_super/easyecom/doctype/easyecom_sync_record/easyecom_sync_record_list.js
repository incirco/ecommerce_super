// EasyEcom Sync Record list view — §9 Stage 4 line-child outcome
// indicator. The Sync Record's lines child carries per-line outcomes
// for nested-document flows (GRN line × N, future Order/Return lines).
// This formatter surfaces the breakdown as a compact chip without
// loading the child table for every row — the parent's
// `ecs_lines_summary` field is recomputed on save by the controller.

frappe.listview_settings["EasyEcom Sync Record"] = {
    add_fields: [
        "status",
        "direction",
        "entity_type",
        "ecs_lines_summary",
        "attempts",
    ],

    get_indicator(doc) {
        // Sync Record outcome is BINARY per §7.3 — Success | Failed.
        // Drift / discrepancy classification is carried in last_error
        // and on child line_status, NOT on the parent status enum.
        const map = {
            Pending: ["Pending", "grey", "status,=,Pending"],
            Running: ["Running", "blue", "status,=,Running"],
            Success: ["Success", "green", "status,=,Success"],
            Failed: ["Failed", "red", "status,=,Failed"],
            Cancelled: ["Cancelled", "darkgrey", "status,=,Cancelled"],
            AlreadySynced: [
                "AlreadySynced",
                "lightblue",
                "status,=,AlreadySynced",
            ],
        };
        return (
            map[doc.status] ||
            ["Unknown", "grey", `status,=,${doc.status}`]
        );
    },

    formatters: {
        ecs_lines_summary(value) {
            if (!value) return "";
            // Colour cue: any 'Failed' → red; any 'Discrepancy' (and
            // no Failed) → orange; otherwise green (all OK).
            const hasFailed = /\bFailed\b/.test(value);
            const hasDisc = /\bDiscrepancy\b/.test(value);
            const colour = hasFailed
                ? "red"
                : hasDisc
                  ? "orange"
                  : "green";
            return `<span class="indicator-pill ${colour}" title="${frappe.utils.escape_html(value)}">${frappe.utils.escape_html(value)}</span>`;
        },
    },

    onload(listview) {
        listview.page.add_menu_item(__("Show only Failed"), () => {
            listview.filter_area.add([
                ["EasyEcom Sync Record", "status", "=", "Failed"],
            ]);
        });
        listview.page.add_menu_item(
            __("Show only Running / Pending"),
            () => {
                listview.filter_area.add([
                    [
                        "EasyEcom Sync Record",
                        "status",
                        "in",
                        ["Pending", "Running"],
                    ],
                ]);
            },
        );
        // §10 Stage 4 — surface §10 Stock Transfer Sync Records by
        // entity_type. §10 outbound + inbound both key Sync Records on
        // entity_doctype="Delivery Note" + entity_type="Delivery Note".
        listview.page.add_menu_item(
            __("Show only §10 Stock Transfer"),
            () => {
                listview.filter_area.add([
                    [
                        "EasyEcom Sync Record",
                        "entity_type",
                        "=",
                        "Delivery Note",
                    ],
                ]);
            },
        );
    },
};
