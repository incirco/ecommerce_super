"""§8d Stage 2 — EE → ERPNext Product Master pull.

The pull is the inbound onboarding flow. After the §8.1.1 flip
(`item_master_mode=erpnext_mastered`) it becomes drift-detection only
(§8.1.8 — that behaviour ships in Stage 5; this module's job ends at
the onboarding/bidirectional behaviour).

Job shape (§8.1.4):
  - Cursor-paginated walk of `/Products/GetProductMaster` (account-wide:
    includeLocations=1; cursor key `nextUrl`; ≤200/page).
  - Count-aware: opens with `/Products/GetProductMastersCount` so the
    FDE has a denominator to watch progress against.
  - Resumable: the next-page URL is persisted on the Account after
    EVERY successful page, so an interrupted walk re-enters at the
    last completed page rather than restarting (a re-call without an
    explicit `start_fresh=True` resumes from the persisted cursor).
  - Per-product savepoint-isolated (§7.1 via 8a `_isolation.py`): one
    bad product never aborts its page or the walk; failed products
    surface as `BatchOutcome.failed` and Flagged-Not-Created map rows
    where they got far enough to identify themselves.

Per-product orchestration (§8.1.3, §8.1.4):
  1. Field Mapping engine translates the payload via the
     EasyEcom-Item-Pull ruleset (reconciled against the captured
     fixtures, §8.0 engine policy).
  2. product_type branching on the response string:
       normal_product → Item path
       combo_product → flag/skip (Stage 4 builds Product Bundles)
       variant_parent / child_product / kit_bom / unknown
                       → Flagged-Not-Created, do NOT create
  3. Matching:
       map row exists → use the mapped Item.
       else sku byte-equals an Item.item_code → auto-map + create map row.
       else create new Item + map row.
     No fuzzy / EAN matching (§8.1.3).
  4. Content gating (corrected per §8.1.4):
       missing HSN → Flagged-Not-Created (held; India Compliance
         enforces gst_hsn_code as mandatory on Item).
       dirty/missing UOM → substitute Account.default_uom and flag
         Created-Flagged.
       unmapped tax rule for any Company → Created-Flagged for that
         Company; 8c resolver auto-creates a To-Configure map.
  5. Item upsert with `is_stock_item` as an input (default 1; Stage 4
     bundle wrappers and a future digital type pass 0).
  6. active:0 → item.disabled = 1 (no delete, ever — §8.1.7).
  7. Multi-Company tax stamping (§8d Stage 2 design, §8.1.4 sync):
     loop the Account's enabled `EasyEcom Company Settings` rows and
     call `resolve_and_stamp_tax(item, ee_product_dict, company)` once
     per Company. The 8c resolver is APPEND + IDEMPOTENT
     (Stage-2-patched, §8d), so each Company's Item Tax rows coexist
     on the one shared item and a re-pull never duplicates.

What this module does NOT do:
  - It does not push (Stage 3).
  - It does not build Product Bundles for combos (Stage 4); a combo
    is currently logged + skipped — the Item Map row exists in
    Flagged-Not-Created state with reason="combo_product (Stage 4)".
  - It does not implement Drift detection (Stage 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_COUNT_GET,
    PRODUCT_MASTER_GET,
)
from ecommerce_super.easyecom.doctype.easyecom_tax_rule_map.easyecom_tax_rule_map import (
    resolve_and_stamp_tax,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows._isolation import BatchOutcome, for_each_record

# Ruleset that translates a GetProductMaster product payload → ERPNext
# Item field dict. Reconciled against real captured payloads in
# tests/ee_mock/ during §8d Stage 2. §8.0 policy: edits to the
# ruleset (e.g. EE renames a field) are an FDE desk action, not a
# code deploy.
ITEM_PULL_RULESET: str = "EasyEcom-Item-Pull"

# Default page size (EE allows up to 200 per §8.1.4).
DEFAULT_PAGE_SIZE: int = 200

# Item product types we know how to handle. The §8.1.4 spec lists
# `normal_product` and `combo_product` as creatable, but Stage 2 only
# creates Items — combos become Product Bundles in Stage 4. Everything
# else (variant_parent / child_product / kit_bom / any future or
# unknown type) is Flagged-Not-Created so an FDE sees a visible task
# rather than silent skipping.
CREATABLE_AS_ITEM: frozenset[str] = frozenset({"normal_product"})
COMBO_TYPE: str = "combo_product"
SUPPORTED_TYPES: frozenset[str] = frozenset({"normal_product", "combo_product"})

# Map row status values that the pull writes. Defined here so a typo
# in flow code becomes an obvious type error rather than a silent
# wrong-state Item Map row.
STATUS_MAPPED: str = "Mapped"
STATUS_CREATED_FLAGGED: str = "Created-Flagged"
STATUS_FLAGGED_NOT_CREATED: str = "Flagged-Not-Created"


@dataclass
class ProductOutcome:
    """Per-product outcome — what the flow did and why."""

    ee_sku: str
    status: str  # one of STATUS_* constants
    erpnext_doctype: str | None = None  # "Item" / "Product Bundle" / None
    erpnext_name: str | None = None
    created: bool = False  # True iff a new Item was created (not auto-mapped)
    flag_reasons: list[str] = field(default_factory=list)
    tax_results: list[dict] = field(default_factory=list)  # one per Company


@dataclass
class PullOutcome:
    """Aggregate outcome for the whole walk."""

    total_count_reported: int | None = None  # from GetProductMastersCount
    pages_walked: int = 0
    products_processed: int = 0
    outcomes: list[ProductOutcome] = field(default_factory=list)
    page_failures: list[dict] = field(default_factory=list)  # cursor / network / payload
    last_cursor: str | None = None  # cursor where the walk stopped (None = exhausted)


# ----- Top-level orchestration -----


def pull_products(
    *,
    account_name: str,
    client: EasyEcomClient | None = None,
    start_fresh: bool = False,
    is_stock_item: int = 1,
    max_pages: int | None = None,
) -> PullOutcome:
    """Walk EE GetProductMaster, upsert Items, stamp taxes per-Company.

    Args:
        account_name: EasyEcom Account docname.
        client: pre-built EasyEcomClient (tests inject a mock; production
            callers leave this None and a default client is constructed).
        start_fresh: if True, ignore Account.item_pull_cursor and start
            from the first page; otherwise resume from the persisted
            cursor (or start fresh when none is set).
        is_stock_item: passed verbatim to new Items. Default 1 for normal
            products (the only path Stage 2 builds); Stage 4 bundle
            wrappers and a future digital type pass 0.
        max_pages: optional safety cap (mostly for tests; production
            leaves this None to walk to exhaustion).

    Returns:
        PullOutcome — see dataclass docstring.

    Idempotency: the per-product upsert is fully idempotent (same payload
    → same end state) and per-Company tax stamping is append+dedupe via
    the patched 8c resolver. Calling this twice in succession on the
    same EE catalogue produces the same DB state.
    """
    account = frappe.get_doc("EasyEcom Account", account_name)
    if client is None:
        client = EasyEcomClient(account=account)

    outcome = PullOutcome()
    outcome.total_count_reported = _read_total_count(client)
    if outcome.total_count_reported is not None:
        account.db_set(
            "item_pull_total_seen",
            outcome.total_count_reported,
            update_modified=False,
            commit=False,
        )

    # Cursor selection — see _resolve_starting_cursor for the resume
    # vs. fresh decision tree.
    starting_cursor: str | None = _resolve_starting_cursor(account, start_fresh=start_fresh)
    if start_fresh:
        # Wipe any stale cursor so a mid-walk crash on this run resumes
        # from this run's progress, not last run's.
        account.db_set("item_pull_cursor", None, update_modified=False, commit=False)
        frappe.db.commit()

    executor = FieldMappingExecutor(ITEM_PULL_RULESET)
    enabled_companies = _enabled_companies(account_name)
    if not enabled_companies:
        # Surface ONCE per pull rather than per-Item — see
        # process_one_product step 7 for the rationale.
        frappe.log_error(
            title="EasyEcom item pull: no enabled Company Settings",
            message=(
                f"Account {account_name!r} has no enabled EasyEcom Company "
                "Settings rows. Items will be created but no Item Tax rows "
                "will be stamped (tax loop is skipped). Enable at least one "
                "Company before relying on these items in tax-bearing "
                "transactions."
            ),
        )

    for page in _iter_pages(
        client,
        starting_cursor=starting_cursor,
        updated_after=account.get("item_pull_last_updated_at"),
        max_pages=max_pages,
    ):
        products = page.get("data") or []
        page_outcome = _process_page(
            products,
            account=account,
            executor=executor,
            enabled_companies=enabled_companies,
            is_stock_item=is_stock_item,
        )
        outcome.outcomes.extend(page_outcome.outcomes)
        outcome.page_failures.extend(page_outcome.page_failures)
        outcome.pages_walked += 1
        outcome.products_processed += len(products)

        # Advance the cursor AFTER the page commits so a crash before
        # next-page fetch leaves the cursor at this page's nextUrl.
        next_cursor = page.get("nextUrl")
        outcome.last_cursor = next_cursor
        account.db_set(
            {
                "item_pull_cursor": next_cursor,
                "item_pull_cursor_at": frappe.utils.now_datetime(),
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()

    # Clean walk — clear the cursor + set high-water for next delta pull.
    if outcome.last_cursor is None and not outcome.page_failures:
        account.db_set(
            {
                "item_pull_cursor": None,
                "item_pull_last_updated_at": frappe.utils.now_datetime(),
            },
            update_modified=False,
            commit=False,
        )
        frappe.db.commit()

    return outcome


# ----- Whitelist surface -----


@frappe.whitelist()
def discover_products(
    account: str, start_fresh: int | bool = False
) -> dict[str, Any]:
    """FDE-facing wrapper, mirrors the §8a discover_locations pattern.

    Permission: EasyEcom FDE / System Manager / EasyEcom System Manager.
    Operator is read-only — pulling is allowed (no EE mutation) but the
    button is FDE-tier because creating Items is a write.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._("Discover Products requires EasyEcom FDE or System Manager."),
            frappe.PermissionError,
        )
    try:
        result = pull_products(
            account_name=account, start_fresh=bool(int(start_fresh or 0))
        )
    except Exception as exc:  # noqa: BLE001 — whitelist boundary
        frappe.log_error(
            title="EasyEcom Discover Products failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "message": (
                f"Discovery pull failed: {type(exc).__name__}: {exc}. "
                "See Error Log for the full trace."
            ),
        }

    by_status: dict[str, int] = {}
    for o in result.outcomes:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    return {
        "ok": True,
        "total_reported": result.total_count_reported,
        "pages_walked": result.pages_walked,
        "products_processed": result.products_processed,
        "by_status": by_status,
        "page_failures": result.page_failures[:10],
        "more_to_walk": bool(result.last_cursor),
    }


