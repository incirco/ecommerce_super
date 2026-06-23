"""EasyEcom REST endpoint URL constants.

Lifted from SPEC §31.3. The Account.api_endpoint is the base URL; these
are the path components. All authenticated endpoints carry the two
mandatory headers (`x-api-key` + `Authorization: Bearer <jwt>`).

New endpoints require updating this file AND adding the corresponding
method to EasyEcomClient (§31.4.1) AND writing a contract test (§28).
"""

from __future__ import annotations

# ----- Authentication (§31.3.1) -----

# POST — body: {"email": ..., "password": ..., "location_key": ...}
# Response: {"jwt": ..., "expires_in": ...}
TOKEN: str = "/access/token"


# ----- Master endpoints (§31.3.2) -----

ITEM_MASTER_UPLOAD: str = "/Wms/Inventory/itemMasterUpload"  # POST
ITEM_MASTER_GET: str = "/Wms/Inventory/getItemMaster"  # GET
CUSTOMER_CREATE: str = "/Customer/createCustomer"  # POST
CUSTOMER_GET: str = "/Customer/getCustomer"  # GET
VENDOR_CREATE: str = "/Wms/Vendor/createVendor"  # POST (legacy — unused)
VENDOR_GET: str = "/Wms/Vendor/getVendor"  # GET (legacy — unused)

# §8f Stage 3+ — Wholesale Vendor (supplier) master.
# /wms/V2/getVendors    → bulk list (flat data[], nextUrl pagination,
#                         created_after / updated_after / updated_before
#                         params for delta pull)
# /wms/CreateVendor     → POST — returns data.vendor_id (write key); body
#                         keys ee_vendor_c_id is NOT set on create
#                         (returned by EE later when the row is read back
#                         via getVendors)
# /wms/UpdateVendor     → POST — keys vendorId; sparse-update; state as
#                         NAME (not id); returns data.vendorId (the
#                         post-update id, observed as 58614 in the
#                         sample — open question per packet, confirm
#                         Stage 4)
# All three are account-wide (no per-location dimension) → foundational
# at the API Call layer. The flow still writes one Sync Record per
# supplier (entity-sync §7.3) for the bulk pull.
VENDORS_GET: str = "/wms/V2/getVendors"  # GET — foundational
WHOLESALE_VENDOR_CREATE: str = "/wms/CreateVendor"  # POST — foundational
WHOLESALE_VENDOR_UPDATE: str = "/wms/UpdateVendor"  # POST — foundational
LOCATIONS_GET: str = "/getAllLocation"  # GET — foundational (§7.7, §8.4.1)
CHANNELS_GET: str = "/current-channel-status"  # GET — per-location (§8.6.3, §8b)

# §8e Stage 2 — Customer-master foundational lookups. Reference data
# (countries + states), discovered-and-cached. Account-scoped — no
# company/location dimension. (§8.2 / §7.7.)
COUNTRIES_GET: str = "/getCountries"  # GET — foundational
STATES_GET: str = "/getStates"  # GET ?countryId=N — foundational

# §8e Stage 3+ — Wholesale Customer master.
# /Wholesale/v2/UserManagement → list (?type=b2b) AND single (?type=b2b&id=N)
#   - foundational at the API Call layer (account-wide; no Company tag)
#   - the FLOW still writes Sync Records per customer (entity-sync §7.3)
# Mirrors §8d Item Pull's "foundational endpoint + per-item Sync Record"
# split.
WHOLESALE_USER_MANAGEMENT: str = "/Wholesale/v2/UserManagement"  # GET — foundational

# §8e Stage 4 — Wholesale Customer push.
# /Wholesale/CreateCustomer → returns data.customerId (the write-side id).
# /Wholesale/UpdateCustomer → keys customerId; all-other-fields-optional;
#   state as NAME (not id, differs from create).
# Both account-wide writes → foundational at the API Call layer.
WHOLESALE_CUSTOMER_CREATE: str = "/Wholesale/CreateCustomer"  # POST — foundational
WHOLESALE_CUSTOMER_UPDATE: str = "/Wholesale/UpdateCustomer"  # POST — foundational

