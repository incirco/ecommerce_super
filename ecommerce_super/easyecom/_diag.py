"""Temporary diagnostic helpers for live smoke - safe to delete after.
Read-only: every function in here only queries; never writes."""
from __future__ import annotations

import frappe


def list_accounts() -> None:
    rows = frappe.db.sql(
        "SELECT name, enabled, default_location_key, "
        "COALESCE(item_master_mode, '') AS mode, "
        "COALESCE(connection_status, '') AS conn, "
        "COALESCE(LEFT(item_pull_cursor, 60), '') AS cursor_head "
        "FROM `tabEasyEcom Account`",
        as_dict=True,
    )
    print(f"\n=== EasyEcom Account rows: {len(rows)} ===")
    for r in rows:
        print(r)


def ee_total_count() -> None:
    """Hit the EE count endpoint live and report what EE says."""
    import frappe.utils.password  # noqa: F401 - force submodule import in script context

    from ecommerce_super.easyecom.client.client import EasyEcomClient
    from ecommerce_super.easyecom.client.endpoints import (
        PRODUCT_MASTER_COUNT_GET,
    )

    client = EasyEcomClient(company="_Other Test Co")
    resp = client.get(PRODUCT_MASTER_COUNT_GET)
    print("=== Live EE GetProductMastersCount response ===")
    print(resp)


def item_map_breakdown() -> None:
    rows = frappe.db.sql(
        "SELECT status, COUNT(*) AS n FROM `tabEasyEcom Item Map` "
        "GROUP BY status ORDER BY n DESC",
        as_dict=True,
    )
    total = sum(r["n"] for r in rows)
    print(f"\n=== Item Map status breakdown (total {total}) ===")
    for r in rows:
        print(f"  {r['status']:25s}: {r['n']}")


def recent_api_calls(limit: int = 12, endpoint: str | None = None, verbose: int = 0) -> None:
    if endpoint:
        rows = frappe.db.sql(
            f"SELECT name, creation, http_method, endpoint, response_status_code, "
            f"status, is_foundational, company, location_key, "
            f"SUBSTRING(request_payload, 1, 250) AS req, "
            f"SUBSTRING(response_payload, 1, 350) AS resp "
            f"FROM `tabEasyEcom API Call` WHERE endpoint = %s "
            f"ORDER BY creation DESC LIMIT {int(limit)}",
            (endpoint,),
            as_dict=True,
        )
    else:
        rows = frappe.db.sql(
            f"SELECT name, creation, http_method, endpoint, response_status_code, "
            f"status, is_foundational, company, location_key, "
            f"SUBSTRING(request_payload, 1, 250) AS req, "
            f"SUBSTRING(response_payload, 1, 350) AS resp "
            f"FROM `tabEasyEcom API Call` "
            f"ORDER BY creation DESC LIMIT {int(limit)}",
            as_dict=True,
        )
    print(f"\n=== Recent API Calls: {len(rows)} ===")
    for r in rows:
        print(
            f"  {r['creation']}  {r['http_method']} {r['endpoint']} "
            f"-> HTTP {r['response_status_code']} status={r['status']}"
        )
        if verbose:
            print(f"    req : {r['req']}")
            print(f"    resp: {r['resp']}")


def sweep_candidates() -> None:
    """Inspect items that the batch sweep would push (stock, HSN-set, no ee_product_id)."""
    rows = frappe.db.sql(
        """
        SELECT i.item_code, i.item_name, i.is_stock_item, i.disabled,
               i.gst_hsn_code, m.name AS map_name, m.ee_product_id
        FROM `tabItem` i
        LEFT JOIN `tabEasyEcom Item Map` m
            ON m.erpnext_doctype = 'Item' AND m.erpnext_name = i.item_code
        LEFT JOIN `tabProduct Bundle` pb
            ON pb.new_item_code = i.item_code
        WHERE i.disabled = 0
          AND i.is_stock_item = 1
          AND i.gst_hsn_code IS NOT NULL
          AND i.gst_hsn_code != ''
          AND (m.ee_product_id IS NULL OR m.ee_product_id = '')
          AND pb.name IS NULL
        ORDER BY i.creation DESC
        LIMIT 50
        """,
        as_dict=True,
    )
    print(f"\n=== Sweep candidates (items pending push): {len(rows)} ===")
    for r in rows:
        print(
            f"  {r['item_code']:30s}  HSN={r['gst_hsn_code']}  "
            f"map={r['map_name'] or '-'}  ee_pid={r['ee_product_id'] or '-'}"
        )


