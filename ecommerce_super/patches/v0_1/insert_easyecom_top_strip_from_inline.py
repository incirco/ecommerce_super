"""Insert the EasyEcom Top Strip Custom HTML Block from EMBEDDED
content (gh#3 follow-up #2).

The two earlier patches (`install_easyecom_top_strip_block` and
`refresh_easyecom_top_strip_block`) both read the block's content from
the on-disk JSON fixture and have a defensive `if not json_path.exists():
return` early-return. The intent was a clean degrade-quietly path —
but it has the side-effect that a deployment which DIDN'T bundle the
JSON fixture file silently records both patches as "executed" in
Patch Log without actually creating the block.

Confirmed live 2026-06-11 on `mmpl16` (Frappe Cloud UAT): both prior
patches appear in `tabPatch Log` but `Custom HTML Block / EasyEcom
Top Strip` doesn't exist. The reporter (garv999) couldn't run the
JSON-file-reachability diagnostic because Frappe Cloud's System
Console (safe_exec) blocks `__import__` and `Path`. So a sync-side
patch that doesn't depend on the on-disk JSON at all is the
guaranteed-recoverable fix.

This patch:
  1. Embeds the block's render payload (html / script / style)
     directly as Python triple-quoted strings — no file I/O.
  2. Inserts the block when it's missing.
  3. When the block already exists with empty/None html/script,
     refreshes the render payload from the embedded copy. Sites where
     the prior patches succeeded (or `bench migrate` planted the row
     via the fixture loader) end up no-op'd.
  4. Idempotent: re-runs leave correctly-populated rows untouched.

The on-disk JSON fixture stays as the canonical source of truth for
future content edits — when the FDE team adjusts the strip, they
edit the JSON. This patch is just the rescue path for deployments
where the JSON didn't reach the bench.
"""

from __future__ import annotations

import frappe


CUSTOM_BLOCK_NAME = "EasyEcom Top Strip"
MODULE = "EasyEcom"

# Render payload — embedded inline. Mirrors
# easyecom/custom_html_block/easyecom_top_strip/easyecom_top_strip.json
# byte-for-byte. If you edit the JSON, sync this patch too — they are
# two copies of the same truth (the JSON wins on fresh installs that
# can find it; this patch wins on deployments that can't).

_HTML = """<div id="easyecom-top-strip" class="ecs-top-strip">
  <div class="ecs-tile" data-key="env">
    <div class="ecs-tile-label">Environment</div>
    <div class="ecs-tile-value" data-bind="env">…</div>
  </div>
  <div class="ecs-tile" data-key="connection">
    <div class="ecs-tile-label">Connection</div>
    <div class="ecs-tile-value" data-bind="connection">…</div>
  </div>
  <div class="ecs-tile ecs-tile-action" data-key="pause">
    <div class="ecs-tile-label">Account State</div>
    <button class="btn btn-sm" id="ecs-pause-toggle" disabled>Loading…</button>
  </div>
  <div class="ecs-tile-empty">
    <div class="ecs-tile-label">No Account configured</div>
    <div class="ecs-tile-hint text-muted">Create an EasyEcom Account to enable this strip.</div>
  </div>
</div>"""

