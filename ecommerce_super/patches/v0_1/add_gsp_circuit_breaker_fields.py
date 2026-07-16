"""gh#152 — add circuit-breaker state fields to EasyEcom Account
so state survives restarts (Redis cache alone wouldn't).

Fields on EasyEcom Account:
  - ecs_gsp_circuit_state (Select): Closed | Open | Half-Open.
    Default Closed. Set by the circuit breaker on transitions.
  - ecs_gsp_circuit_opened_at (Datetime): timestamp of the last
    Closed → Open transition. NULL when Closed. Used by cooldown
    check to know when Open → Half-Open is due.

Idempotent per create_custom_fields.
"""
from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    create_custom_fields(
        {
            "EasyEcom Account": [
                {
                    "fieldname": "gsp_circuit_section",
                    "label": "Custom GSP — Circuit Breaker",
                    "fieldtype": "Section Break",
                    "insert_after": "gsp_rate_limit_per_min",
                    "collapsible": 1,
                    "collapsible_depends_on": (
                        "eval:doc.ecs_gsp_circuit_state && "
                        "doc.ecs_gsp_circuit_state !== 'Closed'"
                    ),
                    "description": (
                        "gh#152 — pauses outbound polling when our "
                        "inbound handlers are failing so we don't "
                        "amplify pressure on EE during our own outage. "
                        "Read-only from the FDE's side (the breaker "
                        "manages both fields)."
                    ),
                },
                {
                    "fieldname": "ecs_gsp_circuit_state",
                    "label": "GSP Circuit State",
                    "fieldtype": "Select",
                    "options": "Closed\nOpen\nHalf-Open",
                    "default": "Closed",
                    "read_only": 1,
                    "insert_after": "gsp_circuit_section",
                    "description": (
                        "Closed = polling runs normally. "
                        "Open = polling paused (inbound is failing). "
                        "Half-Open = single probe permitted after cooldown."
                    ),
                },
                {
                    "fieldname": "ecs_gsp_circuit_opened_at",
                    "label": "GSP Circuit Opened At",
                    "fieldtype": "Datetime",
                    "read_only": 1,
                    "insert_after": "ecs_gsp_circuit_state",
                    "description": (
                        "Timestamp of the last Closed → Open transition. "
                        "NULL when circuit is Closed. Used to compute the "
                        "cooldown expiry (Open + 15 min → Half-Open)."
                    ),
                },
            ],
        }
    )
