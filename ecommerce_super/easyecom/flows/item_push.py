"""§8d Stage 3 — ERPNext → EE Product Master push.

The push is the **outbound onboarding** flow AND the **steady-state
mechanism** (§8.1.1). Once the FDE flips `item_master_mode` to
`erpnext_mastered`, ALL item changes flow ERPNext → EE.

Stage 3 covers NORMAL items only. Combos (Product Bundles) are Stage 4
— the sweep + per-item push detect a bundle wrapper and skip+flag
rather than build a malformed combo payload.

⚠️ HARD CONSTRAINT (build-time): this module is verified ENTIRELY
against HTTP mocks. The push mechanism is real, but during development
and CI no real EE write ever happens — the real call is operated by
the user post-build with appropriate auth. If you ever feel a real
EE write is needed to validate something, STOP and report; do not
make the call.

The three endpoints (§8.1.5):
  - POST /Products/CreateMasterProduct — new item; returns
    {data: {product_id}} which we write back to the map row + Item.
  - POST /Products/UpdateMasterProduct — changed item; keys on sku
    OR productId; partial updates OK.
  - POST /Products/ActivateDeactivateProduct — lifecycle; keys on
    product_id; status 1 (alive) or 0 (dead).

Field translation: the EasyEcom-Item-Push ruleset (separate from
EasyEcom-Item-Pull, different direction) handles real-data-from-item
mappings (Sku, ItemName, ProductTaxCode, dimensions, Cost,
productId). EE-mandatory fields that ERPNext doesn't natively carry
(materialType, itemType, Brand/Category/ModelNumber fallbacks,
TaxRate snap to EE's allowed set) are MANUFACTURED by this flow per
§8.1.5 — they're flow decisions, not translations, and don't belong
in the ruleset per the §8.0 engine-as-translator policy.

Two triggers (§8.1.5 / packet item 4):
  - Individual push — Item.created/edited → push that item.
    Wiring is deliberately NOT in hooks.py for Stage 3: every Item
    save in this codebase (including the entire test suite's many
    create-Item calls) would trigger an enqueue that, in turn,
    would try to call EE. Per the build-time HARD CONSTRAINT, that
    risk is unacceptable. `enqueue_item_push()` is provided as the
    queue-able worker; the FDE or the user wires the doc_event
    when they're ready to flip on real outbound traffic.
  - Batch onboarding sweep — `push_all_pending()` walks
    `_candidate_items_for_sweep()` with savepoint isolation,
    pushes each, returns a SweepOutcome.

Map writeback: a successful Create writes `ee_product_id` and
`ee_sku` to the EasyEcom Item Map row AND to the Item's
`ecs_ee_product_id` custom field — same direction-agnostic map the
pull writes to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import frappe

from ecommerce_super.easyecom.client.client import EasyEcomClient
from ecommerce_super.easyecom.client.endpoints import (
    PRODUCT_MASTER_ACTIVATE_DEACTIVATE,
    PRODUCT_MASTER_CREATE,
    PRODUCT_MASTER_UPDATE,
)
from ecommerce_super.easyecom.doctype.easyecom_tax_rule_map.easyecom_tax_rule_map import (
    _effective_rate_for_template,
)
from ecommerce_super.easyecom.field_mapping.executor import FieldMappingExecutor
from ecommerce_super.easyecom.flows._isolation import for_each_record
from ecommerce_super.easyecom.queue import enqueue_easyecom_job
from ecommerce_super.easyecom.utils.idempotency import item_push_key

ITEM_PUSH_RULESET: str = "EasyEcom-Item-Push"

# EE accepts only this set for TaxRate on Create/Update (§8.1.5).
# Float-equality with tolerance — India Compliance shipped templates
# produce 5.0, 12.0, 18.0, 28.0 cleanly, but legacy bands can be
# 5.000001 etc. TaxRate stays in flow code (not the ruleset) because
# resolving it requires a DB lookup of Item Tax Template + GST rate
# arithmetic — not a constant or simple per-field translation. Every
# other "manufactured" constant (materialType, itemType, Brand
# fallback, Cost fallback chain, ModelNumber default) lives in the
# EasyEcom-Item-Push ruleset as a `conditional_constant` /
# `custom_python` transform so the FDE can edit it in the desk
# without a code deploy.
EE_ALLOWED_TAX_RATES: tuple[float, ...] = (0.0, 3.0, 5.0, 12.0, 18.0, 28.0)
TAX_RATE_TOLERANCE: float = 0.01

# Map status used by Stage 3 (re-uses the §8d Stage-1 enum).
STATUS_MAPPED: str = "Mapped"
STATUS_FLAGGED_NOT_PUSHED: str = "Flagged-Not-Created"


# ----- Outcomes -----


@dataclass
class PushOutcome:
    """Per-item push outcome. The `operation` enum lets a sweep
    summarise in one pass."""

    item_code: str
    pushed: bool  # True iff EE was actually called (mocked or real)
    operation: str  # "create" | "update" | "skipped" | "flagged" | "error"
    ee_product_id: str | None = None
    flag_reasons: list[str] = field(default_factory=list)
    ee_payload: dict | None = None  # for tests / FDE diagnostics


@dataclass
class SweepOutcome:
    total_considered: int = 0
    create_count: int = 0
    update_count: int = 0
    skipped_count: int = 0
    flagged_count: int = 0
    outcomes: list[PushOutcome] = field(default_factory=list)


# ============================================================
# Top-level public API
# ============================================================


def _with_push_sync_record(fn):
    """Decorator that writes a Sync Record after the push function
    returns, mapping the PushOutcome to (status, last_error). Audit
    fix #1 — every push op gets a Sync Record so §22 alert routing
    can subscribe to push failures."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        outcome = fn(*args, **kwargs)
        try:
            from ecommerce_super.easyecom.flows._item_sync_records import (
                map_outcome_to_sync_status,
                write_item_push_sync_record,
            )

            status, last_error = map_outcome_to_sync_status(outcome, "Push")
            # entity_doctype: Item for normal items / lifecycle calls;
            # bundles' push_one_bundle returns ee.item_code = the
            # wrapper Item's code, but the entity is conceptually
            # the Product Bundle. Detect via Product Bundle existence.
            entity_doctype = "Item"
            entity_name = outcome.item_code
            if frappe.db.exists(
                "Product Bundle", {"new_item_code": outcome.item_code}
            ):
                entity_doctype = "Product Bundle"
                # Product Bundle's docname == wrapper item_code in this codebase.
            write_item_push_sync_record(
                entity_doctype=entity_doctype,
                entity_name=entity_name,
                sku=outcome.item_code,
                status=status,
                last_error=last_error,
            )
        except Exception as sr_exc:  # noqa: BLE001
            frappe.log_error(
                title=(
                    f"EasyEcom: push Sync Record write failed for "
                    f"{getattr(outcome, 'item_code', '?')}"
                ),
                message=f"{type(sr_exc).__name__}: {sr_exc}",
            )
        return outcome

    return wrapper


