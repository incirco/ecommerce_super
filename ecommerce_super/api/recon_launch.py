"""Dispatcher API for the Recon apps-screen tile (see #246).

The Recon icon on the v16 apps-screen folder routes to /app/recon-launch,
whose JS calls `get_state()` here to decide:

  - If the recon app (easyecom_recon) is installed on this bench, return
    its workspace route so the JS redirects there immediately.
  - Otherwise, return installed=False; the JS renders an upsell page.

RECON_APP_NAME is the SINGLE rename point — if the recon app ships
under a different Frappe app name, change it here only.
"""
from __future__ import annotations

import frappe


# Single rename point. If the recon app is published under a different
# Frappe app name, change this one constant — every reference below
# reads from it.
RECON_APP_NAME: str = "easyecom_recon"

# The recon app must ship a Workspace whose route resolves to this URL
# (contract fixed in #246). We hard-code it here so the redirect works
# the instant the app is installed, without depending on any config
# lookup or DocType read.
RECON_WORKSPACE_ROUTE: str = "/app/ecommerce-super-recon"


@frappe.whitelist()
def get_state() -> dict:
    """Return the recon app's installation state + target route.

    Called by the recon-launch Page's JS on load. No side effects,
    read-only.
    """
    try:
        installed_apps = frappe.get_installed_apps() or []
    except Exception:
        # Defensive: if the installed-apps lookup fails, fall through
        # to the not-installed branch. Never crash the dispatcher.
        installed_apps = []

    return {
        "installed": RECON_APP_NAME in installed_apps,
        "route": RECON_WORKSPACE_ROUTE,
    }