# ----- Helpers (kept module-private; the per-product function is the only test seam) -----


def _read_total_count(client: EasyEcomClient) -> int | None:
    """Best-effort read of GetProductMastersCount — the FDE-facing
    denominator. A failure here doesn't abort the pull; the count is
    a UX-grade signal, not load-bearing for correctness."""
    try:
        resp = client.get(PRODUCT_MASTER_COUNT_GET)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title="EasyEcom GetProductMastersCount failed (non-fatal)",
            message=f"{type(exc).__name__}: {exc}",
        )
        return None
    count = (resp or {}).get("count")
    if isinstance(count, int):
        return count
    try:
        return int(count) if count is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_starting_cursor(
    account: Any, *, start_fresh: bool
) -> str | None:
    """Resume-vs-fresh decision.

    Returns None when starting fresh (the iterator does an unparameterised
    first call to PRODUCT_MASTER_GET with includeLocations=1). Returns
    the persisted cursor when resuming.
    """
    if start_fresh:
        return None
    return account.get("item_pull_cursor") or None


def _iter_pages(
    client: EasyEcomClient,
    *,
    starting_cursor: str | None,
    updated_after: Any,
    max_pages: int | None,
) -> Iterator[dict]:
    """Yield GetProductMaster page responses.

    First call either uses the resume cursor (absolute URL — EE returns
    full path in nextUrl, the client treats it as absolute) or starts
    with the includeLocations=1 query. Each subsequent call follows
    `nextUrl`. Stops when nextUrl is missing/null or max_pages reached.
    """
    pages_seen = 0
    current_cursor: str | None = starting_cursor
    while True:
        if current_cursor:
            # Resume / continuation: follow nextUrl as an absolute path.
            page = client._request(  # noqa: SLF001 — there's no public absolute-url GET
                "GET",
                endpoint=current_cursor,
                params=None,
                payload=None,
                timeout=60,
                _is_absolute_url=True,
            )
        else:
            params: dict[str, Any] = {
                "includeLocations": 1,
                "limit": DEFAULT_PAGE_SIZE,
            }
            if updated_after:
                # EE expects 'YYYY-MM-DD HH:MM:SS'.
                params["updated_after"] = str(updated_after)
            page = client.get(PRODUCT_MASTER_GET, params=params)

        yield page
        pages_seen += 1
        current_cursor = page.get("nextUrl")
        if not current_cursor or (max_pages is not None and pages_seen >= max_pages):
            return


