"""Persistent setup for the eCommerce Super apps-screen folder (see #246).

Runs on `after_migrate` so that bench migrate's "Removing orphan Desktop
Icons" pass doesn't wipe our folder tile, and the workspace-generated
child icons get their `parent_icon` back-linked automatically.

Responsibilities:

  1. Ensure the parent Folder Desktop Icon "eCommerce Super" exists
     (with `icon_type=Folder`, `link_type=Workspace Sidebar`) — this is
     the same shape ERPNext uses for its "Accounting" folder.

  2. Ensure the Workspace Sidebar "Recon" carries at least one real
     Link item. Frappe's Desktop Icon permission check
     (`desktop_icon.py:is_permitted`) filters out workspace-linked
     icons whose sidebar has no items (or only Section Breaks); adding
     the Marketplace Reconciliation link makes the Recon child icon
     visible in the folder modal.

  3. Re-link the EasyEcom + Recon Desktop Icons back under the parent
     via `parent_icon`. Frappe's `create_desktop_icons_from_workspace`
     auto-generates the child icons on migrate but only sets
     parent_icon when it can match `label == app_title AND
     icon_type == "App"` — our parent is a Folder, so the auto-link
     misses; this hook restores it.

  4. Clear bootinfo + desktop_icons caches so the browser picks up
     the updated state on next reload.

Idempotent — every step checks existing state before writing.
"""
from __future__ import annotations

import frappe


PARENT_FOLDER_LABEL = "eCommerce Super"
CHILD_LABELS = ("EasyEcom", "Recon")
RECON_WORKSPACE = "Recon"
RECON_LAUNCH_PAGE = "recon-launch"


def setup_apps_screen_folder() -> None:
    """Idempotent setup — safe to run on every after_migrate."""
    _ensure_parent_folder_icon()
    _ensure_recon_sidebar_has_link()
    _relink_children_to_parent()
    _clear_desktop_icon_caches()


def _ensure_parent_folder_icon() -> None:
    """Create the parent Folder Desktop Icon if it doesn't exist.

    Matches ERPNext "Accounting" folder shape exactly:
      icon_type=Folder, link_type=Workspace Sidebar,
      no link, no link_to, no logo_url.
    """
    if frappe.db.exists("Desktop Icon", PARENT_FOLDER_LABEL):
        return
    icon = frappe.new_doc("Desktop Icon")
    icon.label = PARENT_FOLDER_LABEL
    icon.icon_type = "Folder"
    icon.link_type = "Workspace Sidebar"
    icon.app = "ecommerce_super"
    icon.standard = 1
    icon.idx = 50
    icon.insert(ignore_permissions=True)


def _ensure_recon_sidebar_has_link() -> None:
    """Recon's Workspace Sidebar record must carry at least one Link
    item (that isn't a Section Break) for the Desktop Icon permission
    check to admit it into the folder modal.

    Creates the Workspace Sidebar record if missing (bench migrate
    doesn't auto-create it for workspaces installed via JSON) and
    appends the Marketplace Reconciliation Link if the sidebar has
    zero real items.
    """
    if not frappe.db.exists("Workspace Sidebar", RECON_WORKSPACE):
        sb = frappe.new_doc("Workspace Sidebar")
        sb.title = RECON_WORKSPACE
        sb.insert(ignore_permissions=True)

    sb = frappe.get_doc("Workspace Sidebar", RECON_WORKSPACE)
    real_items = [
        it for it in (sb.items or [])
        if (it.type or "") != "Section Break"
    ]
    if real_items:
        return  # already has a real item — nothing to do

    sb.append("items", {
        "label": "Marketplace Reconciliation",
        "link_to": RECON_LAUNCH_PAGE,
        "link_type": "Page",
        "type": "Link",
    })
    sb.save(ignore_permissions=True)


def _relink_children_to_parent() -> None:
    """Frappe's `create_desktop_icons_from_workspace` auto-generates
    Desktop Icons for our EasyEcom + Recon workspaces on every migrate,
    but only fills `parent_icon` when the parent's icon_type is "App"
    (see `desktop_icon.py:253`). Our parent is a Folder — so we
    re-link the children manually here.

    Also stamps the app-specific logos back on the children so they
    render with the EasyEcom pyramid + Recon indigo icon in the
    folder modal.
    """
    logo_by_label = {
        "EasyEcom": "/assets/ecommerce_super/images/easyecom-logo.png",
        "Recon":    "/assets/ecommerce_super/images/recon-icon.svg",
    }
    for label in CHILD_LABELS:
        if not frappe.db.exists("Desktop Icon", label):
            continue
        updates: dict = {}
        current_parent = frappe.db.get_value(
            "Desktop Icon", label, "parent_icon"
        )
        if current_parent != PARENT_FOLDER_LABEL:
            updates["parent_icon"] = PARENT_FOLDER_LABEL
        current_logo = frappe.db.get_value(
            "Desktop Icon", label, "logo_url"
        )
        if not current_logo:
            updates["logo_url"] = logo_by_label[label]
        if updates:
            frappe.db.set_value(
                "Desktop Icon", label, updates, update_modified=False,
            )


def _clear_desktop_icon_caches() -> None:
    """Bootinfo + desktop_icons are cached per-user in redis. Cleared
    so the browser pulls fresh state on next reload."""
    try:
        frappe.cache.delete_key("bootinfo")
        frappe.cache.delete_key("desktop_icons")
    except Exception:
        pass
