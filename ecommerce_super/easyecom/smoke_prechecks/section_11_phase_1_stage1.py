"""Pre-flight check for the §11 Stage 2 live smoke on smoke-test.local.

Inspects: EE Account state, Live EE Locations + their warehouses,
any existing Customer/Item Maps that could be used as smoke fixtures,
and any §11 Custom Fields landed via the Stage 1 patch.
"""
import frappe


def check() -> dict:
    out: dict = {}

    accounts = frappe.db.get_all(
        "EasyEcom Account",
        fields=[
            "name",
            "enabled",
            "auto_push_pos_on_save",
            "ecs_b2b_module",
        ],
    )
    out["accounts"] = [str(r) for r in accounts]

    live_locations = frappe.db.get_all(
        "EasyEcom Location",
        filters={"workflow_state": "Live", "enabled": 1},
        fields=[
            "name",
            "location_key",
            "location_name",
            "mapped_warehouse",
            "frappe_company",
            "ee_company_id",
        ],
        limit=10,
    )
    out["live_locations"] = [str(r) for r in live_locations]

    # Existing mapped Customers for B2B smoke.
    mapped_customers = frappe.db.get_all(
        "EasyEcom Customer Map",
        filters={"erpnext_doctype": "Customer", "status": "Mapped"},
        fields=["erpnext_name", "ee_customer_id", "ee_c_id"],
        limit=5,
    )
    out["mapped_customers"] = [str(r) for r in mapped_customers]

    # Existing mapped Items.
    mapped_items = frappe.db.get_all(
        "EasyEcom Item Map",
        filters={"erpnext_doctype": "Item", "status": "Mapped"},
        fields=["erpnext_name", "ee_sku", "ee_product_id"],
        limit=5,
    )
    out["mapped_items"] = [str(r) for r in mapped_items]

    # §11 Stage 1 Custom Fields on Sales Order + EE Account.
    out["s11_custom_fields"] = {}
    for dt, fname in [
        ("EasyEcom Account", "ecs_b2b_module"),
        ("EasyEcom Account", "ecs_eway_origination"),
        ("Sales Order", "ecs_b2b_order_map"),
    ]:
        out["s11_custom_fields"][f"{dt}.{fname}"] = {
            "row": bool(
                frappe.db.exists(
                    "Custom Field", {"dt": dt, "fieldname": fname}
                )
            ),
            "column": bool(frappe.db.has_column(dt, fname)),
        }

    # Has the §11 DocType + table landed?
    out["b2b_order_map"] = {
        "doctype_exists": bool(
            frappe.db.exists("DocType", "EasyEcom B2B Order Map")
        ),
        "table_has_name": bool(
            frappe.db.has_column("EasyEcom B2B Order Map", "name")
        ),
    }

    return out
