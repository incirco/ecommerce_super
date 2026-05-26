"""EasyEcom Drift Field — child table of EasyEcom Item Map.

One row per ERPNext field that the post-flip drift detector found to
differ between the EE payload and the current ERPNext doc. Surfaces
to the FDE on the Item Map form as a structured table (sortable /
filterable) rather than a `||`-delimited Data string. Populated and
cleared by `item_pull._detect_drift_one_product` whenever a re-pull
finds (or no longer finds) divergence.

Component-set drift on a Product Bundle is recorded as a single
row with field='combo_sub_products' and the two values rendered as
sorted (ee_sku, qty) tuples — keeps the same table shape for the
FDE to consume.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomDriftField(Document):
    pass
