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

# Item product types we know how to handle.
#
# EE classifies products as:
#   - normal_product   - standalone sellable item
#   - combo_product    - "virtual combo" - sold as one SKU but
#                        composed of multiple sub_products
#   - child_product    - standalone sellable item that ALSO
#                        appears as a sub_product inside one or
#                        more combos. The seller can move the
#                        SKU both ways: standalone AND as part
#                        of a combo. Confirmed with the user
#                        2026-05-26 against the Harmony sandbox.
#                        Therefore we treat child_product the
#                        same as normal_product on Stage 2 -
#                        create the standalone Item; the §8.1.6
#                        combo component resolver will then find
#                        the Item Map row when the parent combo
#                        is processed.
#   - variant_parent / kit_bom / unknown - not yet supported;
#     held FNC so the FDE sees a visible task.
#
# Stage 4 handles combo_product by building a Product Bundle
# whose components point at the sub_products' standalone Items.
CREATABLE_AS_ITEM: frozenset[str] = frozenset(
    {"normal_product", "child_product"}
)
COMBO_TYPE: str = "combo_product"
SUPPORTED_TYPES: frozenset[str] = frozenset(
    {"normal_product", "child_product", "combo_product"}
)

# Map row status values that the pull writes. Defined here so a typo
# in flow code becomes an obvious type error rather than a silent
# wrong-state Item Map row.
STATUS_MAPPED: str = "Mapped"
STATUS_CREATED_FLAGGED: str = "Created-Flagged"
STATUS_FLAGGED_NOT_CREATED: str = "Flagged-Not-Created"
# Stage 5: post-flip pull state — EE-origin change in steady-state
# (erpnext_mastered) mode. NOT accepted, NOT auto-overwritten — FDE
# resolves per row.
STATUS_DRIFT: str = "Drift"

# Stage 5: item_master_mode values (§8.1.1) — the per-Account flag
# that decides whether the pull is a normal accept-and-create flow
# (onboarding) or a drift detector (erpnext_mastered).
MODE_ONBOARDING: str = "onboarding"
MODE_ERPNEXT_MASTERED: str = "erpnext_mastered"

# Stage 5: fields the drift detector compares between the freshly-
# translated EE payload and the existing ERPNext Item. The list is
# deliberately the user-visible content fields (what the FDE would
# care about an EE edit to); internal IDs (ecs_ee_product_id /
# ecs_ee_cp_id) are NOT in here — they're identity-management state,
# not content. A change to those is a separate concern (remap), not
# drift in the §8.1.8 sense.
DRIFT_COMPARABLE_FIELDS: tuple[str, ...] = (
    "item_name",
    "description",
    "gst_hsn_code",
    "stock_uom",
    "weight_per_unit",
    "ecs_height_cm",
    "ecs_length_cm",
    "ecs_width_cm",
    "ecs_ee_cost",
    "ecs_ee_mrp",
    "standard_rate",
    "ecs_size",
    "ecs_colour",
)


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
        # Item Pull endpoints (GetProductMaster, GetProductMastersCount)
        # are NON-foundational — the EasyEcom API Call validate requires
        # either a Company or a Location Key on every persisted row.
        # §8d items are account-wide, so we pick a Company the same way
        # the Sync Record writes do (§audit #1): first enabled Company
        # Settings, then first Company on the site.
        from ecommerce_super.easyecom.flows._item_sync_records import (
            _company_for_item_sync,
        )

        client = EasyEcomClient(company=_company_for_item_sync())

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

    # `updated_after` is the delta-pull watermark - skip products
    # whose EE-side modified timestamp is <= this value. On start_fresh
    # we MUST NOT pass this; otherwise the previous run's bumped
    # watermark (item_pull_last_updated_at) tells EE "give me only
    # products updated AFTER the moment the prior pull finished",
    # i.e. nothing, and the listing endpoint returns
    # `{"data": "No Data Found"}` while the count endpoint still
    # reports the full catalogue - confusing and indistinguishable
    # from real empty pages. start_fresh by definition wants the
    # whole catalogue, watermark or no watermark.
    updated_after = (
        None if start_fresh else account.get("item_pull_last_updated_at")
    )
    for page in _iter_pages(
        client,
        starting_cursor=starting_cursor,
        updated_after=updated_after,
        max_pages=max_pages,
    ):
        products = _normalise_page_data(page.get("data"))
        # Defensive dedupe in case EE still returns multiple records
        # per SKU despite includeLocations=0 - keep the primary
        # location's record per SKU.
        products = _dedupe_to_primary_location(
            products, primary_location_name=_primary_location_name(),
        )
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


# ----- Drift resolution whitelists (audit fix #7) -----