@_with_push_sync_record
def push_one_item(
    item_code: str,
    *,
    client: EasyEcomClient,
    account: Any,
    executor: FieldMappingExecutor | None = None,
    enabled_companies: list[str] | None = None,
) -> PushOutcome:
    """Push (or update) one ERPNext Item to EE.

    Decides Create vs Update by checking for an existing
    EasyEcom Item Map row with `ee_product_id` set.

    Args:
        item_code: ERPNext Item.item_code.
        client: EasyEcomClient — tests pass a MockClient.
        account: EasyEcom Account doc (already loaded).
        executor: optional pre-built FieldMappingExecutor for
            EasyEcom-Item-Push (caller reuse for batch).
        enabled_companies: list of enabled Company names for tax-rate
            resolution. If None, derived from EasyEcom Company Settings.

    Returns:
        PushOutcome — see dataclass.
    """
    if executor is None:
        executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
    if enabled_companies is None:
        enabled_companies = _enabled_companies()

    item = frappe.get_doc("Item", item_code)

    # === Stage 4: Product Bundle wrapper → dispatch to combo push ===
    bundle_name = frappe.db.get_value(
        "Product Bundle", {"new_item_code": item_code}, "name"
    )
    if bundle_name:
        return push_one_bundle(
            bundle_name,
            client=client,
            account=account,
            executor=executor,
            enabled_companies=enabled_companies,
        )

    # === Disabled items don't get a Create — they may get a
    # lifecycle deactivate if previously pushed (handled separately). ===
    if item.disabled:
        return PushOutcome(
            item_code=item_code,
            pushed=False,
            operation="skipped",
            flag_reasons=[
                "Item is disabled — use push_lifecycle() to send "
                "ActivateDeactivateProduct, don't Create a dead item."
            ],
        )

    # === Build the payload (engine + manufactured constants + checks) ===
    payload, flag_reasons = build_push_payload(
        item, executor=executor, enabled_companies=enabled_companies
    )
    if flag_reasons:
        _upsert_map_row_flagged(item_code, reasons=flag_reasons)
        return PushOutcome(
            item_code=item_code,
            pushed=False,
            operation="flagged",
            flag_reasons=flag_reasons,
            ee_payload=payload,
        )

    # === Route: Create vs Update ===
    existing_map = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Item", "erpnext_name": item_code},
        ["name", "ee_product_id"],
        as_dict=True,
    )

    if existing_map and existing_map.ee_product_id:
        return _do_update(
            item, payload, client=client, account=account,
            ee_product_id=existing_map.ee_product_id,
            enabled_companies=enabled_companies,
        )
    return _do_create(
        item, payload, client=client, account=account,
        existing_map=existing_map, enabled_companies=enabled_companies,
    )


def push_all_pending(
    *,
    account_name: str,
    client: EasyEcomClient | None = None,
    limit: int | None = None,
) -> SweepOutcome:
    """Batch onboarding sweep — push every qualifying ERPNext item.

    **Which-items policy (the default for the onboarding sweep):**

    An Item is a sweep candidate iff:
      - is_stock_item=1 (Stage 3: normal stock items)
      - disabled=0 (don't push dead items)
      - gst_hsn_code is set (mandatory for any tax-bearing transaction
        and required to build a valid EE payload's ProductTaxCode)
      - NOT a Product Bundle wrapper (Stage 4 builds those)
      - NO EasyEcom Item Map row OR the map row has no
        `ee_product_id` (i.e. never successfully pushed)

    Items that already have an `ee_product_id` are NOT swept. They
    were either:
      - Pulled in Stage 2 (mapped to an existing EE product), or
      - Pushed once already and got an ee_product_id back.
    Either way, they're not "pending" — they're known to EE. Changes
    to those items flow via the individual-push trigger (`enqueue_
    item_push`) once it's wired, not via the sweep.

    Why exclude bundles at the SQL level (not just at per-item):
    avoiding work that we'd only skip anyway, AND keeping bundle
    semantics fully Stage-4-owned. The wrapper Item exists in tabItem
    so a naive query would pick it up.

    Savepoint isolation via 8a `for_each_record` — one bad item never
    aborts siblings; failures land in `SweepOutcome.outcomes` with
    operation="error".

    Resumable: the sweep is naturally resumable because the
    which-items query EXCLUDES items already pushed. A second run
    after a partial failure will re-query and pick up only the
    still-pending items — no cursor needed.
    """
    account = frappe.get_doc("EasyEcom Account", account_name)
    if client is None:
        client = EasyEcomClient(account=account)
    executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
    enabled_companies = _enabled_companies()

    candidate_codes = _candidate_items_for_sweep(limit=limit)
    outcome = SweepOutcome(total_considered=len(candidate_codes))

    def _handle(item_code: str) -> None:
        po = push_one_item(
            item_code,
            client=client,
            account=account,
            executor=executor,
            enabled_companies=enabled_companies,
        )
        outcome.outcomes.append(po)

    def _on_failure(item_code: str, exc: BaseException) -> None:
        outcome.outcomes.append(
            PushOutcome(
                item_code=item_code,
                pushed=False,
                operation="error",
                flag_reasons=[f"{type(exc).__name__}: {exc}"],
            )
        )
        frappe.log_error(
            title=f"EasyEcom item push failed: {item_code}",
            message=f"{type(exc).__name__}: {exc}",
        )

    for_each_record(
        candidate_codes,
        handler=_handle,
        on_failure=_on_failure,
        flow_name="item_push_sweep",
    )

    for po in outcome.outcomes:
        if po.operation == "create":
            outcome.create_count += 1
        elif po.operation == "update":
            outcome.update_count += 1
        elif po.operation == "skipped":
            outcome.skipped_count += 1
        else:  # flagged | error
            outcome.flagged_count += 1
    return outcome


