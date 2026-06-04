"""Sync Address records from EasyEcom Location → linked Warehouse.

Source-of-truth contract:
  EasyEcom Location holds the address data (city / state / country /
  pincode / address_line / gstin — supplied by /getAllLocation +
  FDE-set GSTIN). When a Location is mapped to a Warehouse, the
  substrate mirrors that data onto an Address record linked to the
  Warehouse via Dynamic Link.

  The mirrored Address carries `ecs_ee_location = <Location name>`
  as a back-pointer. The Address form JS reads that field, locks the
  address fields, and shows a banner directing the FDE to edit on
  the Location side. Manual edits get clobbered on the next Location
  save by design — single source of truth.

Gating:
  Sync runs when:
    - workflow_state ∈ {Mapped but not Live, Live}
    - mapped_warehouse is set
    - At least one address field has data (city / address_line)
  Otherwise the existing managed Address (if any) is left in place —
  we don't proactively destroy on workflow regressions.

The §10 substrate's `_resolve_warehouse_address` reads any Address
linked to the Warehouse with a non-empty GSTIN; the EE-managed
Address satisfies that query when the Location has a GSTIN set, so
the §10 GST routing gets correct addresses automatically once a
Location is mapped + live.
"""

from __future__ import annotations

from typing import Any

import frappe

LIVE_STATES: frozenset[str] = frozenset({"Mapped but not Live", "Live"})
ADDRESS_TITLE_SUFFIX: str = " (EE)"


def _should_sync(loc: Any) -> bool:
    if not loc:
        return False
    if (loc.workflow_state or "") not in LIVE_STATES:
        return False
    if not loc.mapped_warehouse:
        return False
    # No address data → no sync (avoid creating empty Address shells).
    if not (loc.city or loc.address_line):
        return False
    return True


def _find_managed_address(location_name: str) -> str | None:
    return frappe.db.get_value(
        "Address", {"ecs_ee_location": location_name}, "name"
    )


def _upsert_warehouse_address(*, loc: Any, warehouse: str) -> str | None:
    """Create or refresh the Address mirrored from this Location, link
    it to the Warehouse via Dynamic Link, and stamp the back-pointer."""
    address_title = (
        frappe.db.get_value("Warehouse", warehouse, "warehouse_name")
        or warehouse
    ) + ADDRESS_TITLE_SUFFIX

    existing = _find_managed_address(loc.name)

    # Pre-validate GSTIN against India Compliance. The Location may
    # carry a malformed GSTIN (e.g. synthetic test data, or a typo the
    # FDE will fix later) — we don't want a bad GSTIN to block the
    # whole Address sync, since city/state/pincode are still useful
    # for §10 (Company GSTIN fallback covers tax routing).
    gstin_raw = (loc.gstin or "").strip()
    gstin_valid = ""
    if gstin_raw:
        try:
            from india_compliance.gst_india.utils import validate_gstin
            validate_gstin(gstin_raw, label="GSTIN")
            gstin_valid = gstin_raw
        except Exception as exc:
            frappe.logger().warning(
                f"[ecs] Location {loc.name} GSTIN {gstin_raw!r} failed "
                f"IC validation, skipping GSTIN on Address: {exc}"
            )

    payload: dict[str, Any] = {
        "address_title": address_title,
        "address_type": "Shipping",
        "address_line1": (loc.address_line or "").strip() or address_title,
        "address_line2": "",
        "city": (loc.city or "").strip(),
        "state": (loc.state or "").strip(),
        "country": (loc.country or "India").strip(),
        "pincode": (loc.pincode or "").strip(),
        "gstin": gstin_valid,
        "ecs_ee_location": loc.name,
        # gh#24: ERPNext seeds is_your_company_address as a Custom
        # Field on Address; ERPNextAddress.validate_reference reads it.
        # On sites where the IC / ERPNext custom-field migration hasn't
        # run, the attribute is missing and validate() AttributeError's.
        # Warehouse-linked addresses are never "Company addresses" in
        # the IC sense (they hold the warehouse's GSTIN, not the
        # Company's primary registered office), so 0 is correct.
        "is_your_company_address": 0,
    }
    # GST category — Registered Regular when a VALID GSTIN was kept;
    # otherwise leave whatever's there (don't force Unregistered on
    # Indian addrs we don't have GSTIN for yet — FDE may still be
    # filling it in).
    if gstin_valid:
        payload["gst_category"] = "Registered Regular"

    if existing:
        addr = frappe.get_doc("Address", existing)
        for k, v in payload.items():
            addr.set(k, v)
        # Ensure the Dynamic Link to this Warehouse exists (in case the
        # mapping was re-pointed to a different Warehouse — drop stale
        # links + add the current one).
        _reconcile_warehouse_link(addr, warehouse)
        addr.flags.ignore_permissions = True
        addr.save()
        return addr.name

    addr = frappe.new_doc("Address")
    for k, v in payload.items():
        addr.set(k, v)
    addr.append(
        "links",
        {"link_doctype": "Warehouse", "link_name": warehouse},
    )
    addr.flags.ignore_permissions = True
    addr.insert()
    return addr.name


