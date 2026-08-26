// gh#267 — Corrupt Submitted Sales Invoices report filters
frappe.query_reports["Corrupt Submitted Sales Invoices"] = {
    filters: [
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
            default: frappe.defaults.get_user_default("Company"),
        },
        {
            fieldname: "from_date",
            label: __("From Posting Date"),
            fieldtype: "Date",
            // Wide default — this report exists to catch historical
            // corruption, not just recent. Ops can narrow if needed.
        },
        {
            fieldname: "to_date",
            label: __("To Posting Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
        },
        {
            fieldname: "bucket",
            label: __("Bucket"),
            fieldtype: "Select",
            options: "\nZERO_STOCK\nZERO_GL\nPARTIAL",
            default: "",
        },
    ],

    formatter: function (value, row, column, data, default_formatter) {
        value = default_formatter(value, row, column, data);
        if (column.fieldname === "bucket") {
            const color = {
                ZERO_STOCK: "red",
                ZERO_GL: "red",
                PARTIAL: "orange",
            }[data.bucket] || "grey";
            value = `<span class="indicator ${color}">${data.bucket}</span>`;
        }
        if (column.fieldname === "sle_gap" && data.sle_gap > 0) {
            value = `<span style="color: var(--red-500); font-weight: 600;">${value}</span>`;
        }
        if (column.fieldname === "gl_count" && data.gl_count === 0) {
            value = `<span style="color: var(--red-500); font-weight: 600;">${value}</span>`;
        }
        return value;
    },
};