def enqueue_push_all_pending(
    *, account_name: str, limit: int | None = None
) -> dict[str, Any]:
    """Enqueue one Item Push job per sweep candidate (audit #8).

    Replaces push_all_pending's inline-loop usage for the FDE-facing
    button. Returns IMMEDIATELY with a list of enqueued Queue Job
    docnames + counts; the FDE's browser doesn't hang through N
    sequential EE calls.

    Same which-items policy as push_all_pending (which is still used
    for tests + tooling that wants synchronous results with a mock
    client injected).

    The enqueued handler (item_push_queue_handler) calls
    push_one_item per item; failures land on each job's Queue Job
    row and on the Item Map's Flagged-Not-Created status; the FDE
    sees both via the workspace number cards and the Queue Job
    list. No single-point-of-failure batch state to wrangle.
    """
    candidate_codes = _candidate_items_for_sweep(limit=limit)
    enqueued: list[str] = []
    for item_code in candidate_codes:
        try:
            qj_name = enqueue_item_push(item_code, account_name=account_name)
            enqueued.append(qj_name)
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(
                title=f"EasyEcom sweep enqueue failed: {item_code}",
                message=f"{type(exc).__name__}: {exc}",
            )
    return {
        "total_considered": len(candidate_codes),
        "enqueued_count": len(enqueued),
        "queue_job_names_sample": enqueued[:10],
    }


@_with_push_sync_record
def push_lifecycle(
    item_code: str, *, client: EasyEcomClient, account: Any
) -> PushOutcome:
    """ERPNext disable/enable → EE ActivateDeactivateProduct (§8.1.7).

    No-op when the item has never been pushed (no ee_product_id) —
    nothing on EE's side to toggle.
    """
    item = frappe.get_doc("Item", item_code)
    map_row = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Item", "erpnext_name": item_code},
        ["name", "ee_product_id"],
        as_dict=True,
    )
    if not map_row or not map_row.ee_product_id:
        return PushOutcome(
            item_code=item_code,
            pushed=False,
            operation="skipped",
            flag_reasons=["item has no ee_product_id — never pushed"],
        )

    payload = {
        "product_id": map_row.ee_product_id,
        "status": 0 if item.disabled else 1,
    }
    client.post(PRODUCT_MASTER_ACTIVATE_DEACTIVATE, payload=payload)
    return PushOutcome(
        item_code=item_code,
        pushed=True,
        operation="update",  # lifecycle is an update in EE's vocabulary
        ee_product_id=map_row.ee_product_id,
        ee_payload=payload,
    )


# ============================================================
# §8d Stage 4 — Bundle (combo) push
# ============================================================


# EE rejects a combo with fewer than 2 sub-products (per §8.1.6 spec
# echoing EE FAQ). The constraint is symmetric — pull also flags a
# combo that arrives with <2 sub_products.
MIN_COMBO_SUB_PRODUCTS: int = 2


@_with_push_sync_record
def push_one_bundle(
    bundle_name: str,
    *,
    client: EasyEcomClient,
    account: Any,
    executor: FieldMappingExecutor | None = None,
    enabled_companies: list[str] | None = None,
) -> PushOutcome:
    """Push (or update) an ERPNext Product Bundle to EE as a combo
    product (itemType=1 with subProducts).

    Stage 4 contract (§8.1.6):
    - Components must exist EE-side BEFORE the combo references them
      (dependency-ordering). A component without an ee_product_id
      → FLAG the bundle, don't push a broken combo.
    - EE requires ≥2 sub-products → FLAG if fewer.
    - The bundle gets its OWN map row (linked to "Product Bundle",
      not the wrapper Item). The wrapper Item itself never gets a
      map row from the push path — it exists to anchor the bundle
      but isn't an EE product on its own. (The pull's combo
      creation also uses this same pattern.)
    - itemType=1 comes from the ruleset's `conditional_constant`
      reading `source_doc.flags.is_bundle_wrapper`, which this
      function sets to True on the wrapper Item before calling
      executor.push().
    """
    if executor is None:
        executor = FieldMappingExecutor(ITEM_PUSH_RULESET)
    if enabled_companies is None:
        enabled_companies = _enabled_companies()

    bundle = frappe.get_doc("Product Bundle", bundle_name)
    wrapper_item = frappe.get_doc("Item", bundle.new_item_code)

    # === Resolve components via their own map rows ===
    components, resolution_errors = _resolve_bundle_components(bundle)

    if resolution_errors:
        # Components not yet mapped/pushed → flag the bundle, don't
        # build a broken combo payload.
        _upsert_bundle_map_row_flagged(bundle, reasons=resolution_errors)
        return PushOutcome(
            item_code=bundle.new_item_code,
            pushed=False,
            operation="flagged",
            flag_reasons=resolution_errors,
        )

    if len(components) < MIN_COMBO_SUB_PRODUCTS:
        reason = (
            f"EE requires a combo to have at least {MIN_COMBO_SUB_PRODUCTS} "
            f"sub-products; this bundle has {len(components)}. "
            "Add more components in the Product Bundle (or push the "
            "wrapper Item as a normal product instead)."
        )
        _upsert_bundle_map_row_flagged(bundle, reasons=[reason])
        return PushOutcome(
            item_code=bundle.new_item_code,
            pushed=False,
            operation="flagged",
            flag_reasons=[reason],
        )

    # === Set the per-record flag the ruleset's itemType conditional
    # reads (in-memory, not persisted; lives on the doc's flags dict). ===
    wrapper_item.flags.is_bundle_wrapper = True

    # === Build the wrapper's payload via the ruleset; inject subProducts. ===
    payload, flag_reasons = build_push_payload(
        wrapper_item,
        executor=executor,
        enabled_companies=enabled_companies,
    )
    if flag_reasons:
        # Wrapper-Item content problem (missing dims, no tax) — same
        # FNC path as normal items, but the flag lands on the bundle's
        # map row (not the wrapper's, which has no map row of its own).
        _upsert_bundle_map_row_flagged(bundle, reasons=flag_reasons)
        return PushOutcome(
            item_code=bundle.new_item_code,
            pushed=False,
            operation="flagged",
            flag_reasons=flag_reasons,
            ee_payload=payload,
        )

    # Defensive: confirm the ruleset emitted itemType=1.
    if payload.get("itemType") != 1:
        # Indicates the ruleset's conditional didn't pick up our flag
        # — would mean the conditional was edited to something the FDE
        # didn't intend. Fail loud rather than silently push a normal
        # itemType=0 for a combo (which would be wrong on EE's side).
        raise RuntimeError(
            f"Bundle push expected itemType=1 (combo); ruleset emitted "
            f"itemType={payload.get('itemType')!r}. The "
            "EasyEcom-Item-Push ruleset's itemType rule must read "
            "source_doc.flags.is_bundle_wrapper — the FDE may have "
            "edited the conditional. Fix the ruleset and re-push."
        )

    payload["subProducts"] = [
        {"sku": c["ee_sku"], "quantity": c["qty"]} for c in components
    ]

    # === Route Create vs Update — keyed on bundle's OWN map row ===
    existing_map = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Product Bundle", "erpnext_name": bundle_name},
        ["name", "ee_product_id"],
        as_dict=True,
    )
    if existing_map and existing_map.ee_product_id:
        return _do_update_bundle(
            bundle=bundle,
            wrapper_item=wrapper_item,
            payload=payload,
            client=client,
            account=account,
            ee_product_id=existing_map.ee_product_id,
        )
    return _do_create_bundle(
        bundle=bundle,
        wrapper_item=wrapper_item,
        payload=payload,
        client=client,
        account=account,
        existing_map=existing_map,
    )


