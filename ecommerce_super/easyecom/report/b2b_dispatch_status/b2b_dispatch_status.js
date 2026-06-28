// §11.6 — B2B Dispatch Status filters
frappe.query_reports["B2B Dispatch Status"] = {
    filters: [
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
            default: frappe.defaults.get_user_default("Company"),
            reqd: 1,
        },
        {
            fieldname: "from_date",
            label: __("From Posting Date"),
            fieldtype: "Date",
            default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
        },
        {
            fieldname: "to_date",
            label: __("To Posting Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
        },
        {
            fieldname: "dispatch_status",
            label: __("Dispatch Status"),
            fieldtype: "MultiSelectList",
            get_data: function () {
                return [
                    {value: "Pending",   description: ""},
                    {value: "Shipped",   description: ""},
                    {value: "Delivered", description: ""},
                    {value: "Returned",  description: ""},
                    {value: "Cancelled", description: ""},
                ];
            },
        },
    ],

    formatter: function (value, row, column, data, default_formatter) {
        value = default_formatter(value, row, column, data);
        if (column.fieldname === "dispatch_status") {
            const status = data.dispatch_status || "";
            const colour = {
                Pending: "orange",
                Shipped: "blue",
                Delivered: "green",
                Returned: "red",
                Cancelled: "darkgrey",
            }[status] || "grey";
            value = `<span class="indicator ${colour}">${status || "Unknown"}</span>`;
        }
        if (column.fieldname === "age_days" && data.age_days !== null && data.age_days >= 7
            && (data.dispatch_status === "Pending" || data.dispatch_status === "Shipped")) {
            value = `<span style="color: var(--red-500); font-weight: 600;">${value}</span>`;
        }
        return value;
    },
};