# §8d Item Master (Pull). Cursor-paginated via `nextUrl` (≤200/page);
# count-aware via PRODUCT_MASTER_COUNT_GET. Both are account-wide
# (includeLocations=1 → one catalogue keyed by globally-unique SKU,
# §8.1.4) so they're foundational (no Company tag on the API Call row).
PRODUCT_MASTER_GET: str = "/Products/GetProductMaster"  # GET (bulk; nextUrl)
PRODUCT_MASTER_COUNT_GET: str = "/Products/GetProductMastersCount"  # GET

# §8d Item Master (Push, §8.1.5). Create / Update / lifecycle.
# Create: returns {data: {product_id}} — we write the product_id back to
# the EasyEcom Item Map row. Update: keys on sku OR productId, supports
# partial updates. ActivateDeactivate: keys on product_id, status 1/0.
# All three are account-wide writes — foundational.
PRODUCT_MASTER_CREATE: str = "/Products/CreateMasterProduct"  # POST
PRODUCT_MASTER_UPDATE: str = "/Products/UpdateMasterProduct"  # POST
PRODUCT_MASTER_ACTIVATE_DEACTIVATE: str = "/Products/ActivateDeactivateProduct"  # POST


# ----- Buying flow endpoints (§31.3.3) -----

# §9 packet supersedes the older SPEC §31.3.3 paths (kept below as
# stale-but-retained constants — `PO_CREATE` / `PO_GET` etc. — for
# back-compat. New §9 code targets the live paths below.).
#
# §9 Stage 2 PO push uses TWO channels with TWO keys (§9 packet model):
#   - Content  → /WMS/Cart/CreatePurchaseOrder  keyed `referenceCode`
#   - Status   → /wms/updatePoStatus            keyed `po_id`
# Case difference is real: EE uses uppercase `WMS` on the content
# path and lowercase `wms` on the status path. Don't normalise either.
PURCHASE_ORDER_CREATE: str = "/WMS/Cart/CreatePurchaseOrder"  # POST (§9 Stage 2)
PURCHASE_ORDER_STATUS_UPDATE: str = "/wms/updatePoStatus"  # POST (§9 Stage 2)

# §9 Stage 3 GRN pull endpoint per packet (supersedes the stale
# `/wms/getGrnDetails` referenced in SPEC §9.5.1).
GRN_DETAILS_V2_GET: str = "/Grn/V2/getGrnDetails"  # GET (§9 Stage 3; nextUrl)


# ----- Order creation (§10 / §11 / §12) -----

# Unified order-creation endpoint per §10.G (grounded 2026-05-29 against
# live Harmony round-trip). `orderType` body field discriminates:
#   - "stocktransferorder"  → §10 STN (Stock Transfer Note)
#   - "B2C" / "B2B"         → §11/§12 sales orders (later)
#   - "ProductionOrder"     → out of scope
# Constant name is generic; the discriminator goes in the body, not the
# URL. Bearer JWT only (no x-api-key per the §10.G live test). Returns
# OrderID/SuborderID/InvoiceID as strings — capture all three on the
# §10 Transfer Map row.
CREATE_ORDER: str = "/webhook/v2/createOrder"  # POST (§10 Stage 2 STN)

# Pre-§9-packet constants — RETAINED to avoid breaking any references
# in older SPEC sections / fixtures. Not used by §9 code paths.
PO_CREATE: str = "/Wms/Purchase/createPO"  # POST (stale; replaced by PURCHASE_ORDER_CREATE)
PO_GET: str = "/Wms/Purchase/getPO"  # GET
PO_STATUS_GET: str = "/Wms/Purchase/getPOStatus"  # GET
GRN_GET: str = "/Wms/Inventory/getGRN"  # GET (bulk; Next-Page URL)
GRN_DETAILS_GET: str = "/Wms/Inventory/getGRNDetails"  # GET (stale; replaced by GRN_DETAILS_V2_GET)


# ----- Sales flow endpoints (§31.3.4) -----

ORDERS_GET_ALL: str = "/orders/V2/getAllOrders"  # GET (bulk; Next-Page URL)
ORDER_DETAILS_GET: str = "/orders/V2/getOrderDetails"  # GET

