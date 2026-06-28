"""EasyEcom GSP Token controller — minimal.

The DocType is essentially a passive store; all logic lives in
flows/b2b_sales/gsp_auth.py (mint, validate, cleanup). This file
exists to satisfy Frappe's DocType controller convention.
"""

from __future__ import annotations

from frappe.model.document import Document


class EasyEcomGSPToken(Document):
    pass