_SCRIPT = """// §17.2.1 Top Strip — runs inside the shadow DOM Frappe wraps Custom
// HTML Blocks with. `root_element` is the shadow root, injected by
// frappe.create_shadow_element's script-wrapper. DOM lookups must
// scope to root_element; document.* would never see the shadow-DOM
// nodes.
const root = root_element.querySelector('.ecs-top-strip');
if (root) {
  const fields = ['name','environment_badge','connection_status','enabled'];
  frappe.call({
    method: 'frappe.client.get_list',
    args: {doctype: 'EasyEcom Account', filters: {enabled: 1}, fields: fields, limit_page_length: 1},
    callback: function(r){
      const accounts = (r && r.message) || [];
      const acct = accounts[0];
      const emptyEl = root.querySelector('.ecs-tile-empty');
      const tiles = root.querySelectorAll('.ecs-tile:not(.ecs-tile-empty)');
      if (!acct) {
        tiles.forEach(t => t.style.display = 'none');
        if (emptyEl) emptyEl.style.display = 'block';
        return;
      }
      if (emptyEl) emptyEl.style.display = 'none';
      tiles.forEach(t => t.style.display = 'block');
      const envEl = root.querySelector('[data-bind=env]');
      envEl.textContent = acct.environment_badge || 'Unknown';
      envEl.parentElement.classList.toggle('ecs-env-sandbox', acct.environment_badge === 'Sandbox');
      envEl.parentElement.classList.toggle('ecs-env-production', acct.environment_badge === 'Production');
      const connEl = root.querySelector('[data-bind=connection]');
      connEl.textContent = acct.connection_status || 'Unknown';
      connEl.parentElement.classList.remove('ecs-conn-connected','ecs-conn-degraded','ecs-conn-down','ecs-conn-disabled');
      connEl.parentElement.classList.add('ecs-conn-' + String(acct.connection_status || 'unknown').toLowerCase());
      const btn = root.querySelector('#ecs-pause-toggle');
      btn.disabled = false;
      btn.textContent = acct.enabled ? 'Pause All Syncs' : 'Resume All Syncs';
      btn.className = 'btn btn-sm ' + (acct.enabled ? 'btn-warning' : 'btn-primary');
      btn.onclick = function(){
        frappe.confirm(
          (acct.enabled ? 'Pause' : 'Resume') + ' ALL EasyEcom syncs?',
          function(){
            frappe.call({
              method: 'frappe.client.set_value',
              args: {doctype: 'EasyEcom Account', name: acct.name, fieldname: 'enabled', value: acct.enabled ? 0 : 1},
              callback: function(){ window.location.reload(); }
            });
          }
        );
      };
    }
  });
}"""

_STYLE = """.ecs-top-strip { display: flex; gap: 12px; padding: 12px; flex-wrap: wrap; }
.ecs-tile { background: var(--bg-color, #fff); border: 1px solid var(--border-color, #e0e0e0); border-radius: 8px; padding: 14px 18px; min-width: 180px; flex: 1; }
.ecs-tile-label { font-size: 11px; text-transform: uppercase; color: #888; letter-spacing: 0.5px; }
.ecs-tile-value { font-size: 18px; font-weight: 600; margin-top: 4px; }
.ecs-env-sandbox .ecs-tile-value { color: #d97706; }
.ecs-env-production .ecs-tile-value { color: #059669; }
.ecs-conn-connected .ecs-tile-value { color: #059669; }
.ecs-conn-degraded .ecs-tile-value { color: #d97706; }
.ecs-conn-down .ecs-tile-value { color: #dc2626; }
.ecs-conn-disabled .ecs-tile-value { color: #6b7280; }
.ecs-tile-action button { margin-top: 4px; }
.ecs-tile-empty { display: none; background: #f9fafb; border: 1px dashed #e0e0e0; border-radius: 8px; padding: 14px 18px; flex: 1; }
.ecs-tile-hint { font-size: 12px; margin-top: 4px; }"""


def execute() -> None:
    if not frappe.db.table_exists("Custom HTML Block"):
        # Frappe v15+ ships this DocType; on rare pre-v15 sites it
        # may be absent. Degrade quietly.
        return

    payload = {
        "html": _HTML,
        "script": _SCRIPT,
        "style": _STYLE,
        "is_standard": 1,
        "module": MODULE,
        "private": 0,
    }

    if frappe.db.exists("Custom HTML Block", CUSTOM_BLOCK_NAME):
        # Heal-only: if the existing row has empty html/script, refresh
        # the render payload. Otherwise no-op — leave any FDE edits
        # alone.
        existing = frappe.get_doc("Custom HTML Block", CUSTOM_BLOCK_NAME)
        needs_heal = not (existing.html and existing.script)
        if needs_heal:
            for field, value in payload.items():
                existing.set(field, value)
            existing.save(ignore_permissions=True)
            frappe.db.commit()
            print(
                f"[ecommerce_super] gh#3 follow-up: healed empty "
                f"{CUSTOM_BLOCK_NAME!r} from embedded content"
            )
        return

    # Block doesn't exist — insert fresh from embedded content.
    doc = frappe.new_doc("Custom HTML Block")
    doc.update({"name": CUSTOM_BLOCK_NAME, **payload})
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    print(
        f"[ecommerce_super] gh#3 follow-up: inserted "
        f"{CUSTOM_BLOCK_NAME!r} from embedded content"
    )
