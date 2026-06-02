// Address form UX — lock + alert when the Address is mirrored from
// an EasyEcom Location.
//
// Contract:
//   - `ecs_ee_location` is the back-pointer custom field.
//   - When non-empty, this Address is EE-managed: the FDE should
//     edit the Location, not the Address. The next Location save
//     overwrites any in-place edits here (single source of truth).
//   - We render a banner at the top of the form with a deep link to
//     the Location and mark the source-of-truth fields read-only.
//
// We don't enforce server-side rejection — the read-only rendering
// + banner is "soft lock". If a FDE genuinely needs to bypass during
// incident response, the Console / db.set_value still work.

const EE_MANAGED_FIELDS = [
    "address_line1",
    "address_line2",
    "city",
    "state",
    "country",
    "pincode",
    "gstin",
    "gst_category",
];

frappe.ui.form.on("Address", {
    refresh(frm) {
        apply_ee_lock(frm);
    },
    ecs_ee_location(frm) {
        apply_ee_lock(frm);
    },
});

function apply_ee_lock(frm) {
    const ee_loc = frm.doc.ecs_ee_location;

    EE_MANAGED_FIELDS.forEach((fname) => {
        if (!frm.fields_dict[fname]) return;
        frm.set_df_property(fname, "read_only", ee_loc ? 1 : 0);
    });

    clear_banner(frm);
    if (!ee_loc) return;

    const loc_url = `/app/easyecom-location/${encodeURIComponent(ee_loc)}`;
    const banner = `
        <div class="ecs-ee-lock-banner" style="
            padding:10px 12px;
            background:#fef3c7;
            border:1px solid #fbbf24;
            border-radius:4px;
            margin-bottom:12px;
            font-size:13px;
            line-height:1.5;
        ">
            <b>EasyEcom-managed Address.</b>
            Address fields are mirrored from
            <a href="${loc_url}"><code>${frappe.utils.escape_html(ee_loc)}</code></a>.
            Edit there — any changes here will be overwritten on the next
            Location save.
        </div>
    `;
    if (frm.layout && frm.layout.wrapper) {
        const $wrapper = frm.layout.wrapper.find(".form-layout");
        if ($wrapper.length) {
            $wrapper.prepend(banner);
        } else {
            frm.layout.wrapper.prepend(banner);
        }
    }
}

function clear_banner(frm) {
    if (frm.layout && frm.layout.wrapper) {
        frm.layout.wrapper.find(".ecs-ee-lock-banner").remove();
    }
}