def _process_page(
    products: list[dict],
    *,
    account: Any,
    executor: FieldMappingExecutor,
    enabled_companies: list[str],
    is_stock_item: int,
) -> PullOutcome:
    """Drive the per-product loop with savepoint isolation."""
    page_outcomes: list[ProductOutcome] = []
    page_failures: list[dict] = []

    def _handle(product: dict) -> None:
        po = process_one_product(
            product,
            account=account,
            executor=executor,
            enabled_companies=enabled_companies,
            is_stock_item=is_stock_item,
        )
        page_outcomes.append(po)

    def _on_failure(product: dict, exc: BaseException) -> None:
        sku = (product or {}).get("sku") or "<unknown>"
        page_failures.append(
            {"ee_sku": sku, "error": f"{type(exc).__name__}: {exc}"}
        )
        frappe.log_error(
            title=f"EasyEcom item pull failed: {sku}",
            message=(
                f"{type(exc).__name__}: {exc}\n"
                f"Product payload: {frappe.as_json(product)}"
            ),
        )

    for_each_record(
        products,
        handler=_handle,
        on_failure=_on_failure,
        flow_name="item_pull",
    )
    out = PullOutcome()
    out.outcomes = page_outcomes
    out.page_failures = page_failures
    return out


def process_one_product(
    product: dict,
    *,
    account: Any,
    executor: FieldMappingExecutor,
    enabled_companies: list[str],
    is_stock_item: int = 1,
) -> ProductOutcome:
    """Pull one EE product → upsert ERPNext Item + map row.

    The full per-product orchestration: branch → match → gate →
    upsert → multi-Co tax. Exposed at module scope (not nested) so
    tests can drive single-product cases without standing up a whole
    cursor walk.
    """
    sku = (product or {}).get("sku")
    if not sku:
        raise ValueError("product payload has no sku — cannot proceed")
    product_type = (product or {}).get("product_type") or "<missing>"

    # === Step 1: product_type branching ===
    if product_type not in SUPPORTED_TYPES:
        # variant_parent / child_product / kit_bom / unknown → FNC.
        return _flag_not_created(
            sku,
            reason=f"unsupported product_type: {product_type!r}",
            product=product,
        )
    if product_type == COMBO_TYPE:
        # Stage 4 builds bundles; for now flag the SKU as held with a
        # reason that points an FDE at Stage 4. The map row gets the
        # cp_id / product_id so a Stage 4 backfill can find it later.
        return _flag_not_created(
            sku,
            reason="combo_product (Stage 4 will build the Product Bundle)",
            product=product,
        )

    # === Step 2: translate via the engine ===
    erpnext_fields = executor.pull(product)

    # === Step 3: HSN gate (HOLD, do not create) ===
    hsn = erpnext_fields.get("gst_hsn_code")
    if not hsn or not frappe.db.exists("GST HSN Code", hsn):
        return _flag_not_created(
            sku,
            reason=(
                f"missing or unknown HSN ({hsn!r}); India Compliance "
                "enforces gst_hsn_code as mandatory on Item, so the item "
                "cannot be created. FDE: assign a valid HSN in EE or set "
                "the HSN library mapping, then re-pull."
            ),
            product=product,
        )

    # === Step 4: UOM dirt — substitute default + collect flag reason ===
    flag_reasons: list[str] = []
    raw_uom = erpnext_fields.get("stock_uom")
    if not raw_uom or not frappe.db.exists("UOM", raw_uom):
        substituted = account.get("default_uom") or "Nos"
        flag_reasons.append(
            f"dirty/unknown accounting_unit {raw_uom!r}; substituted "
            f"default UOM {substituted!r} — FDE: verify or correct."
        )
        erpnext_fields["stock_uom"] = substituted

    # === Step 5: matching (§8.1.3) ===
    map_name = frappe.db.get_value(
        "EasyEcom Item Map", {"ee_sku": sku}, "name"
    )
    if map_name:
        map_doc = frappe.get_doc("EasyEcom Item Map", map_name)
        item = _load_or_refresh_item_from_map(map_doc, erpnext_fields)
        created = False
    elif frappe.db.exists("Item", sku):
        # Auto-map: existing ERPNext item with byte-equal item_code.
        item = frappe.get_doc("Item", sku)
        _refresh_existing_item(item, erpnext_fields)
        map_doc = _create_map_row(
            sku=sku,
            erpnext_doctype="Item",
            erpnext_name=item.name,
            ee_product_id=erpnext_fields.get("ecs_ee_product_id"),
            ee_cp_id=erpnext_fields.get("ecs_ee_cp_id"),
            status=STATUS_MAPPED,
        )
        created = False
    else:
        item = _create_item(erpnext_fields, is_stock_item=is_stock_item)
        map_doc = _create_map_row(
            sku=sku,
            erpnext_doctype="Item",
            erpnext_name=item.name,
            ee_product_id=erpnext_fields.get("ecs_ee_product_id"),
            ee_cp_id=erpnext_fields.get("ecs_ee_cp_id"),
            status=STATUS_MAPPED,
        )
        created = True

    # === Step 6: lifecycle (active:0 → disabled) ===
    if product.get("active") in (0, "0", False) and not item.disabled:
        item.disabled = 1
        item.save(ignore_permissions=True)

    # === Step 7: multi-Company tax stamping ===
    # If there are no enabled Company Settings, skip the tax loop
    # silently — flagging EVERY item with the same "no Company
    # configured" message would drown the per-product flag stream in
    # noise. The condition is a pull-level config issue, not a per-
    # product content problem; pull_products logs it once at the start
    # of the walk.
    tax_results: list[dict] = []
    if not enabled_companies:
        pass
    else:
        # Reload to attach a fresh document instance (the in-memory
        # `item` after _create_item / _refresh_existing_item is
        # already persisted; we need a single doc to thread the
        # multi-Co stamps onto and save once at the end).
        item = frappe.get_doc("Item", item.name)
        for company in enabled_companies:
            tax_result = resolve_and_stamp_tax(item, product, company)
            tax_results.append(
                {
                    "company": company,
                    "mapped": tax_result.mapped,
                    "auto_created": tax_result.auto_created,
                    "stamped_count": tax_result.stamped_count,
                    "reconciled": tax_result.reconciled,
                    "discrepancies": tax_result.discrepancies,
                }
            )
            if not tax_result.mapped or not tax_result.reconciled:
                # Either no Tax Rule Map existed (8c just auto-created
                # one and flagged FDE) OR the map's rates didn't match
                # the product's resolved rate. Both are FDE-visible
                # tasks that don't block item creation.
                flag_reasons.append(
                    f"tax for company {company!r}: "
                    + ("; ".join(tax_result.discrepancies) or "unreconciled")
                )
        item.save(ignore_permissions=True)

    # === Step 8: finalise the map row status ===
    final_status = STATUS_CREATED_FLAGGED if flag_reasons else STATUS_MAPPED
    if map_doc.status != final_status or map_doc.flag_reason != _join_reasons(flag_reasons):
        map_doc.status = final_status
        map_doc.flag_reason = _join_reasons(flag_reasons)
        map_doc.save(ignore_permissions=True)

    return ProductOutcome(
        ee_sku=sku,
        status=final_status,
        erpnext_doctype="Item",
        erpnext_name=item.name,
        created=created,
        flag_reasons=flag_reasons,
        tax_results=tax_results,
    )


