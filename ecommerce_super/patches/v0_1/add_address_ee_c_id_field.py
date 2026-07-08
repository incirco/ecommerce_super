"""gh#126 — Custom Field on Address so a §8e-created Address can
carry the originating EE c_id.

When gh#126 dedup wires a new EE c_id onto an existing (canonical)
Customer, the incoming Billing / Shipping addresses land on that
canonical. To keep the reverse lookup crisp ("which addresses came
from which EE c_id?") we tag each such address with `ecs_ee_c_id`.

Not-mandatory, not-search-index. Read-only on the form so an FDE
doesn't accidentally clobber the c_id linkage.

Idempotent per the create_custom_fields contract.
"""
from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "Address": [
                {
                    "fieldname": "ecs_ee_c_id",
                    "label": "EE c_id (source)",
                    "fieldtype": "Data",
                    "insert_after": "ecs_ee_location",
                    "read_only": 1,
                    "description": (
                        "EasyEcom customer identifier (c_id) that this "
                        "Address was originally pulled from. Populated "
                        "by §8e customer_pull when the Alias-dedup path "
                        "wires a new EE c_id onto an existing canonical "
                        "Customer (gh#126). Empty on Addresses created "
                        "by other flows or on the first EE c_id for a "
                        "given canonical (that c_id lives on the "
                        "Customer Map row directly)."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
