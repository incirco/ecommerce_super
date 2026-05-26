"""EasyEcom State — foundational reference cache for /getStates.

SPEC §8.2 / §7.7. One row per (state_id, country) pair. The write-side
of the §8.2 push (CreateCustomer) needs the state ID (int); the read
side returns the state name. Both are stored, plus the pincode-range
metadata for soft-flag pincode→state validation.

The resolver (easyecom.customer.state_resolver.resolve_state) uses a
case-insensitive lookup on (name, country_id) and prefers the LARGEST
state_id when a name appears multiple times — that handles EE's
legacy/merged duplicates (e.g. Daman & Diu vs the merged Dadra &
Nagar Haveli and Daman & Diu unit).

Read-only on the form; populated only by the discover flow.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomState(Document):
    pass