def _resolve_bundle_components(
    bundle: Any,
) -> tuple[list[dict], list[str]]:
    """Walk bundle.items[] and look up each component's EE identity
    via its EasyEcom Item Map row.

    Returns:
        (components, errors). components is a list of
        {item_code, ee_sku, ee_product_id, qty}. errors is a list of
        human-readable reasons covering missing-map and missing-product_id
        — both of which mean "component is not on EE yet" and must
        FLAG the bundle (dependency-ordering, §8.1.6).
    """
    components: list[dict] = []
    errors: list[str] = []
    for row in bundle.items or []:
        component_code = row.item_code
        qty = row.qty
        if not component_code:
            errors.append("bundle has a blank component row — fix in ERPNext")
            continue
        map_row = frappe.db.get_value(
            "EasyEcom Item Map",
            {"erpnext_doctype": "Item", "erpnext_name": component_code},
            ["ee_sku", "ee_product_id"],
            as_dict=True,
        )
        if not map_row:
            errors.append(
                f"component {component_code!r} has no EasyEcom Item Map row "
                "— push or pull it as a normal item first, then re-push "
                "this bundle (dependency-ordering, §8.1.6)."
            )
            continue
        if not map_row.ee_product_id:
            errors.append(
                f"component {component_code!r} is mapped but has no "
                "ee_product_id (never successfully pushed) — push it as a "
                "normal item first, then re-push this bundle "
                "(dependency-ordering, §8.1.6)."
            )
            continue
        if not map_row.ee_sku:
            errors.append(
                f"component {component_code!r}'s map row has no ee_sku "
                "(unexpected — map rows are keyed on ee_sku); investigate "
                "the map row before re-pushing this bundle."
            )
            continue
        components.append(
            {
                "item_code": component_code,
                "ee_sku": map_row.ee_sku,
                "ee_product_id": map_row.ee_product_id,
                "qty": qty,
            }
        )
    return components, errors


def _do_create_bundle(
    *,
    bundle: Any,
    wrapper_item: Any,
    payload: dict,
    client: EasyEcomClient,
    account: Any,
    existing_map: dict | None,
) -> PushOutcome:
    """CreateMasterProduct for a bundle (itemType=1, with subProducts).
    Returned product_id writes back to the BUNDLE's map row (not the
    wrapper Item's — bundles have their own map row per §8.1.2)."""
    idem_key = _idempotency_key(wrapper_item, payload, account)
    response = client.post(
        PRODUCT_MASTER_CREATE, payload=payload, idempotency_key=idem_key
    )
    returned_product_id = ((response or {}).get("data") or {}).get("product_id")
    if not returned_product_id:
        reasons = [
            f"CreateMasterProduct returned no product_id for combo "
            f"(response: {response!r}); FDE: investigate before re-pushing — "
            "EE may have created the combo without us learning its id."
        ]
        _upsert_bundle_map_row_flagged(bundle, reasons=reasons)
        return PushOutcome(
            item_code=wrapper_item.item_code,
            pushed=False,
            operation="flagged",
            flag_reasons=reasons,
            ee_payload=payload,
        )

    product_id_str = str(returned_product_id)
    _upsert_bundle_map_row_after_create(
        bundle, ee_product_id=product_id_str, existing_map=existing_map
    )
    return PushOutcome(
        item_code=wrapper_item.item_code,
        pushed=True,
        operation="create",
        ee_product_id=product_id_str,
        ee_payload=payload,
    )


def _do_update_bundle(
    *,
    bundle: Any,
    wrapper_item: Any,
    payload: dict,
    client: EasyEcomClient,
    account: Any,
    ee_product_id: str,
) -> PushOutcome:
    """UpdateMasterProduct for a bundle. Sends the full subProducts
    array — EE replaces the combo's component set on update."""
    payload = dict(payload)
    payload["productId"] = ee_product_id
    idem_key = _idempotency_key(wrapper_item, payload, account)
    client.post(PRODUCT_MASTER_UPDATE, payload=payload, idempotency_key=idem_key)
    return PushOutcome(
        item_code=wrapper_item.item_code,
        pushed=True,
        operation="update",
        ee_product_id=ee_product_id,
        ee_payload=payload,
    )