# §11 Phase 1 — ERPNext-initiated B2B cancellation.
# Grounded per design-lead's 2026-06-14 EE-doc reference:
#   POST /orders/cancelOrder
#   Headers: x-api-key (mandatory) + Authorization: Bearer <Jwt>
#   Payload: {"reference_code": "<SO name>"}
#   Response: {"code": 200, "message": "Successfully Cancelled the
#             Order with reference_code <SO name>", "data": []}
# Identifier choice: reference_code = SO.name = orderNumber sent at
# createOrder. Works uniformly for Old and New B2B.
CANCEL_ORDER: str = "/orders/cancelOrder"  # POST (§11 Phase 1)
B2B_SO_CREATE: str = "/b2b/createSalesOrder"  # POST
B2B_SO_GET: str = "/b2b/getSalesOrder"  # GET
B2B_INVOICE_UPLOAD: str = "/b2b/uploadInvoice"  # POST (multipart with PDF)
RESERVED_STOCK_GET: str = "/Wms/Inventory/getReservedStock"  # GET
MANIFEST_GET: str = "/Wms/Inventory/getManifest"  # GET
DISPATCH_GET: str = "/Wms/Inventory/getDispatch"  # GET


# ----- Returns and cancellations (§31.3.5) -----

RETURNS_GET_V3: str = "/returns/getReturnsV3"  # GET (bulk; Next-Page URL)
RETURN_DETAILS_GET: str = "/returns/getReturnDetails"  # GET
CANCELLED_ORDERS_GET: str = "/orders/V2/getCancelledOrders"  # GET (bulk; Next-Page URL)


# ----- Inventory (§31.3.6) -----

STOCK_SNAPSHOT_GET: str = "/Wms/Inventory/getStockSnapshot"  # GET
STOCK_MOVEMENTS_GET: str = "/Wms/Inventory/getStockMovements"  # GET
STOCK_ADJUSTMENT_UPLOAD: str = "/Wms/Inventory/uploadStockAdjustment"  # POST


# ----- Foundational endpoints (§7.7 — account-scoped, no Company) -----

# Marked here so the client + logging layer can tag API Call rows with
# is_foundational=1 and leave company blank.
FOUNDATIONAL_ENDPOINTS: frozenset[str] = frozenset(
    {
        TOKEN,
        LOCATIONS_GET,
        # §8d Item Pull is account-wide (includeLocations=1).
        PRODUCT_MASTER_GET,
        PRODUCT_MASTER_COUNT_GET,
        # §8d Item Push — account-wide catalogue writes (no per-location).
        PRODUCT_MASTER_CREATE,
        PRODUCT_MASTER_UPDATE,
        PRODUCT_MASTER_ACTIVATE_DEACTIVATE,
        # §8e Stage 2 — country / state reference data (pure foundational).
        COUNTRIES_GET,
        STATES_GET,
        # §8e Stage 3 — wholesale customer master (account-wide).
        WHOLESALE_USER_MANAGEMENT,
        # §8e Stage 4 — wholesale customer push (account-wide).
        WHOLESALE_CUSTOMER_CREATE,
        WHOLESALE_CUSTOMER_UPDATE,
        # §8f Stage 3+ — wholesale vendor master (account-wide).
        VENDORS_GET,
        WHOLESALE_VENDOR_CREATE,
        WHOLESALE_VENDOR_UPDATE,
    }
)


def is_foundational(endpoint: str) -> bool:
    """True for token acquisition and location-discovery calls (§7.7),
    and for account-wide §8d Item Pull/Push catalogue endpoints.

    Strip the query string before membership check. Cursor follow-up
    calls pass `endpoint="/Products/GetProductMaster?cursor=..."`; the
    exact-string match would miss those and split observability for
    the same logical endpoint across foundational and non-foundational
    buckets. (Also: a non-foundational classification for the cursor
    follow would mean no JWT is set — see client._request — and the
    call would 401. Strip keeps the same foundational policy across
    every page of a cursor walk.)"""
    return endpoint.split("?", 1)[0] in FOUNDATIONAL_ENDPOINTS
