// recon-launch — dispatcher for the Recon apps-screen tile.
// See #246.
//
// On load, calls ecommerce_super.api.recon_launch.get_state:
//   - If the recon app (easyecom_recon) is installed → set_route to
//     its workspace and this page is never actually visible.
//   - Otherwise → render an inline upsell describing the module.

frappe.pages["recon-launch"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __("Marketplace Reconciliation"),
        single_column: true,
    });

    frappe.call({
        method: "ecommerce_super.api.recon_launch.get_state",
        callback: (r) => {
            const state = r.message || {};
            if (state.installed && state.route) {
                // Recon app is installed → redirect to its workspace.
                frappe.set_route(state.route.replace(/^\/app\//, ""));
                return;
            }
            render_upsell(page);
        },
        error: () => {
            // If the API call itself fails, still show the upsell
            // rather than a blank page. Better UX than an error toast.
            render_upsell(page);
        },
    });
};

function render_upsell(page) {
    const is_system_manager = (frappe.user_roles || []).includes("System Manager");

    const cta_button = is_system_manager
        ? `<button class="btn btn-primary btn-sm" id="recon-request-activation">
             ${__("Request Activation")}
           </button>`
        : "";

    const html = `
        <div style="max-width: 720px; margin: 40px auto; padding: 24px;
                    background: #ffffff; border: 1px solid #e5e7eb;
                    border-radius: 12px;">
            <h2 style="font-size: 22px; margin: 0 0 12px; color: #111827;">
                ${__("Marketplace Reconciliation")}
            </h2>
            <p style="font-size: 14px; line-height: 1.6; color: #374151; margin: 0 0 16px;">
                ${__(
                    "Marketplace sellers typically lose 0.5–1.5% of GMV to " +
                    "unreconciled deductions — commission overcharges, missing " +
                    "refund reversals, and returns never received."
                )}
            </p>
            <ul style="font-size: 14px; line-height: 1.7; color: #374151;
                        margin: 0 0 20px; padding-left: 20px;">
                <li>${__("Settlement file ingestion &amp; validation")}</li>
                <li>${__("Expected-vs-actual per order (commission, fees, TDS/TCS)")}</li>
                <li>${__("Claim-ready diff reports")}</li>
            </ul>
            ${cta_button}
        </div>
    `;

    $(page.body).html(html);

    if (is_system_manager) {
        $("#recon-request-activation").on("click", () => {
            frappe.msgprint({
                title: __("Request Activation"),
                message: __(
                    "Contact Incirco to enable the Recon module for this site."
                ),
                indicator: "blue",
            });
        });
    }
}