def _upsert_bundle_map_row_after_create(
    bundle: Any, *, ee_product_id: str, existing_map: dict | None
) -> str:
    """Write back the bundle's EE product_id to its OWN map row
    (erpnext_doctype='Product Bundle'). Mirrors
    _upsert_map_row_after_create but for the bundle dual-object link."""
    if existing_map and existing_map.get("name"):
        frappe.db.set_value(
            "EasyEcom Item Map",
            existing_map["name"],
            {
                "ee_product_id": ee_product_id,
                "status": STATUS_MAPPED,
                "flag_reason": None,
            },
            update_modified=True,
        )
        return existing_map["name"]

    doc = frappe.new_doc("EasyEcom Item Map")
    doc.update(
        {
            "ee_sku": bundle.name,  # bundle name == wrapper's item_code
            "erpnext_doctype": "Product Bundle",
            "erpnext_name": bundle.name,
            "ee_product_id": ee_product_id,
            "status": STATUS_MAPPED,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_bundle_map_row_flagged(
    bundle: Any, *, reasons: list[str]
) -> str:
    """Bundle-flavoured FNC map-row upsert. Always points to the
    Product Bundle (not the wrapper Item) per §8.1.2."""
    existing = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Product Bundle", "erpnext_name": bundle.name},
        "name",
    )
    reason_str = " || ".join(reasons)
    if existing:
        frappe.db.set_value(
            "EasyEcom Item Map",
            existing,
            {"status": STATUS_FLAGGED_NOT_PUSHED, "flag_reason": reason_str},
            update_modified=True,
        )
        return existing
    doc = frappe.new_doc("EasyEcom Item Map")
    doc.update(
        {
            "ee_sku": bundle.name,
            "erpnext_doctype": "Product Bundle",
            "erpnext_name": bundle.name,
            "status": STATUS_FLAGGED_NOT_PUSHED,
            "flag_reason": reason_str,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


# ============================================================
# Queue trigger (Stage 3 — kept here for proximity to bundle work)
# ============================================================


# ============================================================
# Auto-push hook (Stage 6 — doc_event wired in hooks.py)
# ============================================================


# Frappe doc-event handlers receive (doc, method) — `method` is the
# event name ("after_insert" / "on_update"). We accept any kwargs to
# stay forward-compatible if Frappe extends the signature.
def enqueue_on_item_change(doc: Any, method: str | None = None, **_kwargs) -> None:
    """doc_event handler for Item.after_insert / on_update.

    Fires the §8d push for an ERPNext Item save IFF all of these hold:
      1. An enabled EasyEcom Account exists with auto_push_on_save=1.
         Default is 0 — accidental enable on a fresh deployment would
         push every existing Item. FDE toggles ON when ready.
      2. The current operation is NOT inside an EasyEcom pull
         (frappe.flags.in_easyecom_pull). The Stage-2 pull saves Items
         after translating EE payloads; without this gate the hook
         would immediately re-push the just-pulled item back to EE,
         causing a ping-pong (wasteful even though idempotent).
      3. The Item is not a variant-template (`has_variants=1`) — those
         aren't real products we push to EE.

    The handler enqueues via frappe.enqueue (non-blocking on save).
    Push failures land in Error Log + the Item's map row as FNC; the
    Item save itself never fails because of a push problem.

    Test safety: the auto_push_on_save flag defaults 0, so the test
    suite's many Item saves don't trigger any enqueue. Tests of the
    hook itself set the flag explicitly.
    """
    account_name = _account_with_auto_push_enabled()
    if not account_name:
        return
    if frappe.flags.get("in_easyecom_pull"):
        return
    if getattr(doc, "has_variants", 0):
        return
    enqueue_item_push(doc.item_code, account_name=account_name)


def enqueue_on_bundle_change(doc: Any, method: str | None = None, **_kwargs) -> None:
    """doc_event handler for Product Bundle.after_insert / on_update.

    A bundle save is a push trigger via its wrapper Item — push_one_item
    auto-dispatches to push_one_bundle when it sees a Product Bundle
    pointing at this item_code. Reuse the same enqueue function.
    """
    account_name = _account_with_auto_push_enabled()
    if not account_name:
        return
    if frappe.flags.get("in_easyecom_pull"):
        return
    wrapper_code = doc.new_item_code
    if not wrapper_code:
        return
    enqueue_item_push(wrapper_code, account_name=account_name)


def _account_with_auto_push_enabled() -> str | None:
    """The single enabled EasyEcom Account with auto_push_on_save=1.

    §8.1 assumes one EasyEcom Account per deployment (account-wide
    credentials, account-wide catalogue). If multiple are configured
    with auto_push on, returns the first by name — but flag the
    config as ambiguous via Error Log (FDE should disable one)."""
    rows = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1, "auto_push_on_save": 1},
        fields=["name"],
        order_by="name asc",
        limit=2,
    )
    if not rows:
        return None
    if len(rows) > 1:
        frappe.log_error(
            title="EasyEcom: multiple Accounts have auto_push_on_save=1",
            message=(
                f"Auto-push fires for the first by name: {rows[0].name}. "
                "Disable auto_push on the others to remove ambiguity."
            ),
        )
    return rows[0].name


# ============================================================
# Whitelist endpoints — manual FDE triggers (Stage 6)
# ============================================================


# Roles allowed to trigger a manual push (matches the pattern from
# discover_locations / discover_channels / discover_products).
_PUSH_ROLES: frozenset[str] = frozenset(
    {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
)


def _require_push_role() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(_PUSH_ROLES):
        frappe.throw(
            frappe._(
                "EasyEcom push actions require EasyEcom FDE or "
                "System Manager privilege."
            ),
            frappe.PermissionError,
        )


def _resolve_account(account: str | None = None) -> str:
    """Pick the EasyEcom Account name to use for the push.

    If `account` is passed (from a button click on a specific Account
    form), use it. Otherwise pick the single enabled Account — §8.1
    assumes one per deployment. Refuses ambiguity (zero or multiple)
    with a clean error rather than silently picking one."""
    if account:
        if not frappe.db.exists("EasyEcom Account", account):
            frappe.throw(
                frappe._("EasyEcom Account {0} not found.").format(account)
            )
        return account
    rows = frappe.db.get_all(
        "EasyEcom Account",
        filters={"enabled": 1},
        fields=["name"],
        order_by="name asc",
        limit=2,
    )
    if not rows:
        frappe.throw(
            frappe._("No enabled EasyEcom Account found.")
        )
    if len(rows) > 1:
        frappe.throw(
            frappe._(
                "Multiple enabled EasyEcom Accounts; pass `account` to "
                "disambiguate."
            )
        )
    return rows[0].name


@frappe.whitelist()
def push_one_product(item_code: str, account: str | None = None) -> dict[str, Any]:
    """FDE-facing wrapper around push_one_item / push_one_bundle.

    Used by the Item form's "Push to EasyEcom" button. Detects bundle
    wrappers and dispatches automatically (the underlying push_one_item
    handles that). Returns a JS-friendly dict; never raises through
    the whitelist boundary."""
    _require_push_role()
    if not item_code:
        return {"ok": False, "message": "item_code required"}
    if not frappe.db.exists("Item", item_code):
        return {
            "ok": False,
            "message": frappe._("Item {0} not found.").format(item_code),
        }
    account_name = _resolve_account(account)
    account_doc = frappe.get_doc("EasyEcom Account", account_name)
    client = EasyEcomClient(account=account_doc)
    try:
        outcome = push_one_item(
            item_code, client=client, account=account_doc
        )
    except Exception as exc:  # noqa: BLE001 — whitelist boundary
        frappe.log_error(
            title=f"EasyEcom push_one_product failed: {item_code}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "item_code": item_code,
            "message": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "item_code": item_code,
        "pushed": outcome.pushed,
        "operation": outcome.operation,
        "ee_product_id": outcome.ee_product_id,
        "flag_reasons": outcome.flag_reasons,
    }


@frappe.whitelist()
def push_lifecycle_product(
    item_code: str, account: str | None = None
) -> dict[str, Any]:
    """FDE-facing wrapper around push_lifecycle. For the "Sync
    lifecycle to EasyEcom" button on the Item form — sends
    ActivateDeactivateProduct based on current Item.disabled state."""
    _require_push_role()
    if not item_code:
        return {"ok": False, "message": "item_code required"}
    if not frappe.db.exists("Item", item_code):
        return {
            "ok": False,
            "message": frappe._("Item {0} not found.").format(item_code),
        }
    account_name = _resolve_account(account)
    account_doc = frappe.get_doc("EasyEcom Account", account_name)
    client = EasyEcomClient(account=account_doc)
    try:
        outcome = push_lifecycle(
            item_code, client=client, account=account_doc
        )
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=f"EasyEcom push_lifecycle failed: {item_code}",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "item_code": item_code,
        "pushed": outcome.pushed,
        "operation": outcome.operation,
        "flag_reasons": outcome.flag_reasons,
    }


@frappe.whitelist()
def push_all_pending_products(account: str) -> dict[str, Any]:
    """FDE-facing batch sweep — ENQUEUES (audit fix #8).

    Switched from inline-loop (which hung the FDE's browser through N
    sequential EE calls) to enqueue-one-job-per-item via
    enqueue_push_all_pending. Returns IMMEDIATELY with counts +
    sample of enqueued Queue Job names. The FDE then watches the
    Queue Job list / workspace number cards to see progress; each
    item's success or failure is independent.
    """
    _require_push_role()
    if not account:
        return {"ok": False, "message": "account required"}
    try:
        result = enqueue_push_all_pending(account_name=account)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title="EasyEcom push_all_pending_products failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "total_considered": result["total_considered"],
        "enqueued_count": result["enqueued_count"],
        "queue_job_names_sample": result["queue_job_names_sample"],
    }