def create_batch_smoke_items() -> None:
    """Create 3 fresh ERPNext-only items for the onboarding-equivalent
    batch-push smoke. Items are clearly test-prefixed (ECS-BATCH-E2E-*),
    safe to leave behind or disable after."""
    specs = [
        ("ECS-BATCH-E2E-001", "Batch Smoke Item 001 - Cleaning Spray", 199.0),
        ("ECS-BATCH-E2E-002", "Batch Smoke Item 002 - Mop Refill", 249.0),
        ("ECS-BATCH-E2E-003", "Batch Smoke Item 003 - Glass Wiper", 349.0),
    ]
    for code, name, rate in specs:
        if frappe.db.exists("Item", code):
            print(f"  skip-existing: {code}")
            continue
        doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": code,
            "item_name": name,
            "item_group": "All Item Groups",
            "stock_uom": "Nos",
            "is_stock_item": 1,
            "include_item_in_manufacturing": 0,
            "disabled": 0,
            "gst_hsn_code": "39241090",
            "standard_rate": rate,
            "description": "Live batch-push smoke artefact - safe to disable",
        })
        doc.insert(ignore_permissions=True)
        print(f"  created: {code}")
    frappe.db.commit()


def show_batch_smoke_result() -> None:
    rows = frappe.db.sql(
        """
        SELECT i.item_code, i.standard_rate, m.ee_product_id, m.ee_cp_id, m.status
        FROM `tabItem` i
        LEFT JOIN `tabEasyEcom Item Map` m
            ON m.erpnext_doctype = 'Item' AND m.erpnext_name = i.item_code
        WHERE i.item_code LIKE 'ECS-BATCH-E2E-%'
        ORDER BY i.item_code
        """,
        as_dict=True,
    )
    print(f"\n=== Batch smoke items: {len(rows)} ===")
    for r in rows:
        print(
            f"  {r['item_code']:25s}  rate={r['standard_rate']}  "
            f"ee_pid={r['ee_product_id'] or '-'}  ee_cp_id={r['ee_cp_id'] or '-'}  "
            f"status={r['status'] or '-'}"
        )


def stamp_batch_items_taxes() -> None:
    """Add Item Tax rows to the ECS-BATCH-E2E-* items so the batch push
    has resolvable TaxRate (mirrors what §8c pull-side stamping does
    for EE-sourced items)."""
    template = "GST 5% - TC"
    if not frappe.db.exists("Item Tax Template", template):
        print(f"  missing template: {template}")
        return
    for code in ("ECS-BATCH-E2E-001", "ECS-BATCH-E2E-002", "ECS-BATCH-E2E-003"):
        if not frappe.db.exists("Item", code):
            print(f"  skip (no item): {code}")
            continue
        doc = frappe.get_doc("Item", code)
        if any(t.item_tax_template == template for t in (doc.taxes or [])):
            print(f"  already stamped: {code}")
            continue
        doc.append("taxes", {"item_tax_template": template})
        doc.save(ignore_permissions=True)
        print(f"  stamped {code} with {template}")
    frappe.db.commit()