def _reconcile_warehouse_link(addr: Any, warehouse: str) -> None:
    """Drop any Warehouse links on this Address that don't match the
    current mapped_warehouse, then ensure the current one is present.
    Other link types (Company, Customer, etc.) are left untouched."""
    new_links: list[dict] = []
    seen_current = False
    for row in addr.links or []:
        if row.link_doctype == "Warehouse":
            if row.link_name == warehouse:
                new_links.append(row)
                seen_current = True
            # else: drop — Warehouse re-pointed to a different one.
        else:
            new_links.append(row)
    addr.set("links", new_links)
    if not seen_current:
        addr.append(
            "links",
            {"link_doctype": "Warehouse", "link_name": warehouse},
        )


# ============================================================
# Hook entrypoints — wired in hooks.py doc_events.
# ============================================================


def sync_on_location_save(doc: Any, method: str | None = None) -> None:
    """Run after EasyEcom Location is saved. Upsert the mirrored
    Address on the current mapped_warehouse when gating passes."""
    if not _should_sync(doc):
        return
    try:
        _upsert_warehouse_address(loc=doc, warehouse=doc.mapped_warehouse)
    except Exception as exc:
        # Don't let an Address sync failure block the Location save —
        # log and move on. The FDE can re-trigger by re-saving the
        # Location once the underlying issue (e.g. India Compliance
        # GSTIN validation) is fixed.
        frappe.log_error(
            title=f"EE Location → Warehouse Address sync failed: {doc.name}",
            message=f"{type(exc).__name__}: {exc}",
        )


# ============================================================
# Backfill — called by the one-shot patch.
# ============================================================


def backfill_all() -> dict[str, int]:
    """Walk every Live + enabled Location and upsert its mirrored
    Address. Idempotent."""
    locs = frappe.db.get_all(
        "EasyEcom Location",
        filters={"workflow_state": "Live", "enabled": 1},
        fields=["name"],
    )
    synced = 0
    skipped = 0
    failed = 0
    for row in locs:
        loc = frappe.get_doc("EasyEcom Location", row["name"])
        if not _should_sync(loc):
            skipped += 1
            continue
        try:
            _upsert_warehouse_address(loc=loc, warehouse=loc.mapped_warehouse)
            synced += 1
        except Exception as exc:
            failed += 1
            frappe.log_error(
                title=f"Backfill: {loc.name}",
                message=f"{type(exc).__name__}: {exc}",
            )
    frappe.db.commit()
    return {
        "locations_scanned": len(locs),
        "addresses_synced": synced,
        "skipped_missing_data": skipped,
        "failed": failed,
    }