# ============================================================
# Original Stage-3 queue worker
# ============================================================


def enqueue_item_push(item_code: str, *, account_name: str) -> str:
    """Queue-able worker entry for the individual-push trigger.

    Uses the §6.3.1 facade (enqueue_easyecom_job) rather than raw
    frappe.enqueue — the facade creates an EasyEcom Queue Job
    tracking row so the FDE can see push state in the desk, retry
    failures via the standard Queue Job retry button, and so the
    QUEUE_FOR_JOB_TYPE tier routing fires (job_type='Item Push' →
    'default' queue, 120s timeout per §31.4.3).

    Used by:
      - The auto-push hook (Item / Product Bundle doc_event)
      - The push-all-pending batch sweep (one job per candidate
        item — refactored from inline-loop to enqueued-per-item per
        the §8d audit follow-up so the FDE button returns
        immediately rather than blocking through 1000s of EE calls)
      - Drift resolution "Push ERPNext → EE" action on the Item Map
        form (re-asserts ERPNext as SoT via the same enqueued path)

    Returns the EasyEcom Queue Job docname.
    """
    payload = {"item_code": item_code, "account_name": account_name}
    company = _company_for_item_push(account_name)
    idem_key = _item_push_idempotency_key(
        item_code=item_code, account_name=account_name, company=company
    )
    return enqueue_easyecom_job(
        job_type="Item Push",
        company=company,
        target_doctype="Item",
        target_name=item_code,
        payload=payload,
        idempotency_key=idem_key,
    )


def item_push_queue_handler(qj: Any) -> None:
    """JOB_TYPE_HANDLERS['Item Push'] dispatch — workers.execute_job
    calls this with the loaded Queue Job doc.

    Reads target_name (item_code) + payload.account_name, builds the
    real client, calls push_one_item. Raises on EE error so the
    worker's retry/back-off disposition fires per §6.3.8."""
    payload = frappe.parse_json(qj.payload) if qj.payload else {}
    item_code = qj.target_name or payload.get("item_code")
    account_name = payload.get("account_name")
    if not item_code or not account_name:
        raise ValueError(
            f"Item Push job {qj.name} missing item_code or account_name in payload"
        )
    account = frappe.get_doc("EasyEcom Account", account_name)
    client = EasyEcomClient(account=account)
    push_one_item(item_code, client=client, account=account)


def _company_for_item_push(account_name: str) -> str:
    """Pick a Company for the Queue Job row.

    §8d items are account-wide (not per-Company), but the Queue Job
    DocType's company field is a Link to Company (real Company doc
    required by Frappe's link validation). Prefer the first enabled
    EasyEcom Company Settings; fall back to the first Company that
    exists in the site so the row can land. If no Company at all is
    configured (impossible on a real ERPNext site), raise — that's
    a pre-onboarding state where enqueueing can't proceed.
    """
    row = frappe.db.get_value(
        "EasyEcom Company Settings",
        {"enabled": 1},
        "company",
        order_by="company asc",
    )
    if row:
        return row
    fallback = frappe.db.get_value(
        "Company", filters={}, fieldname="name", order_by="creation asc"
    )
    if not fallback:
        raise RuntimeError(
            "Cannot enqueue Item Push: no Company exists on this site "
            "(pre-onboarding state). Create a Company first."
        )
    return fallback


def _item_push_idempotency_key(
    *, item_code: str, account_name: str, company: str
) -> str:
    """sha256('item:{company}:{item_code}:{account_name}:item_push_v1').

    Used as the Queue Job's idempotency_key so a duplicate hook fire
    (e.g. rapid double-save on the Item form) dedupes to one job.
    The inner EE call has its OWN idempotency_key (built by
    item_push._idempotency_key inside push_one_item) — those two are
    separate by design: the Queue-Job key dedupes Frappe-side
    workers; the EE-side key dedupes EE-side calls.
    """
    return item_push_key(
        company=company,
        item_code=item_code,
        ee_location_key=account_name,
        change_hash="item_push_v1",
    )


# ============================================================
# Payload assembly (engine + manufacturing)
# ============================================================