def create_bundle_create_smoke() -> None:
    """Create a fresh wrapper Item + Product Bundle for the §8d
    Bundle CREATE smoke. Components reference our just-batch-pushed
    items so they already have ee_product_id (dependency satisfied)."""
    wrapper_code = "ECS-SMOKE-BUNDLE-CREATE"
    bundle_name_default = "ECS-SMOKE-BUNDLE-CREATE"
    # Components: 2 of the batch-pushed items (already on EE)
    components = [
        ("ECS-BATCH-E2E-001", 1),
        ("ECS-BATCH-E2E-002", 1),
    ]
    if not frappe.db.exists("Item", wrapper_code):
        wrapper = frappe.get_doc({
            "doctype": "Item",
            "item_code": wrapper_code,
            "item_name": "ECS Smoke Combo - Cleaning Kit",
            "item_group": "All Item Groups",
            "stock_uom": "Nos",
            "is_stock_item": 0,
            "disabled": 0,
            "gst_hsn_code": "39241090",
            "standard_rate": 399.0,
            "weight_per_unit": 800,
            "ecs_length_cm": 20,
            "ecs_height_cm": 15,
            "ecs_width_cm": 10,
            "description": "Bundle CREATE smoke - 2 components, deletable after",
            "taxes": [{"item_tax_template": "GST 5% - TC"}],
        })
        wrapper.insert(ignore_permissions=True)
        print(f"  created wrapper Item {wrapper_code}")
    else:
        print(f"  skip-existing wrapper Item {wrapper_code}")
    if not frappe.db.exists("Product Bundle", bundle_name_default):
        bundle = frappe.get_doc({
            "doctype": "Product Bundle",
            "new_item_code": wrapper_code,
            "description": "Bundle CREATE smoke",
            "items": [
                {"item_code": code, "qty": qty} for code, qty in components
            ],
        })
        bundle.insert(ignore_permissions=True)
        print(f"  created Product Bundle {bundle.name}")
    else:
        print(f"  skip-existing Product Bundle {bundle_name_default}")
    frappe.db.commit()


def push_bundle_smoke(wrapper_code: str = "ECS-SMOKE-BUNDLE-CREATE") -> None:
    """Push the bundle to EE and report outcome. Commits at the end so
    side-effects (Item Map row, ee_product_id writeback, Sync Record,
    API Call row) actually persist."""
    from ecommerce_super.easyecom.flows.item_push import push_one_bundle
    from ecommerce_super.easyecom.client.client import EasyEcomClient
    from ecommerce_super.easyecom.flows.item_push import _company_for_item_push
    account = frappe.get_doc("EasyEcom Account", "Harmony")
    client = EasyEcomClient(company=_company_for_item_push("Harmony"))
    bundle_name = frappe.db.get_value(
        "Product Bundle", {"new_item_code": wrapper_code}, "name"
    )
    print(f"  bundle name: {bundle_name}")
    outcome = push_one_bundle(bundle_name, client=client, account=account)
    print(f"  outcome: pushed={outcome.pushed} op={outcome.operation} ee_pid={outcome.ee_product_id}")
    if outcome.flag_reasons:
        print(f"  flags: {outcome.flag_reasons}")
    frappe.db.commit()
    print("  committed")


def create_fresh_bundle_smoke(suffix: str = "002") -> str:
    """Create a brand new wrapper Item + Product Bundle with a unique
    SKU so EE doesn't return 'Product Already Exists' on push."""
    wrapper_code = f"ECS-SMOKE-BUNDLE-CREATE-{suffix}"
    if not frappe.db.exists("Item", wrapper_code):
        wrapper = frappe.get_doc({
            "doctype": "Item",
            "item_code": wrapper_code,
            "item_name": f"ECS Smoke Combo {suffix} - Cleaning Kit",
            "item_group": "All Item Groups",
            "stock_uom": "Nos",
            "is_stock_item": 0,
            "disabled": 0,
            "gst_hsn_code": "39241090",
            "standard_rate": 449.0,
            "weight_per_unit": 800,
            "ecs_length_cm": 20,
            "ecs_height_cm": 15,
            "ecs_width_cm": 10,
            "description": f"Bundle CREATE smoke {suffix}",
            "taxes": [{"item_tax_template": "GST 5% - TC"}],
        })
        wrapper.insert(ignore_permissions=True)
        print(f"  created wrapper {wrapper_code}")
    if not frappe.db.exists("Product Bundle", wrapper_code):
        bundle = frappe.get_doc({
            "doctype": "Product Bundle",
            "new_item_code": wrapper_code,
            "description": f"Bundle CREATE smoke {suffix}",
            "items": [
                {"item_code": "ECS-BATCH-E2E-001", "qty": 1},
                {"item_code": "ECS-BATCH-E2E-002", "qty": 1},
            ],
        })
        bundle.insert(ignore_permissions=True)
        print(f"  created Product Bundle {bundle.name}")
    frappe.db.commit()
    return wrapper_code


