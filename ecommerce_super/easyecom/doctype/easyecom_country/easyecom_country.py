"""EasyEcom Country — foundational reference cache for /getCountries.

SPEC §8.2 / §7.7. One row per EE country id. Pure reference data: the
8e push (CreateCustomer) needs the canonical country NAME for its
`country` field, and the country id is the join key for /getStates.

Read-only on the form. Populated/refreshed only by the discover flow
(easyecom.flows.customer_lookups.pull_countries_and_states). FDE-side
edits would drift the cache from EE's authoritative state; the read-
only fields make accidental edits impossible without role escalation.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomCountry(Document):
    pass
