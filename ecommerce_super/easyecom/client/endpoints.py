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
VENDOR_CREATE: str = "/Wms/Vendor/createVendor"  # POST
VENDOR_GET: str = "/Wms/Vendor/getVendor"  # GET
LOCATIONS_GET: str = "/getAllLocation"  # GET — foundational (§7.7, §8.4.1)
CHANNELS_GET: str = "/current-channel-status"  # GET — per-location (§8.6.3, §8b)

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

PO_CREATE: str = "/Wms/Purchase/createPO"  # POST
PO_GET: str = "/Wms/Purchase/getPO"  # GET
PO_STATUS_GET: str = "/Wms/Purchase/getPOStatus"  # GET
GRN_GET: str = "/Wms/Inventory/getGRN"  # GET (bulk; Next-Page URL)
GRN_DETAILS_GET: str = "/Wms/Inventory/getGRNDetails"  # GET


# ----- Sales flow endpoints (§31.3.4) -----

ORDERS_GET_ALL: str = "/orders/V2/getAllOrders"  # GET (bulk; Next-Page URL)
ORDER_DETAILS_GET: str = "/orders/V2/getOrderDetails"  # GET
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
    }
)


def is_foundational(endpoint: str) -> bool:
    """True for token acquisition and location-discovery calls (§7.7)."""
    return endpoint in FOUNDATIONAL_ENDPOINTS
