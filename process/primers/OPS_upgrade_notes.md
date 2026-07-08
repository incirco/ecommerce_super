# Third-party upgrade notes

Reference file — when a Frappe or ERPNext upgrade ships a breaking change on a *bench-shared* app (i.e. an app that runs on the same bench as `ecommerce_super`), the mitigation note lives here so an FDE isn't left guessing "what did the upgrade break?"

These are **not** `ecommerce_super` bugs. They're one-line ops actions that a System Manager needs to take after upgrading a client's bench. The reason we track them here (rather than trust the FDE will read every Frappe / ERPNext release note) is that our app runs alongside CRM / HR / etc. on the same site, and a breaking change on one of those looks — from the FDE's dashboard — like "the integration stopped working."

If you upgraded a bench and a downstream feature stopped working, scan the list below first. If it's not covered, the upgrade probably didn't break `ecommerce_super` either — look at the release notes of whichever specific app went unusually quiet.

---

## Frappe CRM — data sync disabled by default (Frappe June 2026)

**Source:** [Frappe June 2026 product updates](https://frappe.io/blog/product-updates/product-updates-for-june-2026) — CRM Settings section, flagged as a behavior change.

**What Frappe changed:** CRM Settings gained an explicit "Frappe CRM" section. CRM → ERPNext data sync is now **disabled by default**, and System Managers must whitelist specific users before sync will fire again. Existing setups continue to look installed but stop moving data.

**Whether this affects `ecommerce_super`:** No — our app has no dependency on Frappe CRM. The gotcha is that on a bench that runs *both* CRM and `ecommerce_super`, an FDE debugging "why did customer data stop flowing after the upgrade" would reasonably check us first. Rule this out fast.

**Ops action after a post-June-2026 upgrade on a bench that runs Frappe CRM:**

1. Open Desk → CRM Settings.
2. Locate the "Frappe CRM" section.
3. Enable the sync toggle.
4. Whitelist the users who should be able to sync data.

If the bench does not run Frappe CRM, this note is a no-op — skip it.

---

*(add future third-party breaking-change entries here, newest first, with a source link + one-paragraph description of the ops action)*