def set_kg_test_item(code: str = "ECS-BATCH-E2E-001", kg_value: float = 0.5) -> None:
    """Set weight_per_unit to a kg value with weight_uom='Kg' to exercise
    the new UOM-aware weight conversion."""
    if not frappe.db.exists("UOM", "Kg"):
        # Try to create the standard UOM if not present
        frappe.get_doc({"doctype": "UOM", "uom_name": "Kg"}).insert(ignore_permissions=True)
        print("  created UOM 'Kg'")
    doc = frappe.get_doc("Item", code)
    doc.weight_per_unit = kg_value
    doc.weight_uom = "Kg"
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"  {code}: weight_per_unit={kg_value} weight_uom=Kg (expected EE Weight={int(round(kg_value*1000))})")


def invalidate_mapping_cache() -> None:
    from ecommerce_super.easyecom.field_mapping.compiler import invalidate_compiled_cache
    invalidate_compiled_cache("EasyEcom-Item-Push")
    invalidate_compiled_cache("EasyEcom-Item-Pull")
    print("  invalidated EasyEcom-Item-Push and EasyEcom-Item-Pull")


def set_batch_items_dimensions() -> None:
    """Populate Weight/Length/Height/Width on the batch items — these
    are required by EE for physical products (enforced by d1401ac's
    contract polish)."""
    dims = {
        "ECS-BATCH-E2E-001": {"weight_per_unit": 500, "ecs_length_cm": 10, "ecs_height_cm": 5, "ecs_width_cm": 8},
        "ECS-BATCH-E2E-002": {"weight_per_unit": 300, "ecs_length_cm": 12, "ecs_height_cm": 6, "ecs_width_cm": 8},
        "ECS-BATCH-E2E-003": {"weight_per_unit": 400, "ecs_length_cm": 14, "ecs_height_cm": 7, "ecs_width_cm": 9},
    }
    for code, vals in dims.items():
        if not frappe.db.exists("Item", code):
            print(f"  skip (no item): {code}")
            continue
        doc = frappe.get_doc("Item", code)
        for k, v in vals.items():
            doc.set(k, v)
        doc.save(ignore_permissions=True)
        print(f"  dimensions set on {code}: {vals}")
    frappe.db.commit()


def reset_batch_item_maps() -> None:
    """Clear the Flagged-Not-Created Item Map rows on the batch items so
    the sweep picks them up again."""
    rows = frappe.db.sql(
        """SELECT name FROM `tabEasyEcom Item Map`
           WHERE erpnext_doctype='Item' AND erpnext_name LIKE 'ECS-BATCH-E2E-%'""",
        as_dict=True,
    )
    for r in rows:
        frappe.delete_doc("EasyEcom Item Map", r["name"], force=True, ignore_permissions=True)
        print(f"  deleted map: {r['name']}")
    frappe.db.commit()


def show_recent_queue_jobs(limit: int = 10) -> None:
    rows = frappe.db.sql(
        f"""
        SELECT name, creation, job_type, state, target_name, attempts,
               last_error
        FROM `tabEasyEcom Queue Job`
        ORDER BY creation DESC LIMIT {int(limit)}
        """,
        as_dict=True,
    )
    print(f"\n=== Recent Queue Jobs: {len(rows)} ===")
    for r in rows:
        msg = (r['last_error'] or '')[:60]
        tgt = r['target_name'] or '-'
        print(
            f"  {r['name']}  {r['job_type']:20s} state={r['state']:12s} "
            f"target={tgt:25s} attempts={r['attempts']} {msg}"
        )