@frappe.whitelist()
def dismiss_drift(item_map_name: str) -> dict[str, Any]:
    """Drift resolution action — FDE acknowledges the EE-side change
    is wrong or already-handled upstream. Returns the row to Mapped,
    clears the Drift Fields table, leaves the underlying ERPNext doc
    untouched.

    The next pull will re-detect if the divergence still exists; to
    silence persistent intentional divergence, the FDE adds the
    field to ecs_drift_exclude_fields (audit #10) instead.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Dismiss Drift requires EasyEcom FDE or System Manager."
            ),
            frappe.PermissionError,
        )
    if not item_map_name:
        return {"ok": False, "message": "item_map_name required"}
    if not frappe.db.exists("EasyEcom Item Map", item_map_name):
        return {
            "ok": False,
            "message": f"Item Map {item_map_name!r} not found.",
        }
    doc = frappe.get_doc("EasyEcom Item Map", item_map_name)
    if doc.status != STATUS_DRIFT:
        return {
            "ok": False,
            "message": (
                f"Item Map {item_map_name!r} is not in Drift status "
                f"(current: {doc.status}); nothing to dismiss."
            ),
        }
    doc.status = STATUS_MAPPED
    doc.flag_reason = None
    doc.drift_detected_at = None
    doc.set("drift_fields", [])
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "item_map_name": item_map_name, "status": STATUS_MAPPED}


# ----- Scheduler entry (audit fix #3) -----


def scheduled_discover_products() -> None:
    """Daily §8d delta-pull cron — wired in hooks.py scheduler_events
    at 05:00 IST (after 8a locations 03:30, 8b channels 04:00).

    Mirrors scheduled_discover_locations / scheduled_discover_channels:
    - Catches every exception so a transient EE outage doesn't fail
      the whole scheduler tick.
    - Logs to Error Log so the FDE sees it on the next desk visit.
    - Returns nothing — this is a scheduler hook, not a programmatic API.

    Mode-aware: process_one_product's phase gate decides whether the
    pulled product is accepted-and-created (onboarding) or runs
    through drift detection (erpnext_mastered). No mode-specific
    branching here; same pull_products call works for both phases.

    Delta semantics: pull_products reads
    Account.item_pull_last_updated_at as the updated_after parameter
    on the first GetProductMaster call, so each daily run only sees
    products changed since the previous successful run. A first run
    (no high-water set) does a full walk.

    Single-Account assumption (§8.1 / audit #11): finds the one
    enabled Account and runs against it. Multiple enabled accounts
    is now a DocType-level constraint violation; the scheduler just
    picks the first if a multi-account state somehow exists.
    """
    account_name = frappe.db.get_value(
        "EasyEcom Account", {"enabled": 1}, "name", order_by="name asc"
    )
    if not account_name:
        # No enabled Account — pre-onboarding state, nothing to pull.
        # Quiet log so a fresh deployment's daily cron doesn't spam.
        return
    try:
        pull_products(account_name=account_name)
    except Exception as exc:  # noqa: BLE001 — scheduler boundary
        frappe.log_error(
            title="EasyEcom scheduled product discovery failed",
            message=f"{type(exc).__name__}: {exc}",
        )


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


def _normalise_page_data(data: Any) -> list:
    """Coerce a GetProductMaster page's `data` field to a list.

    EE returns `{"data": [<products>]}` for non-empty pages but
    `{"data": "No Data Found"}` (a STRING) when a page is empty -
    observed live in the Harmony sandbox 2026-05-26 when the cursor
    walked past the last product. Without coercion the caller
    iterates the string character-by-character, each char becomes a
    "record", and downstream `.get(...)` calls on a str raise
    AttributeError - 13 chars of "No Data Found" -> 13 spurious
    failures per page. Treat any non-list shape as empty."""
    if isinstance(data, list):
        return data
    return []


def _dedupe_to_primary_location(
    records: list[dict], *, primary_location_name: str | None
) -> list[dict]:
    """Pick one record per SKU, preferring the primary location.

    Backstop for the case where EE returns multiple records per SKU -
    one per (SKU, location) - even though §8d wants a single master
    record per SKU. Observed live 2026-05-26 in the Harmony sandbox:
    HPC-APC-002 appeared 7 times in a single page (one per warehouse),
    each carrying its own per-channel `cp_id` (per-location partner
    ID). Without dedupe the per-product upsert ran 7 times for the
    same SKU and whichever record came last won non-deterministically.

    Strategy:
      1. If a record's `company_name` matches the EasyEcom Location
         marked `is_primary=1`, take it. That's the master/HQ record;
         its cp_id is the one EE expects on UpdateMasterProduct.
      2. Otherwise take the first occurrence (preserves source order).

    The first-occurrence fallback exists for sandboxes where the
    primary location's record isn't present in a page (mid-walk
    cursor positioning, etc.) - better to take SOMETHING than skip
    the SKU entirely.
    """
    if not primary_location_name:
        # No primary configured - dedupe by first occurrence only.
        seen: dict[str, dict] = {}
        for r in records:
            sku = (r or {}).get("sku")
            if sku and sku not in seen:
                seen[sku] = r
        return list(seen.values())

    by_sku: dict[str, dict] = {}
    primary_seen: set[str] = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        sku = r.get("sku")
        if not sku:
            continue
        if r.get("company_name") == primary_location_name:
            by_sku[sku] = r
            primary_seen.add(sku)
        elif sku not in primary_seen and sku not in by_sku:
            by_sku[sku] = r  # first non-primary fallback
    return list(by_sku.values())


def _primary_location_name() -> str | None:
    """The EasyEcom Location row marked is_primary=1, by its
    location_name (matches EE record's `company_name` field)."""
    return frappe.db.get_value(
        "EasyEcom Location", {"is_primary": 1}, "location_name"
    )


def _iter_pages(
    client: EasyEcomClient,
    *,
    starting_cursor: str | None,
    updated_after: Any,
    max_pages: int | None,
) -> Iterator[dict]:
    """Yield GetProductMaster page responses.

    First call either uses the resume cursor or starts with the
    includeLocations=1 query. Each subsequent call follows `nextUrl`.
    Stops when nextUrl is missing/null or max_pages reached.

    Cursor shape: EE Product Master returns `nextUrl` as a RELATIVE
    path ("/Products/GetProductMaster?cursor=..."). The 8a/8b bulk
    endpoints return absolute URLs in `next_page_url`. Detect by
    scheme — if the cursor starts with http(s), treat as absolute;
    otherwise prepend the Account's api_endpoint by passing through
    as a normal endpoint path.
    """
    pages_seen = 0
    current_cursor: str | None = starting_cursor
    while True:
        if current_cursor:
            is_absolute = current_cursor.startswith(("http://", "https://"))
            page = client._request(  # noqa: SLF001 — there's no public cursor-follow API
                "GET",
                endpoint=current_cursor,
                params=None,
                payload=None,
                timeout=60,
                _is_absolute_url=is_absolute,
            )
        else:
            # §8d is a MASTER product sync - we want one record per SKU,
            # not one per (SKU, location). Pass includeLocations=0 so EE
            # returns the master-only listing. Inventory (per-location
            # stock) is §8e/§8f territory. Observed live 2026-05-26: with
            # includeLocations=1 EE returned 7 records for HPC-APC-002
            # (one per warehouse), each with a different per-channel
            # cp_id, and the pull's repeated upserts left a
            # non-deterministic cp_id stored locally. The first push then
            # sent a stale per-channel cp_id, which EE rejected with the
            # business error "product/sku doest not exist".
            params: dict[str, Any] = {
                "includeLocations": 0,
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
        # Audit #1: write a Failed Sync Record OUTSIDE the rolled-back
        # savepoint so the failure is visible in §18 / §22 routing.
        try:
            from ecommerce_super.easyecom.flows._item_sync_records import (
                STATUS_FAILED,
                write_item_pull_sync_record,
            )

            write_item_pull_sync_record(
                entity_doctype="Item",
                entity_name=sku,
                sku=sku,
                status=STATUS_FAILED,
                last_error=f"{type(exc).__name__}: {exc}",
            )
        except Exception as inner_exc:  # noqa: BLE001
            frappe.log_error(
                title=f"EasyEcom: pull Failed Sync Record write failed for {sku}",
                message=f"{type(inner_exc).__name__}: {inner_exc}",
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

    Sets `frappe.flags.in_easyecom_pull = True` for the duration so
    that the auto-push hook (item_push.enqueue_on_item_change) skips
    any Items / Product Bundles this function saves — without the
    flag, a just-pulled item would immediately re-push back to EE
    (pull→push ping-pong; wasteful, idempotent, but pollutes the
    EE-side change log). The flag is restored in a finally block so
    a raised exception doesn't leak the flag to the next iteration.
    """
    sku = (product or {}).get("sku")
    if not sku:
        raise ValueError("product payload has no sku — cannot proceed")
    product_type = (product or {}).get("product_type") or "<missing>"
    prior_pull_flag = frappe.flags.get("in_easyecom_pull")
    frappe.flags.in_easyecom_pull = True
    try:
        outcome = _process_one_product_inner(
            product, product_type=product_type, sku=sku,
            account=account, executor=executor,
            enabled_companies=enabled_companies,
            is_stock_item=is_stock_item,
        )
        _write_pull_sync_record(outcome, sku=sku)
        return outcome
    finally:
        frappe.flags.in_easyecom_pull = prior_pull_flag


def _write_pull_sync_record(outcome: ProductOutcome, *, sku: str) -> None:
    """Audit #1: write a Sync Record for the pull operation.

    First entity-sync flow to do this; 8e/8f follow. 8a/8b/8c are
    foundational §7.7 and correctly don't.
    """
    try:
        from ecommerce_super.easyecom.flows._item_sync_records import (
            map_outcome_to_sync_status,
            write_item_pull_sync_record,
        )

        status, last_error = map_outcome_to_sync_status(outcome, "Pull")
        write_item_pull_sync_record(
            entity_doctype=outcome.erpnext_doctype or "Item",
            entity_name=outcome.erpnext_name or sku,
            sku=sku,
            status=status,
            last_error=last_error,
        )
    except Exception as exc:  # noqa: BLE001
        # A Sync Record write failure must NOT abort the per-product
        # work that already succeeded. Log and continue — the §10
        # three-log invariant is best-effort, not load-bearing for
        # the underlying ERPNext write.
        frappe.log_error(
            title=f"EasyEcom: pull Sync Record write failed for {sku}",
            message=f"{type(exc).__name__}: {exc}",
        )


def _process_one_product_inner(
    product: dict,
    *,
    product_type: str,
    sku: str,
    account: Any,
    executor: FieldMappingExecutor,
    enabled_companies: list[str],
    is_stock_item: int,
) -> ProductOutcome:
    """The actual per-product logic, called by process_one_product
    under the in_easyecom_pull flag (which gates the auto-push hook
    from re-pushing what we just pulled)."""

    # === Stage 5 gate: phase-governed routing (§8.1.1 / §8.1.8) ===
    # In erpnext_mastered mode, the pull is a DRIFT DETECTOR — it
    # never creates new ERPNext rows and never overwrites mapped
    # rows. Any EE-origin novelty or edit lands as a Drift map row
    # for the FDE to decide on. We branch BEFORE product_type
    # branching so unsupported types (variant/child/kit) ALSO go
    # through drift detection in steady state — an EE-origin
    # unsupported type is still a flag-worthy change ERPNext should
    # see, not silently FNC'd as if it were fresh.
    if account.get("item_master_mode") == MODE_ERPNEXT_MASTERED:
        return _detect_drift_one_product(
            product, account=account, executor=executor
        )

    # === Step 1: product_type branching (onboarding mode) ===
    if product_type not in SUPPORTED_TYPES:
        # variant_parent / child_product / kit_bom / unknown → FNC.
        return _flag_not_created(
            sku,
            reason=f"unsupported product_type: {product_type!r}",
            product=product,
        )
    if product_type == COMBO_TYPE:
        # Stage 4: build/refresh the Product Bundle.
        return _process_combo_product(
            product,
            account=account,
            executor=executor,
            enabled_companies=enabled_companies,
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
    map_doc = (
        frappe.get_doc("EasyEcom Item Map", map_name) if map_name else None
    )

    # An existing map row from a prior pull's FNC outcome has no
    # erpnext target (status='Flagged-Not-Created', erpnext_doctype/
    # erpnext_name both blank). If we're now reaching this step on a
    # re-pull, the gating checks above (product_type, HSN, etc.)
    # passed THIS time - the EE-side data has been fixed. Don't bail
    # via _load_or_refresh_item_from_map ("no erpnext target"); take
    # the create-or-auto-map path and reuse the existing map row in
    # place so the FNC -> CF transition lands without duplicating.
    map_is_unattached = map_doc and not map_doc.erpnext_name

    if map_doc and not map_is_unattached:
        item = _load_or_refresh_item_from_map(map_doc, erpnext_fields)
        created = False
    elif frappe.db.exists("Item", sku):
        # Auto-map: existing ERPNext item with byte-equal item_code.
        item = frappe.get_doc("Item", sku)
        _refresh_existing_item(item, erpnext_fields)
        if map_is_unattached:
            map_doc = _attach_map_row(
                map_doc, erpnext_doctype="Item", erpnext_name=item.name,
                erpnext_fields=erpnext_fields, status=STATUS_MAPPED,
            )
        else:
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
        if map_is_unattached:
            map_doc = _attach_map_row(
                map_doc, erpnext_doctype="Item", erpnext_name=item.name,
                erpnext_fields=erpnext_fields, status=STATUS_MAPPED,
            )
        else:
            map_doc = _create_map_row(
                sku=sku,
                erpnext_doctype="Item",
                erpnext_name=item.name,
                ee_product_id=erpnext_fields.get("ecs_ee_product_id"),
                ee_cp_id=erpnext_fields.get("ecs_ee_cp_id"),
                status=STATUS_MAPPED,
            )
        created = True

    # === Step 5b: EANUPC sync to Item.barcodes (typed EAN row) ===
    # EE's EANUPC is a scalar string; ERPNext stores barcodes as a
    # typed child table. Append the EE value as a barcode_type='EAN'
    # row only if it isn't already present. Mismatched-type rows for
    # the same value are left alone (FDE intent should win). Done
    # here in code because the field-mapping engine's sandbox doesn't
    # express child-row dedupe cleanly.
    _sync_ean_barcode(item, product.get("EANUPC"))

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


# ============================================================
# §8d Stage 5 — drift detection (post-flip pull behaviour, §8.1.8)
# ============================================================


def _detect_drift_one_product(
    product: dict, *, account: Any, executor: FieldMappingExecutor
) -> ProductOutcome:
    """Post-flip pull — DETECTS drift, never accepts or overwrites
    (§8.1.8).

    Three outcomes:
      1. NEW EE-origin product (no Item Map row for the sku): drift.
         Map row is created in Drift status so the SKU is FDE-visible;
         no Item / Product Bundle is created. (This is the case where
         EE has invented a new product post-flip; in steady state
         ERPNext is the source of truth, so new EE-side products are
         not auto-adopted.)
      2. EE-side EDIT to a mapped item (existing map → existing
         ERPNext doc; the translated EE payload differs from current
         ERPNext doc values on one or more comparable fields): drift.
         Map row's status is set to Drift; flag_reason lists each
         differing field as `field: ERPNext=<x> EE→<y>`. Nothing is
         overwritten on the ERPNext side.
      3. NO drift (EE payload matches ERPNext or differs only on
         identity-management fields): map row left as-is, returns
         the unchanged status. A noisy re-pull on a quiet day does
         not flap rows in and out of Drift.

    Bundles are handled with the same comparison applied to the
    wrapper Item (the bundle's content fields live on the wrapper);
    structural component drift (sub_products list changed) is also
    surfaced.

    Pull-side disable in onboarding (active:0 → ERPNext disabled) is
    NOT applied here — an EE-side deactivation in steady state is
    explicitly NOT accepted as truth per §8.1.7. It surfaces as a
    "disabled" drift entry for the FDE.
    """
    sku = product["sku"]
    map_row = frappe.db.get_value(
        "EasyEcom Item Map",
        {"ee_sku": sku},
        ["name", "erpnext_doctype", "erpnext_name", "status"],
        as_dict=True,
    )

    # === Case 1: EE-origin new product post-flip ===
    if not map_row:
        reason = (
            f"EE-origin new product {sku!r} appeared post-flip "
            f"(item_master_mode=erpnext_mastered); not created on the "
            "ERPNext side because ERPNext is the source of truth in "
            "steady state. FDE: either (a) create the item in ERPNext "
            "and push it (which will reconcile this map row via the "
            "Stage-2 auto-map sku==item_code path), or (b) ignore EE-"
            "side novelty by marking this row Disabled."
        )
        _upsert_drift_map_row(
            sku=sku,
            erpnext_doctype=None,
            erpnext_name=None,
            ee_product_id=str(product.get("product_id") or ""),
            ee_cp_id=str(product.get("cp_id") or ""),
            reason=reason,
        )
        return ProductOutcome(
            ee_sku=sku,
            status=STATUS_DRIFT,
            erpnext_doctype=None,
            erpnext_name=None,
            created=False,
            flag_reasons=[reason],
        )

    # === Case 2 / 3: existing mapping — diff the translated payload
    # against the current ERPNext doc. ===
    erpnext_fields = executor.pull(product)
    # Apply the same dirty-UOM substitution the normal pull would, so
    # we compare apples-to-apples (a re-pull with the same dirty
    # accounting_unit produces the same substituted stock_uom →
    # compared to the existing substituted stock_uom → no spurious
    # drift).
    raw_uom = erpnext_fields.get("stock_uom")
    if not raw_uom or not frappe.db.exists("UOM", raw_uom):
        erpnext_fields["stock_uom"] = account.get("default_uom") or "Nos"

    # Locate the wrapper Item (for bundles, the comparable content
    # lives on the wrapper, not the Product Bundle's own fields).
    if map_row.erpnext_doctype == "Item":
        existing_doc = frappe.get_doc("Item", map_row.erpnext_name)
        compare_target = "Item"
    elif map_row.erpnext_doctype == "Product Bundle":
        bundle = frappe.get_doc("Product Bundle", map_row.erpnext_name)
        existing_doc = frappe.get_doc("Item", bundle.new_item_code)
        compare_target = "Product Bundle wrapper Item"
    else:
        # Unknown target type — surface as drift rather than mutate.
        reason = (
            f"Map row {map_row.name} has unknown erpnext_doctype "
            f"{map_row.erpnext_doctype!r}; cannot compare for drift. "
            "FDE: investigate the map row's link target."
        )
        _mark_existing_map_drift(map_row.name, reasons=[reason])
        return ProductOutcome(
            ee_sku=sku, status=STATUS_DRIFT,
            erpnext_doctype=map_row.erpnext_doctype,
            erpnext_name=map_row.erpnext_name,
            created=False, flag_reasons=[reason],
        )

    # Audit #10: read FDE-marked exclude list so intentional divergence
    # doesn't re-flag on every nightly pull. List of field names to
    # skip in the drift comparison for THIS specific Item Map row.
    excluded_fields = _load_excluded_fields(map_row.name)

    # Audit #6: collect structured (field, erpnext_value, ee_value)
    # tuples instead of `||`-delimited strings.
    diffs = _diff_payload_vs_doc_structured(
        erpnext_fields, existing_doc, excluded_fields=excluded_fields
    )

    # Lifecycle drift — EE active:0 vs ERPNext disabled. In onboarding
    # the pull would have flipped Item.disabled; in steady state we
    # flag it (unless excluded).
    if "disabled" not in excluded_fields:
        ee_disabled = 1 if product.get("active") in (0, "0", False) else 0
        en_disabled = existing_doc.disabled or 0
        if ee_disabled != en_disabled:
            diffs.append(
                {"field": "disabled",
                 "erpnext_value": str(en_disabled),
                 "ee_value": str(ee_disabled)}
            )

    # Bundle-specific: component-set drift (unless excluded).
    if (
        map_row.erpnext_doctype == "Product Bundle"
        and "combo_sub_products" not in excluded_fields
    ):
        bundle_diff = _bundle_component_drift_structured(product, bundle)
        if bundle_diff is not None:
            diffs.append(bundle_diff)

    if not diffs:
        # Clean re-pull — map row left untouched, clear any prior
        # drift table rows (the divergence has been resolved upstream).
        _clear_drift_state(map_row.name)
        return ProductOutcome(
            ee_sku=sku,
            status=map_row.status or STATUS_MAPPED,
            erpnext_doctype=map_row.erpnext_doctype,
            erpnext_name=map_row.erpnext_name,
            created=False,
            flag_reasons=[],
        )

    _record_drift_with_table(map_row.name, diffs=diffs)
    # Backward-compat: provide flag_reasons as human-readable strings
    # too (ProductOutcome and tests still consume the list).
    reason_strings = [
        f"{d['field']}: ERPNext={d['erpnext_value']!r} EE→{d['ee_value']!r}"
        for d in diffs
    ]
    return ProductOutcome(
        ee_sku=sku,
        status=STATUS_DRIFT,
        erpnext_doctype=map_row.erpnext_doctype,
        erpnext_name=map_row.erpnext_name,
        created=False,
        flag_reasons=reason_strings,
    )


def _load_excluded_fields(map_name: str) -> set[str]:
    """Read the FDE-marked exclude list off the Item Map row. Empty
    set when the FDE hasn't excluded anything (default Stage-5
    behaviour preserved)."""
    rows = frappe.db.get_all(
        "EasyEcom Exclude Field",
        filters={"parent": map_name, "parenttype": "EasyEcom Item Map"},
        fields=["field"],
    )
    return {r.field for r in rows if r.field}


def _diff_payload_vs_doc_structured(
    erpnext_fields: dict, existing_doc: Any, *, excluded_fields: set[str]
) -> list[dict]:
    """Structured replacement for the Stage-5 _diff_payload_vs_doc.
    Returns a list of dicts shaped like the EasyEcom Item Map Drift
    Field child rows."""
    diffs: list[dict] = []
    for fld in DRIFT_COMPARABLE_FIELDS:
        if fld in excluded_fields:
            continue
        if fld not in erpnext_fields:
            continue
        ee_value = erpnext_fields.get(fld)
        en_value = existing_doc.get(fld)
        if _values_differ(ee_value, en_value):
            diffs.append(
                {
                    "field": fld,
                    "erpnext_value": _stringify(en_value),
                    "ee_value": _stringify(ee_value),
                }
            )
    return diffs


def _bundle_component_drift_structured(
    product: dict, bundle: Any
) -> dict | None:
    """Combo subProducts diff as one structured row (field='combo_sub_products')."""
    ee_components = sorted(
        ((sp.get("sku") or "", sp.get("quantity") or 0)
         for sp in (product.get("sub_products") or [])),
        key=lambda x: x[0],
    )
    en_components: list[tuple[str, Any]] = []
    for row in bundle.items or []:
        ee_sku = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": row.item_code},
            "ee_sku",
        )
        en_components.append((ee_sku or "", row.qty or 0))
    en_components.sort(key=lambda x: x[0])
    if ee_components == en_components:
        return None
    return {
        "field": "combo_sub_products",
        "erpnext_value": str(en_components),
        "ee_value": str(ee_components),
    }


def _record_drift_with_table(map_name: str, *, diffs: list[dict]) -> None:
    """Audit #6: write the structured diff to the EasyEcom Item Map
    Drift Field child table. Replaces _mark_existing_map_drift's
    `||`-delimited Data.

    Set parent map status to Drift, drift_detected_at to now, and
    flag_reason to a short summary (e.g. '3 fields drifted') —
    Drift Field table is the authoritative detail."""
    map_doc = frappe.get_doc("EasyEcom Item Map", map_name)
    map_doc.set("drift_fields", [])
    now = frappe.utils.now_datetime()
    for d in diffs:
        map_doc.append(
            "drift_fields",
            {
                "field": d["field"],
                "erpnext_value": d["erpnext_value"],
                "ee_value": d["ee_value"],
                "detected_at": now,
            },
        )
    map_doc.status = STATUS_DRIFT
    map_doc.drift_detected_at = now
    map_doc.flag_reason = (
        f"{len(diffs)} field(s) drifted — see Drift Fields table below"
    )
    map_doc.save(ignore_permissions=True)


def _clear_drift_state(map_name: str) -> None:
    """Clear the drift table + drift_detected_at on a clean re-pull
    (the divergence was resolved upstream — no need to keep the
    historical diff lingering)."""
    map_doc = frappe.get_doc("EasyEcom Item Map", map_name)
    if not map_doc.get("drift_fields"):
        return
    map_doc.set("drift_fields", [])
    map_doc.drift_detected_at = None
    map_doc.save(ignore_permissions=True)


def _stringify(v: Any) -> str:
    """Stringify a value for the drift table's Data fields. Long
    values truncate so the cell stays readable."""
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= 200 else (s[:197] + "...")


def _diff_payload_vs_doc(
    erpnext_fields: dict, existing_doc: Any, *, compare_target: str
) -> list[str]:
    """Compare the translated EE payload against the current ERPNext
    doc on DRIFT_COMPARABLE_FIELDS. Returns a list of human-readable
    diff strings."""
    diffs: list[str] = []
    for field in DRIFT_COMPARABLE_FIELDS:
        if field not in erpnext_fields:
            continue
        ee_value = erpnext_fields.get(field)
        en_value = existing_doc.get(field)
        if _values_differ(ee_value, en_value):
            diffs.append(
                f"{field} on {compare_target}: "
                f"ERPNext={en_value!r} EE→{ee_value!r}"
            )
    return diffs


def _values_differ(a: Any, b: Any) -> bool:
    """Drift comparison with None / "" / 0 leniency.

    - None / "" / missing all treat as 'absent' — absent vs absent is
      not drift. Absent vs a real value IS drift.
    - Numerics compared with a small float tolerance (dimensions and
      rates can wobble in the last decimal across float_to_str /
      str_to_float round-trips).
    - Strings compared byte-for-byte after stripping (an extra space
      from EE isn't drift).
    """
    def _norm(v: Any) -> Any:
        if v in (None, ""):
            return None
        if isinstance(v, str):
            return v.strip() or None
        return v

    na, nb = _norm(a), _norm(b)
    if na is None and nb is None:
        return False
    if na is None or nb is None:
        return True
    try:
        return abs(float(na) - float(nb)) > 0.0001
    except (TypeError, ValueError):
        return na != nb


def _bundle_component_drift(product: dict, bundle: Any) -> list[str]:
    """Surface drift on the combo's sub-products set vs the Bundle's
    items child table. Compares as sorted (ee_sku, qty) tuples."""
    ee_components = sorted(
        ((sp.get("sku") or "", sp.get("quantity") or 0)
         for sp in (product.get("sub_products") or [])),
        key=lambda x: x[0],
    )
    # Build the ERPNext side by resolving each component's ee_sku.
    en_components: list[tuple[str, Any]] = []
    for row in bundle.items or []:
        ee_sku = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": row.item_code},
            "ee_sku",
        )
        en_components.append((ee_sku or "", row.qty or 0))
    en_components.sort(key=lambda x: x[0])

    if ee_components == en_components:
        return []
    return [
        f"combo sub_products on Product Bundle: "
        f"ERPNext={en_components!r} EE→{ee_components!r}"
    ]


def _upsert_drift_map_row(
    *,
    sku: str,
    erpnext_doctype: str | None,
    erpnext_name: str | None,
    ee_product_id: str,
    ee_cp_id: str,
    reason: str,
) -> str:
    """Create or update a map row in Drift status (the §8.1.8 review
    state). Used for the EE-origin-new-product case where no map row
    existed before."""
    existing = frappe.db.get_value(
        "EasyEcom Item Map", {"ee_sku": sku}, "name"
    )
    if existing:
        frappe.db.set_value(
            "EasyEcom Item Map",
            existing,
            {
                "status": STATUS_DRIFT,
                "flag_reason": reason,
                "ee_product_id": ee_product_id or None,
                "ee_cp_id": ee_cp_id or None,
            },
            update_modified=True,
        )
        return existing
    doc = frappe.new_doc("EasyEcom Item Map")
    doc.update(
        {
            "ee_sku": sku,
            "erpnext_doctype": erpnext_doctype,
            "erpnext_name": erpnext_name,
            "ee_product_id": ee_product_id or None,
            "ee_cp_id": ee_cp_id or None,
            "status": STATUS_DRIFT,
            "flag_reason": reason,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _mark_existing_map_drift(map_name: str, *, reasons: list[str]) -> None:
    """Flip an existing map row to Drift status with the diff list as
    the reason. NEVER mutates the linked Item / Product Bundle —
    drift is read-only on the ERPNext side."""
    frappe.db.set_value(
        "EasyEcom Item Map",
        map_name,
        {"status": STATUS_DRIFT, "flag_reason": _join_reasons(reasons)},
        update_modified=True,
    )


# ============================================================
# §8d Stage 4 — combo_product pull (Product Bundle)
# ============================================================


# A combo must aggregate to at least 2 units total to be a real combo
# (otherwise it's the same as selling the standalone). EE allows
# either shape - confirmed in the Harmony sandbox 2026-05-26:
#
#   - "true combo": multiple distinct sub-products, e.g.
#     VC-KITCHEN-001 wraps {APC qty=1, DSH qty=1, FVW qty=1, KDG qty=1}
#     -> total qty 4
#   - "multi-pack":  one sub-product with qty>1, e.g. a 2-pack of X
#     sold as a different SKU than X -> 1 sub, qty=2
#
# The previous rule (>=2 DISTINCT sub-products) wrongly rejected
# multi-packs. The right invariant is "sum of qty >= 2".
MIN_COMBO_TOTAL_QTY: int = 2


def _process_combo_product(
    product: dict,
    *,
    account: Any,
    executor: FieldMappingExecutor,
    enabled_companies: list[str],
) -> ProductOutcome:
    """Build (or refresh) a Product Bundle from an EE combo_product
    payload. §8.1.6.

    Shape: an EE combo carries the same top-level fields as a normal
    product (sku, product_name, hsn_code, etc.) PLUS a `sub_products`
    list. The pull:
      1. Translates the wrapper-Item-shaped fields via the same
         EasyEcom-Item-Pull ruleset (no separate combo ruleset — the
         engine output is identical, only the flow does different
         things with it).
      2. Runs the same content gates as a normal product (HSN held;
         dirty UOM → Created-Flagged).
      3. Resolves each sub_product's ee_sku to an existing
         EasyEcom Item Map → ERPNext Item. UNRESOLVED component →
         FLAG the whole bundle (don't create a broken Bundle), per
         §8.1.6 component-identity-resolver model.
      4. Enforces total component qty >=2 (a 1x1 combo is the
         standalone; multi-pack 1xN and true N-distinct combos
         both pass).
      5. Creates/refreshes the wrapper Item (`is_stock_item=0` — a
         Product Bundle's new_item_code must point to a non-stock
         item; Stage 1 verified this constraint) and the
         Product Bundle doc.
      6. Stamps multi-Company tax on the WRAPPER Item (the same
         entity that ERPNext invoices for the bundle's sale).
      7. Writes the bundle's OWN map row keyed on the bundle SKU,
         pointing to "Product Bundle" (not the wrapper Item) per
         §8.1.2 dual-object-link contract.
    """
    sku = product["sku"]

    # === Step 1: translate via the ruleset (wrapper Item fields) ===
    erpnext_fields = executor.pull(product)

    # === Step 2: HSN gate (HOLD, do not create) ===
    hsn = erpnext_fields.get("gst_hsn_code")
    if not hsn or not frappe.db.exists("GST HSN Code", hsn):
        return _flag_not_created(
            sku,
            reason=(
                f"combo: missing or unknown HSN ({hsn!r}); India "
                "Compliance enforces gst_hsn_code as mandatory on Item "
                "(and the bundle's wrapper Item is still an Item)."
            ),
            product=product,
        )

    # === Step 3: UOM dirt (CREATE + FLAG) ===
    flag_reasons: list[str] = []
    raw_uom = erpnext_fields.get("stock_uom")
    if not raw_uom or not frappe.db.exists("UOM", raw_uom):
        substituted = account.get("default_uom") or "Nos"
        flag_reasons.append(
            f"dirty/unknown accounting_unit {raw_uom!r}; substituted "
            f"default UOM {substituted!r}."
        )
        erpnext_fields["stock_uom"] = substituted

    # === Step 4: resolve sub_products via component map rows ===
    sub_products = (product or {}).get("sub_products") or []
    components, resolution_errors = _resolve_pull_sub_products(sub_products)

    if resolution_errors:
        # Don't create a broken Bundle (§8.1.6 dependency contract).
        return _flag_not_created(
            sku,
            reason=" || ".join(resolution_errors),
            product=product,
        )

    total_qty = sum(float(c.get("qty") or 0) for c in components)
    if total_qty < MIN_COMBO_TOTAL_QTY:
        comp_summary = ", ".join(
            f"{c.get('item_code')}*{c.get('qty')}" for c in components
        ) or "none"
        return _flag_not_created(
            sku,
            reason=(
                f"combo's total component qty is {total_qty} "
                f"(components: {comp_summary}); needs total qty "
                f">={MIN_COMBO_TOTAL_QTY} to be a meaningful combo "
                "(a 1x1 combo is identical to selling the standalone). "
                "FDE: increase a sub-product qty or add components."
            ),
            product=product,
        )

    # === Step 5: upsert wrapper Item + Product Bundle ===
    # Map lookup is keyed on ee_sku (the natural key). If the existing
    # map points to a "Product Bundle", this is a re-pull — refresh
    # the wrapper Item + Bundle in place. If it points to "Item", it's
    # a type collision (EE renamed a normal product into a combo, or
    # the FDE manually re-mapped) — surface as an error rather than
    # silently mutate the link.
    map_name = frappe.db.get_value(
        "EasyEcom Item Map", {"ee_sku": sku}, "name"
    )
    bundle_existed = False
    map_doc = frappe.get_doc("EasyEcom Item Map", map_name) if map_name else None
    # An existing map row with erpnext_doctype/erpnext_name both blank
    # came from a prior pull's FNC outcome (sub_products not yet
    # mapped, etc.). On this re-pull the gating checks above passed,
    # so we now have everything needed to build the Bundle - route to
    # the create branch and ATTACH the existing row in place. Without
    # this we'd FNC again with the unhelpful "unknown erpnext_doctype"
    # reason.
    map_is_unattached = (
        map_doc is not None
        and not map_doc.erpnext_doctype
        and not map_doc.erpnext_name
    )

    if map_doc and not map_is_unattached:
        if map_doc.erpnext_doctype == "Product Bundle":
            wrapper_item, bundle = _load_or_refresh_bundle_from_map(
                map_doc, erpnext_fields, components
            )
            bundle_existed = True
        elif map_doc.erpnext_doctype == "Item":
            # EE-side type change (normal → combo). Don't silently
            # mutate; surface for the FDE.
            return _flag_not_created(
                sku,
                reason=(
                    f"combo {sku!r} maps to an existing ERPNext Item "
                    f"({map_doc.erpnext_name!r}), not a Product Bundle. "
                    "The EE product changed type from normal to combo. "
                    "FDE: decide whether to convert the ERPNext side "
                    "(delete the Item + this map row; the next pull "
                    "will create a Product Bundle)."
                ),
                product=product,
            )
        else:
            return _flag_not_created(
                sku,
                reason=(
                    f"Item Map for combo {sku!r} has unknown "
                    f"erpnext_doctype={map_doc.erpnext_doctype!r}"
                ),
                product=product,
            )
    else:
        # Fresh combo OR a re-pull of a previously-FNC combo whose
        # sub_products are now mapped. Create the wrapper Item + the
        # Product Bundle, then either insert a fresh map row or attach
        # the existing unattached one.
        wrapper_item = _create_item(erpnext_fields, is_stock_item=0)
        bundle = _create_product_bundle(
            wrapper_item.item_code, components=components
        )
        if map_is_unattached:
            map_doc = _attach_map_row(
                map_doc,
                erpnext_doctype="Product Bundle",
                erpnext_name=bundle.name,
                erpnext_fields=erpnext_fields,
                status=STATUS_MAPPED,
            )
        else:
            map_doc = _create_map_row(
                sku=sku,
                erpnext_doctype="Product Bundle",
                erpnext_name=bundle.name,
                ee_product_id=erpnext_fields.get("ecs_ee_product_id"),
                ee_cp_id=erpnext_fields.get("ecs_ee_cp_id"),
                status=STATUS_MAPPED,
            )

    # === Step 5b: EANUPC sync on the wrapper Item (typed EAN row) ===
    # Combos can also carry an EANUPC on EE side. Apply the same
    # typed-barcode sync as the normal-product path.
    _sync_ean_barcode(wrapper_item, product.get("EANUPC"))

    # === Step 6: lifecycle (active:0 → disable wrapper) ===
    if product.get("active") in (0, "0", False) and not wrapper_item.disabled:
        wrapper_item.disabled = 1
        wrapper_item.save(ignore_permissions=True)

    # === Step 7: multi-Company tax stamping on the wrapper Item ===
    # The bundle's sales transactions reference the wrapper Item, which
    # is what carries Item Tax rows. Components carry their own taxes
    # (set when they were pulled / pushed as normal items).
    tax_results: list[dict] = []
    if enabled_companies:
        wrapper_item = frappe.get_doc("Item", wrapper_item.name)
        for company in enabled_companies:
            tax_result = resolve_and_stamp_tax(wrapper_item, product, company)
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
                flag_reasons.append(
                    f"tax for company {company!r}: "
                    + ("; ".join(tax_result.discrepancies) or "unreconciled")
                )
        wrapper_item.save(ignore_permissions=True)

    # === Step 8: finalise the bundle's map row ===
    final_status = STATUS_CREATED_FLAGGED if flag_reasons else STATUS_MAPPED
    if map_doc.status != final_status or map_doc.flag_reason != _join_reasons(flag_reasons):
        map_doc.status = final_status
        map_doc.flag_reason = _join_reasons(flag_reasons)
        map_doc.save(ignore_permissions=True)

    return ProductOutcome(
        ee_sku=sku,
        status=final_status,
        erpnext_doctype="Product Bundle",
        erpnext_name=bundle.name,
        created=not bundle_existed,
        flag_reasons=flag_reasons,
        tax_results=tax_results,
    )


def _resolve_pull_sub_products(
    sub_products: list[dict],
) -> tuple[list[dict], list[str]]:
    """For each EE sub_product, find the existing Item Map row by
    sub_product['sku'] → return the resolved ERPNext Item.item_code
    + the qty.

    Returns (components, errors).
      components: list of {item_code (ERPNext), qty}
      errors: list of human-readable reasons for unresolved components.
    A component without a map row OR mapped to a non-Item target
    (e.g. mapped to a Product Bundle — nested combos aren't supported)
    counts as unresolved and contributes a reason."""
    components: list[dict] = []
    errors: list[str] = []
    for sp in sub_products:
        sp_sku = (sp or {}).get("sku")
        qty = (sp or {}).get("quantity") or 1
        if not sp_sku:
            errors.append("sub_product has no sku — EE payload malformed")
            continue
        map_row = frappe.db.get_value(
            "EasyEcom Item Map",
            {"ee_sku": sp_sku},
            ["erpnext_doctype", "erpnext_name"],
            as_dict=True,
        )
        if not map_row or not map_row.erpnext_name:
            errors.append(
                f"sub_product {sp_sku!r} not yet mapped — pull or push it "
                "as a normal item first, then re-pull this combo "
                "(component-identity resolver, §8.1.6)."
            )
            continue
        if map_row.erpnext_doctype != "Item":
            errors.append(
                f"sub_product {sp_sku!r} maps to "
                f"{map_row.erpnext_doctype}, not Item — nested combos "
                "(combo-of-combos) aren't supported. FDE: unmap or "
                "convert the sub-product."
            )
            continue
        components.append({"item_code": map_row.erpnext_name, "qty": qty})
    return components, errors


def _create_product_bundle(
    wrapper_item_code: str, *, components: list[dict]
) -> Any:
    """Insert a Product Bundle whose new_item_code is the wrapper.
    components is the list from _resolve_pull_sub_products."""
    bundle = frappe.new_doc("Product Bundle")
    bundle.update({"new_item_code": wrapper_item_code})
    for c in components:
        bundle.append("items", {"item_code": c["item_code"], "qty": c["qty"]})
    bundle.insert(ignore_permissions=True)
    return bundle


def _load_or_refresh_bundle_from_map(
    map_doc: Any, erpnext_fields: dict, components: list[dict]
) -> tuple[Any, Any]:
    """Re-pull path: refresh the wrapper Item + Product Bundle in
    place. The bundle's items list is REPLACED with the freshly-
    resolved component set (an EE-side combo edit — adding/removing
    sub-products — must propagate)."""
    bundle = frappe.get_doc("Product Bundle", map_doc.erpnext_name)
    wrapper_item = frappe.get_doc("Item", bundle.new_item_code)
    _refresh_existing_item(wrapper_item, erpnext_fields)
    bundle.set("items", [])
    for c in components:
        bundle.append("items", {"item_code": c["item_code"], "qty": c["qty"]})
    bundle.save(ignore_permissions=True)
    return wrapper_item, bundle


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


def _sync_ean_barcode(item: Any, ee_eanupc: Any) -> None:
    """Append the EE-provided EANUPC value to Item.barcodes as an
    EAN-typed row, with dedupe semantics.

    EE's EANUPC field is a scalar string carrying an EAN-format
    barcode. ERPNext's Item.barcodes is a typed child table
    (barcode + barcode_type). The pull mirrors the push: only the
    EAN type is touched.

    Dedupe rules:
      - If a row already exists with this barcode AND type='EAN',
        no-op (already in the desired state).
      - If a row exists with this barcode but a DIFFERENT type
        (UPC, ISBN), leave it alone - the FDE's typing wins; we
        do NOT silently re-classify a UPC as an EAN just because
        EE returned it under EANUPC.
      - Else, append a new row with type=EAN.

    No-op for None / empty incoming values.
    """
    if not ee_eanupc:
        return
    eanupc = str(ee_eanupc).strip()
    if not eanupc:
        return
    # Junk placeholder values EE sends when the seller hasn't entered
    # a real EAN. Treat as "no barcode" so ERPNext's barcode validator
    # doesn't reject the entire Item save. Observed live 2026-05-26
    # in the Harmony sandbox: 3 products carrying EANUPC='NA' produced
    # InvalidBarcode page_failures. The case-insensitive set covers
    # the common shapes; FDE can clean up EE-side data when convenient.
    if eanupc.upper() in {"NA", "N/A", "-", "0", "NIL", "NONE", "NULL"}:
        return

    existing = list(item.get("barcodes") or [])
    for row in existing:
        if (row.barcode or "") == eanupc:
            # Already present (any type) - respect FDE typing.
            return

    item.append("barcodes", {"barcode": eanupc, "barcode_type": "EAN"})
    item.save(ignore_permissions=True)


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
    incoming EE payload (additive). Also re-syncs the map row's own
    EE identity fields (ee_product_id, ee_cp_id) - those must track
    EE's primary-location values, not stay frozen at first-pull state.
    Without this re-sync the Item's ecs_ee_cp_id drifts away from
    Item Map's ee_cp_id and observability breaks."""
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

    # Re-sync the map row's EE identity to match what was just
    # written onto the Item. Identity fields drift if pull preserves
    # the original ones forever - confirmed live 2026-05-26 when
    # Item.ecs_ee_cp_id updated to the primary's 125293829 but
    # Item Map.ee_cp_id stayed at the stale 125293825 from earlier.
    new_pid = erpnext_fields.get("ecs_ee_product_id")
    new_cpid = erpnext_fields.get("ecs_ee_cp_id")
    if new_pid != map_doc.ee_product_id or new_cpid != map_doc.ee_cp_id:
        map_doc.ee_product_id = new_pid
        map_doc.ee_cp_id = new_cpid
        map_doc.save(ignore_permissions=True)
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


def _attach_map_row(
    map_doc: Any,
    *,
    erpnext_doctype: str,
    erpnext_name: str,
    erpnext_fields: dict,
    status: str,
) -> Any:
    """Attach a previously unattached (FNC) map row to a now-existing
    ERPNext target. Used when a re-pull of a SKU whose prior pull was
    Flagged-Not-Created (no Item / Bundle created) succeeds on this
    pass because the EE-side data was fixed (e.g. HSN updated).
    Reuses the existing row instead of creating a duplicate, which
    would violate the UNIQUE constraint on ee_sku."""
    map_doc.erpnext_doctype = erpnext_doctype
    map_doc.erpnext_name = erpnext_name
    map_doc.ee_product_id = (
        erpnext_fields.get("ecs_ee_product_id") or map_doc.ee_product_id
    )
    map_doc.ee_cp_id = (
        erpnext_fields.get("ecs_ee_cp_id") or map_doc.ee_cp_id
    )
    map_doc.status = status
    map_doc.flag_reason = None
    map_doc.save(ignore_permissions=True)
    return map_doc


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