def build_push_payload(
    item: Any,
    *,
    executor: FieldMappingExecutor,
    enabled_companies: list[str],
) -> tuple[dict, list[str]]:
    """Translate item via the ruleset + compute TaxRate + check mandatories.

    Returns (payload, flag_reasons). flag_reasons non-empty → caller
    MUST NOT push; the item is FNC ("Flagged-Not-Pushed").

    Everything the FDE can configure is in the EasyEcom-Item-Push
    ruleset (edit in the desk; no code deploy):
      - Sku / ItemName / Description / ProductTaxCode / dimensions
        — direct mappings, identity / str_strip / float_to_str
      - **materialType (default 1)** — `conditional_constant` rule
      - **itemType (default 0)** — `conditional_constant` rule
      - **Brand fallback ("Unbranded")** — `custom_python` rule
      - **ModelNumber default (= item_code)** — `identity` rule with
        item_code as the source
      - **Cost fallback chain (ecs_ee_cost → valuation_rate → 0)** —
        `custom_python` rule

    Things THIS FLOW does (not ruleset, because they're not constants
    or simple per-field translations):
      - TaxRate resolution: looks up Item Tax Template's effective rate
        for the item's per-Company tax rows, snaps to EE's allowed set
        {0, 3, 5, 12, 18, 28}. Requires DB query + arithmetic.
      - Hard-mandatory presence checks for physical dims (Weight /
        Length / Height / Width). These are pre-flight payload-shape
        decisions, not translations — a missing-dim payload should
        never reach EE, but the rule decides whether to send is
        flow-level (we won't try to push a degenerate payload).
      - None-stripping (EE rejects null fields).
    """
    flag_reasons: list[str] = []

    # 1) All field-by-field translation lives in the ruleset.
    payload = executor.push(item)

    # 2) TaxRate — computed; if None, FLAG (hard mandatory, no defensible default).
    tax_rate = _resolve_tax_rate(item, enabled_companies=enabled_companies)
    if tax_rate is None:
        flag_reasons.append(
            "TaxRate cannot be resolved — item has no Item Tax row whose "
            "template resolves to one of EE's allowed rates "
            f"({EE_ALLOWED_TAX_RATES}). Configure the relevant Tax Rule "
            "Map (FDE: §8c desk) and re-push."
        )
    else:
        payload["TaxRate"] = tax_rate

    # 3) Hard-mandatory presence check for physical attributes.
    # Treat 0 / "0" / 0.0 / "0.0" / None / "" all as "not sourced".
    # ERPNext Float fields default to 0 when the user hasn't set them;
    # 0g / 0cm is a data hole for a physical product, not a real value
    # (Stage 4 may revisit this for digital products that legitimately
    # have zero weight — but the digital product type ships as
    # Flagged-Not-Created in Stage 2 pull, so it can't reach push here
    # in Stage 3).
    for fld in ("Weight", "Length", "Height", "Width"):
        if _is_missing_or_zero(payload.get(fld)):
            flag_reasons.append(
                f"{fld} missing or zero — required by EE for a physical "
                f"product. Set Item.{_erpnext_dim_field(fld)} and re-push."
            )

    # 4) Strip None/empty values — EE rejects null fields.
    payload = {k: v for k, v in payload.items() if v not in (None, "")}

    return payload, flag_reasons


def _erpnext_dim_field(ee_field: str) -> str:
    """Translate the EE dimension field name back to the ERPNext
    source field for the flag's "set X" message."""
    return {
        "Weight": "weight_per_unit",
        "Length": "ecs_length_cm",
        "Height": "ecs_height_cm",
        "Width": "ecs_width_cm",
    }.get(ee_field, ee_field)


def _is_missing_or_zero(v: Any) -> bool:
    """True if the value is None / empty / zero. Handles both numeric
    and string-of-numeric (the ruleset's float_to_str produces '0.0'
    for a default-zero Float field)."""
    if v in (None, ""):
        return True
    try:
        return float(v) == 0
    except (TypeError, ValueError):
        return False


def _resolve_tax_rate(item: Any, *, enabled_companies: list[str]) -> float | None:
    """Pick a single TaxRate for the EE payload.

    EE takes ONE TaxRate per product, but ERPNext can hold per-Company
    rows on item.taxes (the §8d Stage-2 multi-Co stamp). Picking
    strategy:
      1. Prefer the first enabled Company's row (deterministic).
      2. Fall back to any row whose template resolves cleanly.
      3. Snap the resolved decimal rate (e.g. 0.18) to EE's
         percentage-form allowed set ({0, 3, 5, 12, 18, 28}).
      4. If no row resolves, return None — caller flags.

    Cross-Company tax variance for the same item is a real-world FDE
    concern; Stage 3 picks deterministically and leaves the reconciliation
    visible via the (per-Company) Item Tax rows that remain on the Item.
    """
    if not item.get("taxes"):
        return None
    target_companies = enabled_companies or []
    rows = list(item.taxes)

    def _try_resolve_one(tax_row: Any) -> float | None:
        if not tax_row.item_tax_template:
            return None
        rate_dec = _effective_rate_for_template(tax_row.item_tax_template)
        if rate_dec is None:
            return None
        rate_pct = rate_dec * 100.0
        for allowed in EE_ALLOWED_TAX_RATES:
            if abs(rate_pct - allowed) <= TAX_RATE_TOLERANCE:
                return allowed
        return None

    # Pass 1: prefer the first enabled Company's row.
    for target in target_companies:
        for r in rows:
            owner = frappe.db.get_value(
                "Item Tax Template", r.item_tax_template, "company"
            )
            if owner != target:
                continue
            resolved = _try_resolve_one(r)
            if resolved is not None:
                return resolved

    # Pass 2: any row that resolves.
    for r in rows:
        resolved = _try_resolve_one(r)
        if resolved is not None:
            return resolved
    return None


# ============================================================
# EE call routing — Create / Update / writeback
# ============================================================


