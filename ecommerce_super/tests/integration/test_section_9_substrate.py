"""§9 Stage 1 substrate tests — PO Map + GRN Map + Sync Record Line.

Stage 1 is DocTypes + schema + ruleset repoints + one settings field.
NO flow logic, NO EE calls, NO Purchase Receipts. Stages 2-4 wire the
actual push / pull / reconciliation / UI flows.

Mirrors test_supplier_map_substrate.py for structure. Covers:
  - PO Map DocType: schema invariants, status enum, two-key model
    (reference_code + ee_po_id), drift-status surface, shared
    Drift/Exclude child references, controller validation.
  - GRN Map DocType: schema invariants, status enum (incl. STN-Routed
    / Held-Pre-QC / Deleted-Post-Receipt), grn_status_id range,
    STN-routing consistency guard, three-key resolution surface
    (warehouse / vendor / PO).
  - Sync Record Line: source_line_number Int field added in §9, all
    other §7.1-shipped fields still present.
  - Sync Record: `lines` child table referencing Sync Record Line,
    empty-list is valid (parity with §8 masters).
  - GRN/Inward Policy: grn_receipt_trigger_status select field with
    options 1/2/3 and default '3 QC Complete'.
  - Field-mapping rulesets: PO-Push targets Supplier Map.ee_vendor_id
    (WRITE key); GRN-Pull targets Supplier Map.ee_vendor_c_id (READ
    key); both use the new lookup_field transformer.
  - Engine: lookup_field transformer is registered and behaves.

§8d standing failures (test_item_pull_stage2, test_item_lifecycle_drift_stage5)
are pre-existing and NOT touched here.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_po_map.easyecom_po_map import (
    VALID_STATUS_VALUES as PO_MAP_STATUSES,
)
from ecommerce_super.easyecom.doctype.easyecom_grn_map.easyecom_grn_map import (
    VALID_GRN_STATUS_IDS,
    VALID_STATUS_VALUES as GRN_MAP_STATUSES,
)
from ecommerce_super.easyecom.exceptions import FieldMappingRuleError
from ecommerce_super.easyecom.field_mapping.transformers import (
    TRANSFORMERS,
    TransformContext,
    apply_transformer,
)


_PREFIX = "TEST-S9-S1-"


def _wipe_section9_maps() -> None:
    for n in frappe.db.get_all(
        "EasyEcom PO Map",
        filters={"reference_code": ("like", f"{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom PO Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    for n in frappe.db.get_all(
        "EasyEcom GRN Map",
        filters={"grn_invoice_number": ("like", f"{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom GRN Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


class TestPOMapSchema(FrappeTestCase):
    """The DocType exists, status enum matches the §9 packet, the
    two-key model is wired (reference_code unique-required content-key,
    ee_po_id nullable status-key), and the drift surface reuses §8f's
    shared child DocTypes."""

    def test_doctype_exists(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom PO Map"))

    def test_status_enum_is_packet_spec(self) -> None:
        meta = frappe.get_meta("EasyEcom PO Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(opts, PO_MAP_STATUSES)
        self.assertEqual(
            opts,
            {"Mapped", "Created-Flagged", "Flagged-Not-Created", "Drift", "Disabled"},
            "§9 PO Map enum must mirror the §8 master maps verbatim",
        )

    def test_autoname_uses_purchase_order(self) -> None:
        meta = frappe.get_meta("EasyEcom PO Map")
        self.assertEqual(
            meta.autoname,
            "format:ECS-PO-{purchase_order}",
            "§9 packet: autoname stamps name = ECS-PO-{linked PO name}",
        )

    def test_reference_code_is_unique_and_required(self) -> None:
        """Content-channel key — = linked PO name. UNIQUE + reqd."""
        meta = frappe.get_meta("EasyEcom PO Map")
        f = meta.get_field("reference_code")
        self.assertTrue(f.reqd, "reference_code must be required")
        self.assertTrue(f.unique, "reference_code must be UNIQUE at the DB level")

    def test_ee_po_id_field_exists_nullable_not_unique(self) -> None:
        """Status-channel key — NULL until first CreatePurchaseOrder
        returns data.poId. Captured separately; not unique at this layer
        (EE-side uniqueness is EE's problem)."""
        meta = frappe.get_meta("EasyEcom PO Map")
        f = meta.get_field("ee_po_id")
        self.assertIsNotNone(f, "ee_po_id field must exist on PO Map")
        self.assertFalse(
            f.reqd, "ee_po_id must NOT be reqd — captured post-first-push"
        )
        self.assertFalse(
            f.unique,
            "ee_po_id should not assert DB UNIQUE here; EE owns its int uniqueness",
        )

    def test_purchase_order_link_field_exists(self) -> None:
        meta = frappe.get_meta("EasyEcom PO Map")
        f = meta.get_field("purchase_order")
        self.assertIsNotNone(f)
        self.assertEqual(f.fieldtype, "Link")
        self.assertEqual(f.options, "Purchase Order")
        self.assertTrue(f.reqd)

    def test_status_drift_surface_fields_present(self) -> None:
        """§9 drift on PO Map is status-only by design (PO docs
        submitted-immutable in ERPNext). Schema must expose
        last_pushed_po_status + ee_observed_po_status + ee_observed_at
        but NO content_snapshot."""
        meta = frappe.get_meta("EasyEcom PO Map")
        for fname in (
            "last_pushed_po_status",
            "ee_observed_po_status",
            "ee_observed_at",
        ):
            self.assertIsNotNone(
                meta.get_field(fname), f"PO Map must expose {fname}"
            )
        self.assertIsNone(
            meta.get_field("content_snapshot"),
            "No content_snapshot field on PO Map — PO content is "
            "submitted-immutable in ERPNext (§9 packet)",
        )

    def test_drift_exclude_children_use_shared_doctypes(self) -> None:
        """§9 reuses §8f's shared Drift Field / Exclude Field child
        DocTypes — no new PO-specific children."""
        meta = frappe.get_meta("EasyEcom PO Map")
        self.assertEqual(
            meta.get_field("drift_fields").options, "EasyEcom Drift Field"
        )
        self.assertEqual(
            meta.get_field("exclude_fields").options, "EasyEcom Exclude Field"
        )


class TestPOMapPermissions(FrappeTestCase):
    def test_perms_mirror_supplier_map(self) -> None:
        """Permissions roster matches §8 master maps: Operator read-only,
        FDE r/w/c, EasyEcom System Manager + System Manager full."""
        meta = frappe.get_meta("EasyEcom PO Map")
        perms_by_role = {p.role: p for p in meta.permissions}
        self.assertIn("EasyEcom Operator", perms_by_role)
        self.assertTrue(perms_by_role["EasyEcom Operator"].read)
        self.assertFalse(perms_by_role["EasyEcom Operator"].write)
        self.assertIn("EasyEcom FDE", perms_by_role)
        self.assertTrue(perms_by_role["EasyEcom FDE"].write)
        self.assertTrue(perms_by_role["EasyEcom FDE"].create)
        self.assertIn("EasyEcom System Manager", perms_by_role)
        self.assertTrue(perms_by_role["EasyEcom System Manager"].delete)


class TestPOMapValidation(FrappeTestCase):
    """Controller-level guards — status enum, link target existence,
    reference_code ↔ purchase_order lockstep."""

    def tearDown(self) -> None:
        _wipe_section9_maps()
        frappe.db.rollback()

    def test_validate_refuses_unknown_status(self) -> None:
        doc = frappe.new_doc("EasyEcom PO Map")
        doc.update(
            {
                "reference_code": f"{_PREFIX}PO-001",
                "purchase_order": f"{_PREFIX}PO-001",
                "status": "Bogus",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_validate_refuses_nonexistent_purchase_order(self) -> None:
        doc = frappe.new_doc("EasyEcom PO Map")
        doc.update(
            {
                "reference_code": f"{_PREFIX}GHOST",
                "purchase_order": f"{_PREFIX}GHOST",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_validate_refuses_reference_code_decoupled_from_po(self) -> None:
        """The §9 push keys on reference_code = PO name. They must stay
        in lockstep; out-of-band edits that decouple them are refused."""
        doc = frappe.new_doc("EasyEcom PO Map")
        doc.update(
            {
                "reference_code": "MISMATCHED",
                "purchase_order": f"{_PREFIX}PO-001",
                "status": "Mapped",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()


class TestGRNMapSchema(FrappeTestCase):
    def test_doctype_exists(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "EasyEcom GRN Map"))

    def test_status_enum_is_packet_spec(self) -> None:
        meta = frappe.get_meta("EasyEcom GRN Map")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(opts, GRN_MAP_STATUSES)
        # The 7-state set per packet step (5) PLUS the 'Dismissed'
        # state added by the 2026-05-29 corrective commit (FIX 1) —
        # FDE-closed unknown-PO drift. The packet's Open Decision #4
        # (resolved 2026-05-28) authorises the FDE dismiss action;
        # this dedicated state lets the worklist filter "open drift"
        # without lumping in closed dismissals.
        self.assertEqual(
            opts,
            {
                "Pending",
                "Receipted",
                "Held-Pre-QC",
                "STN-Routed",
                "Failed",
                "Discrepancy",
                "Deleted-Post-Receipt",
                "Dismissed",
            },
            "§9 GRN Map enum must match the 7-state packet set + "
            "Dismissed (FIX 1 corrective).",
        )

    def test_autoname_uses_ee_grn_id(self) -> None:
        meta = frappe.get_meta("EasyEcom GRN Map")
        self.assertEqual(meta.autoname, "format:ECS-GRN-{ee_grn_id}")

    def test_ee_grn_id_is_unique_required_int(self) -> None:
        meta = frappe.get_meta("EasyEcom GRN Map")
        f = meta.get_field("ee_grn_id")
        self.assertEqual(f.fieldtype, "Int")
        self.assertTrue(f.reqd, "ee_grn_id must be required — it's the natural PK")
        self.assertTrue(
            f.unique, "ee_grn_id must be UNIQUE — idempotency hinge for the pull"
        )

    def test_resolution_surface_fields(self) -> None:
        """Three keys earn their place: warehouse / vendor / PO. The
        warehouse + vendor are int (EE c_id-style); the PO has two
        variants (free-text po_ref_num + int ee_po_id fallback)."""
        meta = frappe.get_meta("EasyEcom GRN Map")
        for fname in (
            "inwarded_warehouse_c_id",
            "vendor_c_id",
            "po_ref_num",
            "ee_po_id",
        ):
            self.assertIsNotNone(
                meta.get_field(fname), f"GRN Map must expose {fname}"
            )

    def test_routed_to_stn_is_check_default_zero(self) -> None:
        meta = frappe.get_meta("EasyEcom GRN Map")
        f = meta.get_field("routed_to_stn")
        self.assertEqual(f.fieldtype, "Check")
        self.assertEqual(str(f.default), "0")

    def test_purchase_receipt_and_linked_po_map_links(self) -> None:
        meta = frappe.get_meta("EasyEcom GRN Map")
        pr = meta.get_field("purchase_receipt")
        self.assertEqual(pr.fieldtype, "Link")
        self.assertEqual(pr.options, "Purchase Receipt")
        po = meta.get_field("linked_po_map")
        self.assertEqual(po.fieldtype, "Link")
        self.assertEqual(po.options, "EasyEcom PO Map")


class TestGRNMapValidation(FrappeTestCase):
    def tearDown(self) -> None:
        _wipe_section9_maps()
        frappe.db.rollback()

    def test_refuses_unknown_status(self) -> None:
        doc = frappe.new_doc("EasyEcom GRN Map")
        doc.update({"ee_grn_id": 911001, "status": "WhoKnows"})
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_refuses_grn_status_id_outside_valid_range(self) -> None:
        """Live finding 2026-05-28: real Harmony GRNs have
        grn_status_id=5 (Completed/Closed beyond QC). The §9 packet's
        documented {1,2,3,4} enum was incomplete. Validator widened
        to {1..10}; clearly bogus values (0, negative, >10) still
        rejected."""
        doc = frappe.new_doc("EasyEcom GRN Map")
        # 99 — clearly out of range, rejected.
        doc.update({"ee_grn_id": 911002, "status": "Pending", "grn_status_id": 99})
        with self.assertRaises(frappe.ValidationError):
            doc.validate()
        # 0 also rejected.
        doc.grn_status_id = 0
        with self.assertRaises(frappe.ValidationError):
            doc.validate()
        # 5 (Harmony-observed Completed) — accepted.
        doc.grn_status_id = 5
        doc.validate()
        # NULL is fine (substrate may insert before pull fills the header).
        doc.grn_status_id = None
        doc.validate()  # no raise

    def test_stn_routing_requires_both_flag_and_status(self) -> None:
        """Inconsistent combinations are rejected: routed_to_stn=1
        without status=STN-Routed, and vice versa."""
        doc = frappe.new_doc("EasyEcom GRN Map")
        doc.update(
            {"ee_grn_id": 911003, "status": "STN-Routed", "routed_to_stn": 0}
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

        doc2 = frappe.new_doc("EasyEcom GRN Map")
        doc2.update(
            {"ee_grn_id": 911004, "status": "Pending", "routed_to_stn": 1}
        )
        with self.assertRaises(frappe.ValidationError):
            doc2.validate()

    def test_stn_routed_cannot_have_purchase_receipt(self) -> None:
        """STN-Routed GRNs go through §10, not §9 — §9 creates NO PR."""
        doc = frappe.new_doc("EasyEcom GRN Map")
        doc.update(
            {
                "ee_grn_id": 911005,
                "status": "STN-Routed",
                "routed_to_stn": 1,
                "purchase_receipt": "GHOST-PR",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()


class TestSyncRecordLineSchema(FrappeTestCase):
    """The Sync Record Line child DocType ships with §7.1; §9 adds
    source_line_number. The other fields are §7.1's contract — this
    test pins them so a future refactor doesn't silently drop one."""

    def test_doctype_exists_as_child(self) -> None:
        self.assertTrue(
            frappe.db.exists("DocType", "EasyEcom Sync Record Line")
        )
        meta = frappe.get_meta("EasyEcom Sync Record Line")
        self.assertTrue(meta.istable, "EasyEcom Sync Record Line must be a child DocType")

    def test_source_line_number_added_for_section_9(self) -> None:
        meta = frappe.get_meta("EasyEcom Sync Record Line")
        f = meta.get_field("source_line_number")
        self.assertIsNotNone(
            f,
            "§9 Stage 1 adds source_line_number (Int) so flows can correlate "
            "rows back to upstream payload ordinals (e.g. GRN line index)",
        )
        self.assertEqual(f.fieldtype, "Int")

    def test_section_7_1_fields_still_present(self) -> None:
        """Regression: §9 must not have inadvertently renamed or dropped
        the original §7.1 fields. The §9 patch-notes record the
        decision to KEEP the existing field names (target_field,
        line_status, ecs_integration_discrepancy) rather than renaming
        to the §9-packet-style names — they're already wired."""
        meta = frappe.get_meta("EasyEcom Sync Record Line")
        for fname in (
            "source_line_ref",
            "target_field",
            "line_status",
            "reason",
            "ecs_integration_discrepancy",
        ):
            self.assertIsNotNone(
                meta.get_field(fname),
                f"§7.1 line field {fname!r} must remain on Sync Record Line",
            )

    def test_line_status_enum(self) -> None:
        meta = frappe.get_meta("EasyEcom Sync Record Line")
        opts = set((meta.get_field("line_status").options or "").split("\n"))
        self.assertEqual(opts, {"OK", "Failed", "Discrepancy"})


class TestSyncRecordLinesChildTableParity(FrappeTestCase):
    """The Sync Record `lines` child table was added at §7.1 amendment
    time. §8 master Sync Records must still be insertable with the table
    empty — that's the parity-check the prompt asks for."""

    def test_master_sync_record_inserts_with_empty_lines(self) -> None:
        company = (
            frappe.db.get_value("Company", filters={}, fieldname="name")
            or "Test Company"
        )
        # An item-master flow Sync Record — the §8 shape, no nested lines.
        sr = frappe.new_doc("EasyEcom Sync Record")
        sr.update(
            {
                "entity_type": "Item",
                "entity_doctype": "Item",
                "entity_name": "TEST-S9-S1-ITEM",
                "direction": "Pull",
                "status": "Success",
                "outcome_reason": "Stage 1 parity check",
                "company": company,
                "correlation_id": "test-s9-s1-corr-001",
                "idempotency_key": "test-s9-s1-idem-001",
                "easyecom_account": frappe.db.get_value(
                    "EasyEcom Account", filters={}, fieldname="name"
                )
                or "test-account",
            }
        )
        try:
            # ignore_links: the linked Item is a real-data link; this
            # test only cares about the lines-child-table contract.
            sr.insert(ignore_permissions=True, ignore_links=True)
            self.assertEqual(
                len(sr.lines or []),
                0,
                "§8 master shape: `lines` child table empty by contract",
            )
        finally:
            try:
                frappe.delete_doc(
                    "EasyEcom Sync Record",
                    sr.name,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass
            frappe.db.rollback()


class TestGRNInwardPolicySettings(FrappeTestCase):
    """The §9 Stage 1 GRN/Inward Policy field — grn_receipt_trigger_status
    — must exist on EasyEcom Account, be Select-typed with three options,
    and default to '3 QC Complete' (the safe default per packet)."""

    def test_field_exists_with_three_options(self) -> None:
        meta = frappe.get_meta("EasyEcom Account")
        f = meta.get_field("grn_receipt_trigger_status")
        self.assertIsNotNone(f, "grn_receipt_trigger_status missing")
        self.assertEqual(f.fieldtype, "Select")
        opts = (f.options or "").split("\n")
        self.assertEqual(
            opts,
            ["1 CREATED", "2 QC Pending", "3 QC Complete"],
            "§9 Stage 1: three-option select 1 / 2 / 3 in lifecycle order",
        )

    def test_default_is_qc_complete(self) -> None:
        meta = frappe.get_meta("EasyEcom Account")
        f = meta.get_field("grn_receipt_trigger_status")
        self.assertEqual(f.default, "3 QC Complete")

    def test_is_required(self) -> None:
        meta = frappe.get_meta("EasyEcom Account")
        f = meta.get_field("grn_receipt_trigger_status")
        self.assertTrue(f.reqd, "grn_receipt_trigger_status is reqd (§9 packet)")


class TestGRNPullSchedulerIntentionallyUnwired(FrappeTestCase):
    """§9 Stage 4 — the GRN-pull cron is INTENTIONALLY not registered.
    Auto-firing on an Account with NULL grn_pull_high_watermark would
    drag in 7 days of historical GRNs from EE's backstop and create
    PRs for already-manually-receipted POs. The handler ships; the
    cron auto-fire is deferred to §9 closeout (separate packet) once
    the cold-start safety gates land. This test pins the deferral so
    a future contributor doesn't silently restore the cron and ship
    an unsafe surprise on the next migrate."""

    def test_grn_pull_cron_is_NOT_registered(self) -> None:
        from ecommerce_super.hooks import scheduler_events

        cron = scheduler_events.get("cron") or {}
        target = (
            "ecommerce_super.easyecom.flows."
            "grn_pull.scheduled_grn_pull"
        )
        all_entries = [
            (sched, fn)
            for sched, fns in cron.items()
            for fn in fns
            if fn == target
        ]
        self.assertEqual(
            len(all_entries),
            0,
            f"scheduled_grn_pull is wired into cron — found "
            f"{all_entries}. This was deliberately deferred to the "
            "§9 closeout pending cold-start safety gates. If you're "
            "re-wiring, make sure the gates landed first AND update "
            "this test (rename to TestGRNPullSchedulerRegistration).",
        )

    def test_grn_pull_handler_still_callable(self) -> None:
        """The handler itself ships and is manually invokable —
        only the cron auto-fire is deferred."""
        from ecommerce_super.easyecom.flows import grn_pull

        self.assertTrue(
            callable(grn_pull.scheduled_grn_pull),
            "scheduled_grn_pull must remain manually callable.",
        )


class TestRulesetRepoints(FrappeTestCase):
    """§9 Stage 1 repoints — PO-Push targets Supplier Map.ee_vendor_id
    (WRITE key); GRN-Pull targets Supplier Map.ee_vendor_c_id (READ key).
    Verified against the loaded fixture (after migrate)."""

    def _get_ruleset(self, name: str) -> dict | None:
        if not frappe.db.exists("EasyEcom Field Mapping", name):
            return None
        return frappe.get_doc("EasyEcom Field Mapping", name).as_dict()

    def test_po_push_supplier_rule_uses_lookup_field(self) -> None:
        rs = self._get_ruleset("EasyEcom-PO-Push")
        if rs is None:
            self.skipTest(
                "EasyEcom-PO-Push ruleset not loaded (run `bench migrate` first)"
            )
        sup_rules = [r for r in rs["rules"] if r.get("erpnext_path") == "supplier"]
        self.assertEqual(len(sup_rules), 1, "exactly one supplier rule expected")
        r = sup_rules[0]
        self.assertEqual(
            r["transform_push"],
            "lookup_field",
            "§9 Stage 1 repoint: supplier resolves via Supplier Map lookup",
        )
        args = r.get("transform_args") or {}
        if isinstance(args, str):
            import json as _json

            args = _json.loads(args)
        self.assertEqual(args.get("doctype"), "EasyEcom Supplier Map")
        self.assertEqual(args.get("filter_field"), "erpnext_name")
        self.assertEqual(
            args.get("target_field"),
            "ee_vendor_id",
            "PO push must target the WRITE key (ee_vendor_id), not the READ key",
        )

    def test_grn_pull_supplier_rule_uses_read_key(self) -> None:
        rs = self._get_ruleset("EasyEcom-GRN-Pull")
        if rs is None:
            self.skipTest("EasyEcom-GRN-Pull ruleset not loaded")
        sup_rules = [r for r in rs["rules"] if r.get("erpnext_path") == "supplier"]
        self.assertEqual(len(sup_rules), 1)
        r = sup_rules[0]
        self.assertEqual(r["transform_pull"], "lookup_field")
        args = r.get("transform_args") or {}
        if isinstance(args, str):
            import json as _json

            args = _json.loads(args)
        self.assertEqual(args.get("doctype"), "EasyEcom Supplier Map")
        self.assertEqual(
            args.get("filter_field"),
            "ee_vendor_c_id",
            "GRN pull must filter on the READ key (ee_vendor_c_id), different from "
            "PO push by design (§8.3 two-id model)",
        )
        self.assertEqual(args.get("target_field"), "erpnext_name")

    def test_no_stale_sync_rulesets_for_po_or_grn(self) -> None:
        """Parity with §8e/§8f retirements: there must be no
        EasyEcom-PO-Sync / EasyEcom-GRN-Sync bidirectional rulesets
        active. (None ever existed; this guards against future
        accidental re-introduction.)"""
        for stale in ("EasyEcom-PO-Sync", "EasyEcom-GRN-Sync"):
            row = frappe.db.get_value(
                "EasyEcom Field Mapping", stale, ["active"], as_dict=True
            )
            if row is not None:
                self.assertEqual(
                    int(row.active or 0),
                    0,
                    f"{stale} ruleset must be inactive if present at all",
                )


class TestLookupFieldTransformer(FrappeTestCase):
    """The new lookup_field transformer is registered, validates its
    args, and resolves Map-row → target field correctly."""

    def test_registered_in_transformer_registry(self) -> None:
        self.assertIn("lookup_field", TRANSFORMERS)

    def test_resolves_supplier_map_to_ee_vendor_id(self) -> None:
        """Insert a Supplier Map row; verify lookup_field returns the
        right field. Uses a real Supplier Map so the test exercises the
        DB query path."""
        # Setup: make a Supplier + Supplier Map.
        supplier_name = f"{_PREFIX}LookupSup"
        if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
            root = frappe.new_doc("Supplier Group")
            root.update({"supplier_group_name": "All Supplier Groups", "is_group": 1})
            root.insert(ignore_permissions=True)
        sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        if not sg:
            sg_doc = frappe.new_doc("Supplier Group")
            sg_doc.update(
                {
                    "supplier_group_name": f"{_PREFIX}SG",
                    "parent_supplier_group": "All Supplier Groups",
                    "is_group": 0,
                }
            )
            sg_doc.insert(ignore_permissions=True)
            sg = sg_doc.name
        if not frappe.db.exists("Supplier", supplier_name):
            s = frappe.new_doc("Supplier")
            s.update(
                {
                    "supplier_name": supplier_name,
                    "supplier_type": "Company",
                    "supplier_group": sg,
                    "country": "India",
                }
            )
            s.insert(ignore_permissions=True)
        sup_docname = frappe.db.get_value(
            "Supplier", {"supplier_name": supplier_name}, "name"
        )

        if not frappe.db.exists(
            "EasyEcom Supplier Map", {"ee_vendor_c_id": "99000001"}
        ):
            m = frappe.new_doc("EasyEcom Supplier Map")
            m.update(
                {
                    "ee_vendor_c_id": "99000001",
                    "ee_vendor_id": "WK-99000001",
                    "erpnext_doctype": "Supplier",
                    "erpnext_name": sup_docname,
                    "status": "Mapped",
                }
            )
            m.insert(ignore_permissions=True)
        frappe.db.commit()

        try:
            # Forward: supplier name → ee_vendor_id (PO-Push direction).
            ctx = TransformContext(direction="push")
            result = apply_transformer(
                "lookup_field",
                sup_docname,
                args={
                    "doctype": "EasyEcom Supplier Map",
                    "filter_field": "erpnext_name",
                    "target_field": "ee_vendor_id",
                },
                context=ctx,
            )
            self.assertEqual(result, "WK-99000001")

            # Reverse: ee_vendor_c_id → erpnext_name (GRN-Pull direction).
            ctx_pull = TransformContext(direction="pull")
            result = apply_transformer(
                "lookup_field",
                "99000001",
                args={
                    "doctype": "EasyEcom Supplier Map",
                    "filter_field": "ee_vendor_c_id",
                    "target_field": "erpnext_name",
                },
                context=ctx_pull,
            )
            self.assertEqual(result, sup_docname)
        finally:
            for n in frappe.db.get_all(
                "EasyEcom Supplier Map",
                filters={"ee_vendor_c_id": "99000001"},
                pluck="name",
            ):
                try:
                    frappe.delete_doc(
                        "EasyEcom Supplier Map",
                        n,
                        force=True,
                        ignore_permissions=True,
                    )
                except Exception:
                    pass
            try:
                frappe.delete_doc(
                    "Supplier", sup_docname, force=True, ignore_permissions=True
                )
            except Exception:
                pass
            frappe.db.commit()

    def test_raises_on_missing_row(self) -> None:
        """Stage 2/3 flow code will catch this and surface as
        flag-not-pushed; Stage 1 just verifies the raise happens."""
        ctx = TransformContext(direction="push")
        with self.assertRaises(FieldMappingRuleError):
            apply_transformer(
                "lookup_field",
                "NoSuchSupplier",
                args={
                    "doctype": "EasyEcom Supplier Map",
                    "filter_field": "erpnext_name",
                    "target_field": "ee_vendor_id",
                },
                context=ctx,
            )