# ----- Internal: Item/Map upsert helpers -----


def _create_item(erpnext_fields: dict, *, is_stock_item: int) -> Any:
    """Insert a new ERPNext Item from the translated fields.

    `is_stock_item` is an INPUT (default 1 from caller) — Stage 4 bundle
    wrappers pass 0 since a Product Bundle's `new_item_code` must point
    to a non-stock wrapper. A future digital-product type will also
    pass 0. Hardcoding to 1 here would force Stage 4 to subclass or
    monkeypatch.
    """
    # item_group / stock_uom are required by ERPNext on insert; both
    # are sourced from the translated dict + a sensible default. We
    # don't ship per-Company Item Defaults on pull — those are an FDE
    # post-processing decision.
    doc = frappe.new_doc("Item")
    fields = {k: v for k, v in erpnext_fields.items() if v is not None}
    doc.update(fields)
    doc.is_stock_item = is_stock_item
    if not doc.item_group:
        doc.item_group = _default_item_group()
    doc.insert(ignore_permissions=True)
    return doc


def _refresh_existing_item(item: Any, erpnext_fields: dict) -> None:
    """Refresh EE-supplied fields on an existing Item (auto-map path
    or persisted-map path). Skip None values so an EE payload that
    momentarily drops a field doesn't NULL out an existing value
    (same additive-refresh semantics as §8a Location)."""
    updates = {k: v for k, v in erpnext_fields.items() if v is not None}
    # Don't overwrite item_code — that's the join key and changing it
    # would break the mapping.
    updates.pop("item_code", None)
    item.update(updates)
    item.save(ignore_permissions=True)