def _do_create(
    item: Any,
    payload: dict,
    *,
    client: EasyEcomClient,
    account: Any,
    existing_map: dict | None,
    enabled_companies: list[str],
) -> PushOutcome:
    """CreateMasterProduct + product_id writeback to map + Item.

    The mock client returns the same shape as EE: {data: {product_id: …}}.
    A missing product_id in the response is treated as a flag (EE
    accepted but didn't return what we need to identify the product
    later — equivalent to a failed map writeback)."""
    idem_key = _idempotency_key(item, payload, account)
    response = client.post(
        PRODUCT_MASTER_CREATE, payload=payload, idempotency_key=idem_key
    )
    returned_product_id = ((response or {}).get("data") or {}).get("product_id")
    if not returned_product_id:
        reasons = [
            f"CreateMasterProduct returned no product_id (response: {response!r}); "
            "FDE: investigate before re-pushing — the SKU may have been created "
            "EE-side without us learning its id."
        ]
        _upsert_map_row_flagged(item.item_code, reasons=reasons)
        return PushOutcome(
            item_code=item.item_code,
            pushed=False,
            operation="flagged",
            flag_reasons=reasons,
            ee_payload=payload,
        )

    product_id_str = str(returned_product_id)
    _upsert_map_row_after_create(
        item.item_code,
        ee_product_id=product_id_str,
        existing_map=existing_map,
    )
    # Stamp on the Item too for FDE visibility (Stage-2-style: same
    # field that the pull writes to).
    if item.get("ecs_ee_product_id") != product_id_str:
        item.db_set("ecs_ee_product_id", product_id_str, update_modified=False)

    return PushOutcome(
        item_code=item.item_code,
        pushed=True,
        operation="create",
        ee_product_id=product_id_str,
        ee_payload=payload,
    )


def _do_update(
    item: Any,
    payload: dict,
    *,
    client: EasyEcomClient,
    account: Any,
    ee_product_id: str,
    enabled_companies: list[str],
) -> PushOutcome:
    """UpdateMasterProduct keyed on productId. EE's update is partial-
    update-friendly; we send the full payload anyway because:
      - Partial-update semantics let EE no-op fields it already has;
      - Sending the full set keeps Create vs Update divergence to
        just the endpoint+key, not the payload shape — simpler test
        surface.
    """
    payload = dict(payload)
    payload["productId"] = ee_product_id
    idem_key = _idempotency_key(item, payload, account)
    client.post(PRODUCT_MASTER_UPDATE, payload=payload, idempotency_key=idem_key)
    return PushOutcome(
        item_code=item.item_code,
        pushed=True,
        operation="update",
        ee_product_id=ee_product_id,
        ee_payload=payload,
    )


# ============================================================
# Map row upsert
# ============================================================


def _upsert_map_row_after_create(
    item_code: str, *, ee_product_id: str, existing_map: dict | None
) -> str:
    """Write back the EE-returned product_id to the Item Map row.

    Three input shapes:
      - No map row exists → create a new Mapped row with the product_id.
      - Map row exists (e.g. from a previous flagged attempt) but no
        product_id → update it: status=Mapped, set ee_product_id, clear flag.
      - Map row exists WITH a product_id → shouldn't happen on a Create
        path (we route to Update), but defensively just refresh the id.
    """
    sku = item_code  # Stage 3: sku == item_code (ERPNext is the source)
    if existing_map and existing_map.get("name"):
        frappe.db.set_value(
            "EasyEcom Item Map",
            existing_map["name"],
            {
                "ee_product_id": ee_product_id,
                "status": STATUS_MAPPED,
                "flag_reason": None,
            },
            update_modified=True,
        )
        return existing_map["name"]

    doc = frappe.new_doc("EasyEcom Item Map")
    doc.update(
        {
            "ee_sku": sku,
            "erpnext_doctype": "Item",
            "erpnext_name": item_code,
            "ee_product_id": ee_product_id,
            "status": STATUS_MAPPED,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_map_row_flagged(item_code: str, *, reasons: list[str]) -> str:
    """Create or update the Item Map row in Flagged-Not-Created state
    because we COULDN'T push. Mirrors the Stage-2 pull's
    `_flag_not_created` helper but for the push side."""
    existing = frappe.db.get_value(
        "EasyEcom Item Map",
        {"erpnext_doctype": "Item", "erpnext_name": item_code},
        "name",
    )
    reason_str = " || ".join(reasons)
    if existing:
        frappe.db.set_value(
            "EasyEcom Item Map",
            existing,
            {"status": STATUS_FLAGGED_NOT_PUSHED, "flag_reason": reason_str},
            update_modified=True,
        )
        return existing
    doc = frappe.new_doc("EasyEcom Item Map")
    doc.update(
        {
            "ee_sku": item_code,
            "erpnext_doctype": "Item",
            "erpnext_name": item_code,
            "status": STATUS_FLAGGED_NOT_PUSHED,
            "flag_reason": reason_str,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


# ============================================================
# Helpers
# ============================================================


def _enabled_companies() -> list[str]:
    rows = frappe.db.get_all(
        "EasyEcom Company Settings",
        filters={"enabled": 1},
        fields=["company"],
        order_by="company asc",
    )
    return [r.company for r in rows if r.company]


def _candidate_items_for_sweep(limit: int | None = None) -> list[str]:
    """Items the onboarding sweep should consider — see push_all_pending
    docstring for the policy."""
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    rows = frappe.db.sql(
        f"""
        SELECT i.item_code
        FROM `tabItem` i
        LEFT JOIN `tabEasyEcom Item Map` m
            ON m.erpnext_doctype = 'Item'
            AND m.erpnext_name = i.item_code
            AND m.ee_product_id IS NOT NULL
            AND m.ee_product_id != ''
        LEFT JOIN `tabProduct Bundle` pb
            ON pb.new_item_code = i.item_code
        WHERE i.disabled = 0
          AND i.is_stock_item = 1
          AND i.gst_hsn_code IS NOT NULL
          AND i.gst_hsn_code != ''
          AND m.name IS NULL
          AND pb.name IS NULL
        ORDER BY i.creation ASC
        {limit_clause}
        """,
        as_dict=True,
    )
    return [r.item_code for r in rows]


def _idempotency_key(item: Any, payload: dict, account: Any) -> str:
    """Per-§6.1: sha256('item:{company}:{item_code}:{ee_location_key}:{change_hash}').

    'company' is the first enabled EasyEcom Company Settings — push is
    account-wide but the idempotency dimension is per-Company by spec.
    'change_hash' is sha256 of the serialised payload so the same
    payload → same key (idempotent retry-safe), a changed payload →
    new key (Update is allowed to proceed)."""
    enabled = _enabled_companies()
    company = enabled[0] if enabled else "shared"
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    change_hash = hashlib.sha256(payload_bytes).hexdigest()
    return item_push_key(
        company=company,
        item_code=item.item_code,
        ee_location_key=account.get("default_location_key") or "account-wide",
        change_hash=change_hash,
    )