def _load_or_refresh_item_from_map(map_doc: Any, erpnext_fields: dict) -> Any:
    """Resolve the Item the map row points to and refresh it from the
    incoming EE payload (additive)."""
    if not map_doc.erpnext_doctype or not map_doc.erpnext_name:
        raise ValueError(
            f"Item Map {map_doc.name} has no erpnext target — "
            "Stage 2 pull cannot upsert without a known target"
        )
    if map_doc.erpnext_doctype != "Item":
        raise ValueError(
            f"Item Map {map_doc.name} points to {map_doc.erpnext_doctype}, "
            "not Item — Stage 4 will handle Product Bundles."
        )
    item = frappe.get_doc("Item", map_doc.erpnext_name)
    _refresh_existing_item(item, erpnext_fields)
    return item


def _create_map_row(
    *,
    sku: str,
    erpnext_doctype: str | None,
    erpnext_name: str | None,
    ee_product_id: str | None,
    ee_cp_id: str | None,
    status: str,
    flag_reason: str | None = None,
) -> Any:
    """Create an EasyEcom Item Map row. Caller picks the status."""
    doc = frappe.new_doc("EasyEcom Item Map")
    doc.update(
        {
            "ee_sku": sku,
            "erpnext_doctype": erpnext_doctype,
            "erpnext_name": erpnext_name,
            "ee_product_id": ee_product_id,
            "ee_cp_id": ee_cp_id,
            "status": status,
            "flag_reason": flag_reason,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _flag_not_created(
    sku: str, *, reason: str, product: dict
) -> ProductOutcome:
    """Create or update a Flagged-Not-Created map row for a SKU we
    can't create on this pull (unsupported type or missing HSN).

    Idempotent: a re-pull of the same un-creatable SKU updates the
    existing row's flag_reason rather than raising on the UNIQUE
    constraint. The ee_product_id / ee_cp_id are captured so a later
    fix (assign HSN in EE; re-pull) can reconcile the row to Mapped
    by following sku → existing FNC row → upgrade.
    """
    existing = frappe.db.get_value(
        "EasyEcom Item Map", {"ee_sku": sku}, "name"
    )
    if existing:
        doc = frappe.get_doc("EasyEcom Item Map", existing)
        doc.status = STATUS_FLAGGED_NOT_CREATED
        doc.flag_reason = reason
        doc.ee_product_id = str(product.get("product_id") or doc.ee_product_id or "")
        doc.ee_cp_id = str(product.get("cp_id") or doc.ee_cp_id or "")
        doc.save(ignore_permissions=True)
    else:
        doc = _create_map_row(
            sku=sku,
            erpnext_doctype=None,
            erpnext_name=None,
            ee_product_id=str(product.get("product_id") or ""),
            ee_cp_id=str(product.get("cp_id") or ""),
            status=STATUS_FLAGGED_NOT_CREATED,
            flag_reason=reason,
        )
    return ProductOutcome(
        ee_sku=sku,
        status=STATUS_FLAGGED_NOT_CREATED,
        erpnext_doctype=None,
        erpnext_name=None,
        created=False,
        flag_reasons=[reason],
    )


# ----- Internal: Company / UOM helpers -----


def _enabled_companies(account_name: str) -> list[str]:
    """Return ERPNext Company names whose EasyEcom Company Settings row
    is enabled. Multi-Company sites: this list drives the per-Company
    tax-stamp loop. Order is stable (Company name asc) for test
    determinism."""
    rows = frappe.db.get_all(
        "EasyEcom Company Settings",
        filters={"enabled": 1},
        fields=["company"],
        order_by="company asc",
    )
    return [r.company for r in rows if r.company]


def _default_item_group() -> str:
    """Fall back to ERPNext's stock 'All Item Groups' root if no other
    default is set. The FDE can change item_group on the form."""
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    # Defensive — a fresh ERPNext bench should always have this root,
    # but new test sites sometimes don't. Pick the first available.
    first = frappe.db.get_value("Item Group", filters={}, fieldname="name")
    if not first:
        raise frappe.ValidationError(
            "No Item Group exists on this site — the §8d pull needs at "
            "least one (typically 'All Item Groups') to create Items."
        )
    return first


def _join_reasons(reasons: list[str]) -> str | None:
    """Join multiple flag reasons into one Data field (||-delimited).

    The map row's flag_reason is a single Data — when a product has
    multiple problems (dirty UOM AND unmapped tax for two Companies),
    they all need to land on the row. Easier to grep / less ambiguous
    than newlines in a Data field."""
    if not reasons:
        return None
    return " || ".join(reasons)
